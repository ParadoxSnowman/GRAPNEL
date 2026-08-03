"""End-to-end run: ingest -> detect -> corroborate -> dossier -> publish.

Invoked by the scheduled workflow and by `python -m grapnel.pipeline`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

import numpy as np

from . import cables, detect, dossier, outages
from .config import Config
from .sources.base import empty_positions, empty_static

log = logging.getLogger("grapnel")

SCHEMA_VERSION = 1


def build_sources(cfg: Config):
    out = []
    for name in cfg.sources:
        if name == "digitraffic":
            from .sources.digitraffic import DigitrafficSource

            out.append(DigitrafficSource())
        elif name == "dma":
            from .sources.dma import DMASource

            out.append(DMASource(cache_dir=Path(cfg.cache_dir) / "dma"))
        else:
            log.warning("unknown source %r, skipping", name)
    return out


def archive(cfg: Config, positions: pd.DataFrame, static: pd.DataFrame) -> None:
    """Append this run's observations to the local Parquet archive.

    Live feeds have no history, so the archive is the only way to accumulate a
    baseline. Partitioned by day so a long-running deployment stays queryable
    without loading everything.
    """
    if positions.empty:
        return
    data_dir = Path(cfg.data_dir)
    for day, part in positions.groupby(positions["ts"].dt.date):
        d = data_dir / "positions" / f"date={day:%Y-%m-%d}"
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
        try:
            part.to_parquet(d / f"part-{stamp}.parquet", index=False)
        except Exception as exc:
            log.warning("parquet write failed (%s); falling back to CSV", exc)
            part.to_csv(d / f"part-{stamp}.csv.gz", index=False, compression="gzip")
    if not static.empty:
        sd = data_dir / "static"
        sd.mkdir(parents=True, exist_ok=True)
        try:
            static.to_parquet(sd / f"static-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}.parquet", index=False)
        except Exception:
            pass


def load_archive(cfg: Config, since: dt.datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read back recent positions and static rows from the archive."""
    data_dir = Path(cfg.data_dir)
    pos_files, stat_files = [], []
    pdir = data_dir / "positions"
    if pdir.exists():
        for day_dir in sorted(pdir.glob("date=*")):
            try:
                day = dt.datetime.strptime(day_dir.name[5:], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if day < since - dt.timedelta(days=1):
                continue
            pos_files.extend(day_dir.glob("*.parquet"))
    sdir = data_dir / "static"
    if sdir.exists():
        stat_files = sorted(sdir.glob("*.parquet"))[-200:]

    def _read(files, empty):
        frames = []
        for f in files:
            try:
                frames.append(pd.read_parquet(f))
            except Exception as exc:
                log.warning("unreadable archive file %s: %s", f, exc)
        return pd.concat(frames, ignore_index=True) if frames else empty()

    positions = _read(pos_files, empty_positions)
    static = _read(stat_files, empty_static)
    if not positions.empty:
        positions = positions[positions["ts"] >= since]
        positions = positions.drop_duplicates(subset=["mmsi", "ts"]).sort_values(["mmsi", "ts"])
    if not static.empty:
        static = static.drop_duplicates(subset=["mmsi", "name", "imo", "callsign", "destination"])
    return positions.reset_index(drop=True), static.reset_index(drop=True)


def _iso(x) -> str:
    return pd.Timestamp(x).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_positions(positions: pd.DataFrame) -> pd.DataFrame:
    """Most recent fix per MMSI."""
    if positions.empty:
        return positions
    return positions.sort_values("ts").groupby("mmsi", as_index=False).last()


def build_vessel_layer(positions: pd.DataFrame, static: pd.DataFrame, index, routes) -> tuple[dict, list]:
    """Current position of every vessel seen, plus who is sitting in a corridor.

    This exists because a live AIS feed returns ONE fix per vessel per poll and
    every detector needs a track. On a fresh deployment there is no track yet,
    so a detections-only map is blank for hours and looks broken. It is not
    broken - it has nothing to say yet - but a blank map is indistinguishable
    from a failure, so the map shows what is actually out there while the
    archive fills.

    The corridor watchlist is the genuinely useful part on day one: "which hulls
    are over a cable right now" needs no history at all, and it is the same
    question a duty watch officer would ask. It is also the part most open to
    misreading, which is why the payload carries its own base-rate warning.
    """
    latest = latest_positions(positions)
    if latest.empty:
        return {"type": "FeatureCollection", "features": []}, []

    meta_by_mmsi: dict[int, dict] = {}
    if not static.empty:
        for r in static.dropna(subset=["mmsi"]).itertuples():
            cur = meta_by_mmsi.setdefault(int(r.mmsi), {})
            for k in ("name", "ship_type", "imo", "destination", "callsign"):
                v = getattr(r, k, None)
                if v is not None and str(v) not in ("", "nan", "<NA>", "None") and not cur.get(k):
                    cur[k] = str(v)

    # Nearest-route distance for every vessel at once: one vectorised pass per
    # route rather than one per vessel, which matters at a few thousand hulls.
    lats = latest["lat"].to_numpy(dtype="float64")
    lons = latest["lon"].to_numpy(dtype="float64")
    best_d = np.full(len(latest), np.inf)
    best_r = np.full(len(latest), -1, dtype=int)
    for ridx in range(len(routes)):
        d = index.distance_m(ridx, lats, lons)
        closer = d < best_d
        best_d[closer] = d[closer]
        best_r[closer] = ridx

    feats, watch = [], []
    for i, r in enumerate(latest.itertuples()):
        mmsi = int(r.mmsi)
        meta = meta_by_mmsi.get(mmsi, {})
        hits = index.hits(float(r.lon), float(r.lat))
        nearest = routes[best_r[i]].name if best_r[i] >= 0 else None

        props = {
            "mmsi": mmsi,
            "name": meta.get("name"),
            "ship_type": meta.get("ship_type"),
            "sog": None if pd.isna(r.sog) else round(float(r.sog), 1),
            "cog": None if pd.isna(r.cog) else round(float(r.cog), 1),
            "nav_status": None if pd.isna(r.nav_status) else str(r.nav_status),
            "ts": _iso(r.ts),
            "in_corridor": bool(hits),
            "nearest_cable": nearest,
            "nearest_m": int(best_d[i]) if np.isfinite(best_d[i]) else None,
        }
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(float(r.lon), 5), round(float(r.lat), 5)]},
            "properties": props,
        })

        if hits:
            watch.append({
                **props,
                "cables": sorted({routes[j].name for j in hits}),
                "cable_positional_class": sorted({routes[j].positional_class for j in hits}),
                "imo": meta.get("imo"),
                "callsign": meta.get("callsign"),
                "destination": meta.get("destination"),
                "lat": round(float(r.lat), 5),
                "lon": round(float(r.lon), 5),
                "vessel": dossier.build(mmsi, static, positions, lat=float(r.lat), lon=float(r.lon)),
            })

    # Slowest first: a stopped hull over a cable is the one worth a look.
    watch.sort(key=lambda w: (w["sog"] if w["sog"] is not None else 99, w["nearest_m"] or 0))
    return {"type": "FeatureCollection", "features": feats}, watch


