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

    log.info("published %d detections to %s", len(detections), out)


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
    log.info("detecting over %d positions from %d vessels", len(positions), positions["mmsi"].nunique() if not positions.empty else 0)

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
