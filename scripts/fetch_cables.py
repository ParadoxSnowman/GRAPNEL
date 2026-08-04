#!/usr/bin/env python3
"""Fetch real submarine cable geometry and subset it to an area of interest.

TeleGeography stopped maintaining their public GitHub repo and their live API
intermittently refuses automated clients, so relying on a single live fetch at
pipeline time is a bad bet for something that is supposed to run unattended.
This pulls from community mirrors of the same dataset, subsets to a bounding
box, and writes it into data/cables/ where the pipeline picks it up before it
touches the network at all.

    python scripts/fetch_cables.py --area baltic
    python scripts/fetch_cables.py --bbox 120.5 24.5 122.5 26.5 --name taiwan

LICENCE. This is TeleGeography's dataset, CC BY-NC-SA 3.0: attribution
required, non-commercial, share-alike. Redistributing the subset - which is
what this script does - inherits all three. The sidecar .meta.json records
that, and it travels with the data.

POSITIONAL CLASS. This is DISPLAY geometry. TeleGeography draw these routes in
Adobe Illustrator for cartographic clarity; they are not survey positions and
error against the laid route is routinely tens of kilometres. The sidecar marks
it DISPLAY so the detector caps anything built on it at LOW confidence. Do not
hand-edit that field. If you want confident detections, export charted ENC
CBLSUB geometry instead and mark it CHARTED.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

# Community mirrors of the TeleGeography export, newest schema first. Tried in
# order; the first that returns a usable FeatureCollection wins.
MIRRORS = [
    ("v3", "https://raw.githubusercontent.com/lintaojlu/submarine_cable_information/master/web/public/api/v3/cable/cable-geo.json",
           "https://raw.githubusercontent.com/lintaojlu/submarine_cable_information/master/web/public/api/v3/cable/all.json"),
    ("v3", "https://raw.githubusercontent.com/hasan-soliman/CABLE/master/web/public/api/v3/cable/cable-geo.json",
           "https://raw.githubusercontent.com/hasan-soliman/CABLE/master/web/public/api/v3/cable/all.json"),
    ("v2", "https://raw.githubusercontent.com/yvonne-lo/submarinecablemap/master/public/api/v2/cable/cable-geo.json", None),
    ("v2", "https://raw.githubusercontent.com/worldart/aemkei_submarinecablemap/master/public/api/v2/cable/cable-geo.json", None),
    # Upstream last: it 403s automated clients often enough to be unreliable,
    # but when it works it is the freshest.
    ("v3", "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json",
           "https://www.submarinecablemap.com/api/v3/cable/all.json"),
]

AREAS = {
    "baltic":           (9.0, 53.0, 30.5, 66.0),
    "gulf-of-finland":  (18.0, 58.5, 30.5, 61.0),
    "southern-baltic":  (14.0, 54.0, 21.5, 58.0),
    "irish-sea":        (-7.0, 51.5, -2.5, 55.0),
    "north-sea":        (0.0, 51.0, 10.0, 58.5),
    "taiwan":           (118.0, 21.5, 123.5, 27.0),
    "world":            (-180.0, -90.0, 180.0, 90.0),
}


def coords_of(geom):
    c = geom.get("coordinates")
    if c is None:
        return
    stack = [c]
    while stack:
        cur = stack.pop()
        if cur and isinstance(cur[0], (int, float)):
            yield cur
        else:
            stack.extend(cur)


def intersects(geom, box) -> bool:
    minx, miny, maxx, maxy = box
    return any(minx <= p[0] <= maxx and miny <= p[1] <= maxy for p in coords_of(geom))


def fetch(url):
    r = requests.get(url, timeout=90, headers={
        "User-Agent": "grapnel/0.1 (open-source cable monitoring)",
        "Accept": "application/json,*/*",
    })
    r.raise_for_status()
    return r.json()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch and subset real cable geometry")
    ap.add_argument("--area", choices=sorted(AREAS), default="baltic")
    ap.add_argument("--bbox", nargs=4, type=float, default=None,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--name", default=None, help="output basename (defaults to --area)")
    ap.add_argument("--out", default=None, help="output directory (default data/cables)")
    args = ap.parse_args(argv)

    box = tuple(args.bbox) if args.bbox else AREAS[args.area]
    name = args.name or (args.area if not args.bbox else "custom")
    outdir = Path(args.out) if args.out else ROOT / "data" / "cables"
    outdir.mkdir(parents=True, exist_ok=True)

    geo = meta = None
    used = None
    for schema, geo_url, meta_url in MIRRORS:
        try:
            print(f"trying {geo_url}")
            candidate = fetch(geo_url)
            if not isinstance(candidate, dict) or not candidate.get("features"):
                print("  no features, skipping")
                continue
            geo, used = candidate, geo_url
            if meta_url:
                try:
                    raw = fetch(meta_url)
                    meta = {m.get("id"): m for m in raw} if isinstance(raw, list) else {}
                except requests.RequestException:
                    meta = {}
            print(f"  ok: {len(geo['features'])} features ({schema})")
            break
        except (requests.RequestException, ValueError) as exc:
            print(f"  failed: {exc}")

    if geo is None:
        print("\nEvery mirror failed. Check connectivity, or download cable-geo.json by hand "
              "and drop it in data/cables/.", file=sys.stderr)
        return 1

    meta = meta or {}
    kept = []
    for f in geo["features"]:
        if not intersects(f.get("geometry") or {}, box):
            continue
        props = dict(f.get("properties") or {})
        m = meta.get(props.get("id"), {})
        # 'coordinates' in properties is a label anchor, not geometry. Drop it:
        # leaving it in invites someone downstream to read it as the route.
        props.pop("coordinates", None)
        props.pop("color", None)
        if m:
            props["owners"] = m.get("owners", "")
            props["rfs"] = m.get("rfs", "")
            props["length"] = m.get("length", "")
        kept.append({"type": "Feature", "geometry": f["geometry"], "properties": props})

    if not kept:
        print(f"No cables intersect {box}. Widen the box.", file=sys.stderr)
        return 1

    out = outdir / f"telegeography-{name}.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": kept},
                              separators=(",", ":")), encoding="utf-8")

    sidecar = outdir / f"telegeography-{name}.meta.json"
    sidecar.write_text(json.dumps({
        "positional_class": "DISPLAY",
        "source": "TeleGeography via community mirror (CC BY-NC-SA 3.0)",
        "source_url": used,
        "bbox": list(box),
        "features": len(kept),
        "warning": ("Cartographic geometry drawn in Adobe Illustrator, NOT survey positions. "
                    "Error against the laid route is routinely 10-50 km. Detections built on "
                    "this are capped at LOW confidence by design."),
        "licence": "CC BY-NC-SA 3.0 - attribution, non-commercial, share-alike",
    }, indent=2), encoding="utf-8")

    names = sorted({f["properties"].get("name", "?") for f in kept})
    print(f"\nwrote {out}  ({len(kept)} features, {out.stat().st_size:,} bytes)")
    print(f"wrote {sidecar}")
    print(f"\n{len(names)} cables in '{name}':")
    for n in names[:60]:
        print(f"  - {n}")
    if len(names) > 60:
        print(f"  ... and {len(names) - 60} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