def publish(cfg: Config, routes, detections, faults, positions, static, window_start, window_end) -> None:
    """Write everything the static site reads. No server, no database."""
    out = Path(cfg.site_data_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- cable layer -------------------------------------------------------
    (out / "cables.geojson").write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [r.to_geojson_feature() for r in routes]},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # --- corridors, for the map to show what "near" actually means ---------
    corridor_feats = []
    for r in routes:
        try:
            poly = r.corridor(cfg.thresholds.corridor_m)
            corridor_feats.append(
                {
                    "type": "Feature",
                    "geometry": json.loads(json.dumps(poly.__geo_interface__)),
                    "properties": {"cable_id": r.cable_id, "name": r.name,
                                   "positional_class": r.positional_class},
                }
            )
        except Exception:
            continue
    (out / "corridors.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": corridor_feats}, separators=(",", ":")),
        encoding="utf-8",
    )

    # --- live vessels and corridor watchlist -------------------------------
    index = detect.CorridorIndex(routes, cfg.thresholds.corridor_m)
    vessel_layer, watchlist = build_vessel_layer(positions, static, index, routes)
    (out / "vessels.geojson").write_text(json.dumps(vessel_layer, separators=(",", ":")), encoding="utf-8")
    (out / "watchlist.json").write_text(
        json.dumps({
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": len(watchlist),
            "note": ("Vessels whose most recent fix falls inside a cable corridor. Presence is "
                     "not behaviour and carries no implication whatsoever: cable routes run "
                     "through shipping lanes, anchorages and fishing grounds, so on a busy day "
                     "this list is mostly ordinary traffic going about its business."),
            "vessels": watchlist[:200],
        }, separators=(",", ":")),
        encoding="utf-8")

    # --- detections with dossiers attached ---------------------------------
    detections = detections[: cfg.max_detections_published]
    for d in detections:
        d.vessel = dossier.build(d.mmsi, static, positions, lat=d.lat, lon=d.lon, ts=d.start_ts)

    (out / "detections.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window": {"start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                "area": {"name": cfg.name, "bbox": list(cfg.bbox)},
                "counts": {
                    "detections": len(detections),
                    "vessels_observed": int(positions["mmsi"].nunique()) if not positions.empty else 0,
                    "positions_observed": int(len(positions)),
                    "routes": len(routes),
                    "routes_charted": sum(1 for r in routes if r.positional_class == cables.CHARTED),
                    "in_corridor_now": len(watchlist),
                },
                "thresholds": vars(cfg.thresholds),
                "detections": [d.to_dict() for d in detections],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    (out / "faults.json").write_text(
        json.dumps({"faults": [f.to_dict() for f in faults]}, separators=(",", ":")),
        encoding="utf-8",
    )

    # --- incident library passes through unchanged -------------------------
    inc_path = Path(cfg.incidents_file)
    if inc_path.exists():
        (out / "incidents.json").write_text(inc_path.read_text(encoding="utf-8"), encoding="utf-8")

    log.info("published %d detections, %d live vessels, %d in corridor -> %s",
             len(detections), len(vessel_layer["features"]), len(watchlist), out)


def run(cfg: Config, refresh_cables: bool = False, window_days: int | None = None) -> int:
    window_days = window_days or 7
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(days=window_days)

    routes = cables.load_routes(cfg, Path(cfg.cache_dir), refresh=refresh_cables)
    if not routes:
        log.error("no cable routes loaded - nothing to detect against")
        return 1

    # Pull whatever is on the wire now and fold it into the archive, then run
    # detection over the whole retained window rather than only the new rows.
    frames_p, frames_s = [], []
    for src in build_sources(cfg):
        try:
            p = src.positions(window_start, now, cfg.bbox)
            s = src.static(window_start, now, cfg.bbox)
            log.info("%s: %d positions, %d static rows", src.name, len(p), len(s))
            frames_p.append(p)
            frames_s.append(s)
        except Exception as exc:
            log.exception("source %s failed: %s", src.name, exc)

    fresh_p = pd.concat(frames_p, ignore_index=True) if frames_p else empty_positions()
    fresh_s = pd.concat(frames_s, ignore_index=True) if frames_s else empty_static()
    archive(cfg, fresh_p, fresh_s)

    positions, static = load_archive(cfg, window_start)
    if positions.empty:
        positions, static = fresh_p, fresh_s
    n_vessels = positions["mmsi"].nunique() if not positions.empty else 0
    per_vessel = (len(positions) / n_vessels) if n_vessels else 0
    log.info("detecting over %d positions from %d vessels (%.1f fixes each)", len(positions), n_vessels, per_vessel)
    if per_vessel < 3:
        log.warning(
            "Only %.1f fixes per vessel in this window. Detectors need a track, not a snapshot, "
            "so expect few or no detections until the archive fills - roughly %d more polls at "
            "the current cadence. The live vessel layer and corridor watchlist work immediately. "
            "Run scripts/bootstrap.py against the DMA archive for detections from real history now.",
            per_vessel, max(0, int(6 - per_vessel)))

    if positions.empty:
        # A run that saw nothing at all is almost always a transport failure,
        # not an empty sea. Publishing it would wipe a working map and make the
        # outage look like a quiet day, so keep the previous payload and fail
        # loudly instead.
        prev = Path(cfg.site_data_dir) / "detections.json"
        if prev.exists():
            log.error("no positions from any source; keeping the previously published payload")
            return 2
        log.error("no positions from any source and nothing previously published")
        return 2

    dets = detect.run(positions, routes, cfg.thresholds)
    dets = detect.flag_repeat_presence(dets)

    faults = outages.load_manual_faults(Path(cfg.data_dir) / "faults.json")
    faults += outages.load_manual_faults(Path(cfg.cache_dir).parent / "config" / "faults.json")
    dets = outages.corroborate(dets, faults)

    publish(cfg, routes, dets, faults, positions, static, window_start, now)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="grapnel", description="Subsea cable threat monitoring")
    ap.add_argument("--config", default=None)
    ap.add_argument("--refresh-cables", action="store_true", help="re-download the cable layer")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    return run(Config.load(args.config), refresh_cables=args.refresh_cables, window_days=args.window_days)


if __name__ == "__main__":
    raise SystemExit(main())
