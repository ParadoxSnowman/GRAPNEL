"""Cable route ingest and corridor construction.

Two classes of cable geometry. Conflating them is the single most common way to
build a detection tool that produces nothing but noise:

  DISPLAY geometry (TeleGeography). Routes are hand-drawn in Adobe Illustrator
  for cartographic clarity. They are not survey positions; error against the
  laid route is routinely 10-50 km. Licensed CC BY-NC-SA 3.0. Use for the map
  layer and for naming cables. NEVER as the sole basis for a detection.

  CHARTED geometry (S-57/S-101 ENC, object classes CBLSUB and CBLARE). Issued
  by national hydrographic offices with a stated positional accuracy. This is
  what a mariner is legally navigating against. Use for detection corridors.

load_routes() runs happily on display geometry alone, but every route carries
positional_class, and detect.py refuses to emit anything above LOW confidence
from a DISPLAY-class route.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests
from shapely.geometry import LineString, MultiLineString, shape

from .geom import buffer_line_metres, densify

log = logging.getLogger(__name__)

TELEGEOGRAPHY_CABLE_GEO = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
TELEGEOGRAPHY_CABLE_META = "https://www.submarinecablemap.com/api/v3/cable/all.json"
TELEGEOGRAPHY_LANDINGS = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"

DISPLAY = "DISPLAY"
CHARTED = "CHARTED"


@dataclass
class CableRoute:
    cable_id: str
    name: str
    positional_class: str  # DISPLAY | CHARTED
    source: str
    line: LineString
    owners: str = ""
    rfs: str = ""
    length_km: float | None = None
    stated_accuracy_m: float | None = None
    corridors: dict = field(default_factory=dict, repr=False)

    def corridor(self, metres: float):
        key = float(metres)
        if key not in self.corridors:
            self.corridors[key] = buffer_line_metres(self.line, key)
        return self.corridors[key]

    def to_geojson_feature(self):
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[round(x, 5), round(y, 5)] for x, y in self.line.coords]},
            "properties": {
                "cable_id": self.cable_id,
                "name": self.name,
                "positional_class": self.positional_class,
                "source": self.source,
                "owners": self.owners,
                "rfs": self.rfs,
                "length_km": self.length_km,
                "stated_accuracy_m": self.stated_accuracy_m,
            },
        }


def _explode(geom):
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        yield from geom.geoms


def _intersects_bbox(line, bbox) -> bool:
    minx, miny, maxx, maxy = bbox
    lminx, lminy, lmaxx, lmaxy = line.bounds
    return not (lmaxx < minx or lminx > maxx or lmaxy < miny or lminy > maxy)


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None


def fetch_telegeography(cache_dir: Path, refresh: bool = False) -> tuple[dict, dict]:
    """Download and cache the display layer.

    Their public GitHub mirror stopped being maintained; the v3 API endpoints
    still serve. Cached aggressively so a Pages build does not hammer them.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    geo_path = cache_dir / "telegeography-cable-geo.json"
    meta_path = cache_dir / "telegeography-cable-all.json"

    for path, url in ((geo_path, TELEGEOGRAPHY_CABLE_GEO), (meta_path, TELEGEOGRAPHY_CABLE_META)):
        if path.exists() and not refresh:
            continue
        log.info("fetching %s", url)
        r = requests.get(url, timeout=60, headers={"User-Agent": "grapnel/0.1"})
        r.raise_for_status()
        path.write_text(r.text, encoding="utf-8")

    geo = json.loads(geo_path.read_text(encoding="utf-8"))
    try:
        meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = {m.get("id"): m for m in meta_raw} if isinstance(meta_raw, list) else {}
    except (json.JSONDecodeError, OSError):
        meta = {}
    return geo, meta


def routes_from_telegeography(geo: dict, meta: dict, bbox=None) -> list[CableRoute]:
    out = []
    for feat in geo.get("features", []):
        props = feat.get("properties") or {}
        cid = str(props.get("id") or props.get("cable_id") or props.get("name", "unknown"))
        m = meta.get(cid, {})
        try:
            g = shape(feat["geometry"])
        except (KeyError, ValueError, AttributeError):
            continue
        for i, ls in enumerate(_explode(g)):
            if bbox and not _intersects_bbox(ls, bbox):
                continue
            out.append(CableRoute(
                cable_id=f"{cid}:{i}" if i else cid,
                name=props.get("name", cid),
                positional_class=DISPLAY,
                source="TeleGeography (CC BY-NC-SA 3.0) - display geometry, not survey",
                line=densify(ls, 2000.0),
                owners=str(m.get("owners", "") or props.get("owners", "")),
                rfs=str(m.get("rfs", "") or props.get("rfs", "")),
                length_km=_to_float(m.get("length")),
            ))
    return out


def routes_from_enc_geojson(path: Path, source_label: str, stated_accuracy_m: float | None = None) -> list[CableRoute]:
    """Load charted cable geometry exported from ENC data.

    Produce the input with GDAL against an S-57 cell:

        ogr2ogr -f GeoJSON fi_cblsub.json ENC_ROOT/FI5xxxxx.000 CBLSUB

    Any GeoJSON of LineStrings works; OBJNAM is used for the name if present.
    """
    path = Path(path)
    if not path.exists():
        log.warning("charted cable file missing: %s", path)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, feat in enumerate(data.get("features", [])):
        props = feat.get("properties") or {}
        try:
            g = shape(feat["geometry"])
        except (KeyError, ValueError, AttributeError):
            continue
        name = props.get("OBJNAM") or props.get("name") or f"{source_label} CBLSUB {i}"
        for j, ls in enumerate(_explode(g)):
            out.append(CableRoute(
                cable_id=f"enc:{source_label}:{i}:{j}",
                name=str(name),
                positional_class=CHARTED,
                source=source_label,
                line=densify(ls, 2000.0),
                stated_accuracy_m=stated_accuracy_m,
            ))
    return out


def load_routes(cfg, cache_dir: Path, refresh: bool = False) -> list[CableRoute]:
    """Assemble the route set for the configured area of interest."""
    routes: list[CableRoute] = []

    for entry in cfg.charted_cable_files:
        routes.extend(routes_from_enc_geojson(
            Path(entry["path"]), entry.get("label", entry["path"]), entry.get("stated_accuracy_m")))

    if cfg.use_telegeography:
        try:
            geo, meta = fetch_telegeography(Path(cache_dir), refresh=refresh)
            routes.extend(routes_from_telegeography(geo, meta, bbox=cfg.bbox))
        except (requests.RequestException, OSError, json.JSONDecodeError) as exc:
            log.warning("TeleGeography fetch failed (%s); continuing with charted routes only", exc)

    charted = sum(1 for r in routes if r.positional_class == CHARTED)
    log.info("loaded %d routes (%d charted, %d display-only)", len(routes), charted, len(routes) - charted)
    return routes
