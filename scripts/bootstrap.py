#!/usr/bin/env python3
"""Backfill real AIS history so the map has real detections immediately.

The problem this solves: Digitraffic serves current positions only. One poll
returns one fix per vessel, and every detector needs a track, so a fresh
deployment produces no detections for hours regardless of how much traffic is
out there. That is correct behaviour and it looks exactly like a broken tool.

The Danish Maritime Authority publish complete daily AIS archives back to 2006,
free, no key. One day of Danish and western Baltic waters gives you dense,
multi-hour tracks for thousands of hulls - enough to exercise every detector on
real vessels the first time you run it.

    python scripts/bootstrap.py --days 2
    python scripts/bootstrap.py --date 2025-12-31 --bbox 14 54 21.5 58

Be aware of the size. Each daily file is 1.5-2.6 GB compressed and roughly ten
million rows. We stream and filter to the bounding box on the way past, so peak
memory stays low, but the download is real and the first run is slow. Files are
cached in .cache/dma/ and reused.

Note the coverage mismatch that trips people up: DMA covers DANISH waters and
the western Baltic. The default Gulf of Finland area of interest is outside it.
Bootstrap with a southern Baltic box (the Bornholm-Oland-Gotland corridor, where
C-Lion1 and the Sweden-Lithuania interconnects run) and switch back to the Gulf
of Finland for live watching.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grapnel import cables, detect, outages  # noqa: E402
from grapnel.config import Config  # noqa: E402
from grapnel.pipeline import archive, publish  # noqa: E402
from grapnel.sources.dma import DMASource  # noqa: E402

log = logging.getLogger("bootstrap")

# Southern Baltic: Bornholm through Oland and Gotland to the Lithuanian coast.
# Overlaps DMA coverage and contains the routes involved in the November 2024
# C-Lion1 and BCS East-West Interlink incidents.
DEFAULT_BBOX = (14.0, 54.0, 21.5, 58.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill detections from the DMA archive")
    ap.add_argument("--days", type=int, default=1, help="how many days back from --date")
    ap.add_argument("--date", default=None, help="end date, YYYY-MM-DD (default: three days ago)")
    ap.add_argument("--bbox", nargs=4, type=float, default=None,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--keep-cache", action="store_true", help="keep the downloaded zips")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    cfg = Config.load(args.config)
    # DMA publish on a lag, so default to a date that is certainly available
    # rather than yesterday, which will 404 about half the time.
    end = (dt.datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
           if args.date else dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3))
    end = end.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - dt.timedelta(days=args.days - 1)).replace(hour=0, minute=0, second=0)

    bbox = tuple(args.bbox) if args.bbox else DEFAULT_BBOX
    cfg.bbox = bbox

    log.info("backfilling %s to %s over bbox %s", start.date(), end.date(), bbox)
    log.info("each day is 1.5-2.6 GB; first run will take a while")

    routes = cables.load_routes(cfg, Path(cfg.cache_dir))
    if not routes:
        log.error("no cable routes loaded for this bbox - nothing to detect against")
        return 1

    src = DMASource(cache_dir=Path(cfg.cache_dir) / "dma")
    positions = src.positions(start, end, bbox)
    static = src.static()

    if positions.empty:
        log.error("no positions returned. Check the date is published at "
                  "https://web.ais.dk/aisdata/ and that the bbox overlaps Danish waters.")
        return 1

    n = positions["mmsi"].nunique()
    log.info("%d positions, %d vessels, %.1f fixes each", len(positions), n, len(positions) / n)

    archive(cfg, positions, static)

    dets = detect.run(positions, routes, cfg.thresholds)
    dets = detect.flag_repeat_presence(dets)
    faults = outages.load_manual_faults(Path(cfg.data_dir) / "faults.json")
    dets = outages.corroborate(dets, faults)

    publish(cfg, routes, dets, faults, positions, static, start, end)

    by_kind: dict[str, int] = {}
    for d in dets:
        by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
    log.info("published %d detections %s", len(dets), by_kind)

    if not args.keep_cache:
        log.info("downloaded archives kept in %s - delete manually to reclaim disk",
                 Path(cfg.cache_dir) / "dma")

    print()
    print(f"{len(dets)} detections from real AIS history.")
    for d in dets[:20]:
        print(f"  [{d.confidence:8}] {d.kind:16} mmsi={d.mmsi} {d.summary[:80]}")
    print()
    print("Serve it:  cd docs && python -m http.server 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
