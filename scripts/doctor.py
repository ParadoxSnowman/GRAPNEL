#!/usr/bin/env python3
"""Diagnose a GRAPNEL install before blaming the detectors.

Most "it shows nothing" reports are not detector problems. They are an
unreachable feed, a bounding box that does not overlap the source's coverage,
a cable layer that failed to download, or an archive that is simply too young
to contain a track. Each of those produces an identical symptom - an empty map -
and none of them raises an exception.

    python scripts/doctor.py          # local, verbose
    python scripts/doctor.py --ci     # writes to the Actions run summary

Exits 0 always in --ci mode: this is a diagnosis, not a gate. It runs first in
the workflow so a misconfiguration is legible on the run page instead of
appearing as a stack trace four hundred lines into the pipeline log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"
MARK = {OK: "PASS", WARN: "WARN", BAD: "FAIL", INFO: "----"}

results: list[tuple[str, str, str]] = []


def check(level, title, detail=""):
    results.append((level, title, detail))


# Coverage envelopes of each source, so we can tell someone their bounding box
# is somewhere the feed simply does not reach. This is the single most common
# cause of a legitimately empty map.
COVERAGE = {
    "digitraffic": ((16.0, 58.0, 32.0, 66.5), "Finnish waters and approaches"),
    "dma": ((3.0, 53.0, 18.0, 60.0), "Danish waters and the western Baltic"),
}


def overlaps(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()

    # ---------------------------------------------------------------- config
    try:
        from grapnel.config import Config
        cfg = Config.load()
        check(OK, "Config loads", f"area '{cfg.name}', bbox {cfg.bbox}, sources {cfg.sources}")
    except Exception as exc:
        check(BAD, "Config failed to load", str(exc))
        return report(args)

    if not cfg.sources:
        check(BAD, "No AIS sources configured", "set `sources:` in config/settings.yml")

    for name in cfg.sources:
        env = COVERAGE.get(name)
        if not env:
            continue
        box, desc = env
        if overlaps(tuple(cfg.bbox), box):
            check(OK, f"bbox overlaps {name} coverage", desc)
        else:
            check(BAD, f"bbox is outside {name} coverage",
                  f"{name} covers {desc} ({box}). Your bbox {tuple(cfg.bbox)} does not intersect it, "
                  f"so this source will always return zero rows. This is the usual reason for an empty map.")

    # --------------------------------------------------------------- network
    if not args.no_network:
        import requests
        endpoints = [
            ("Digitraffic locations", "https://meri.digitraffic.fi/api/ais/v1/locations"),
            ("TeleGeography cables", "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"),
        ]
        if "dma" in cfg.sources:
            endpoints.append(("DMA archive index", "https://web.ais.dk/aisdata/"))

        for label, url in endpoints:
            try:
                r = requests.get(url, timeout=45, stream=True,
                                 headers={"Digitraffic-User": "grapnel/doctor",
                                          "Accept-Encoding": "gzip"})
                if r.ok:
                    check(OK, f"{label} reachable", f"HTTP {r.status_code}")
                else:
                    check(BAD, f"{label} returned HTTP {r.status_code}", url)
                r.close()
            except Exception as exc:
                check(BAD, f"{label} unreachable", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- cables
    try:
        from grapnel import cables
        routes = cables.load_routes(cfg, Path(cfg.cache_dir))
        charted = sum(1 for r in routes if r.positional_class == cables.CHARTED)
        if not routes:
            check(BAD, "No cable routes for this bbox",
                  "Nothing to detect against. Either the cable layer failed to download or no "
                  "route passes through your bounding box.")
        elif charted:
            check(OK, f"{len(routes)} routes loaded", f"{charted} charted, {len(routes)-charted} display-only")
        else:
            check(WARN, f"{len(routes)} routes loaded, none charted",
                  "All geometry is display-grade, so every detection is capped at LOW confidence "
                  "by design. Load ENC CBLSUB data to lift that ceiling.")
    except Exception as exc:
        check(BAD, "Cable layer failed", f"{type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- archive
    pdir = Path(cfg.data_dir) / "positions"
    days = sorted(pdir.glob("date=*")) if pdir.exists() else []
    if not days:
        check(WARN, "Position archive is empty",
              "First run, or the Actions cache was not restored. Detectors need a track, not a "
              "snapshot, so expect no detections for roughly three hours of polling. The live "
              "vessel layer and corridor watchlist work immediately. Use scripts/bootstrap.py "
              "for real detections now.")
    else:
        check(OK, f"Archive spans {len(days)} day(s)", f"{days[0].name[5:]} to {days[-1].name[5:]}")

    # ------------------------------------------------------------- published
    det = Path(cfg.site_data_dir) / "detections.json"
    if det.exists():
        try:
            d = json.loads(det.read_text())
            c = d.get("counts", {})
            obs, pos = c.get("vessels_observed", 0), c.get("positions_observed", 0)
            per = (pos / obs) if obs else 0
            note = f"{len(d.get('detections', []))} detections, {obs} vessels, {per:.1f} fixes each"
            if d.get("demo"):
                check(WARN, "Published payload is SYNTHETIC DEMO DATA", note + " — run the pipeline to replace it")
            elif per < 3:
                check(WARN, "Archive too thin for detection", note +
                      " — this is a bootstrap state, not a fault")
            else:
                check(OK, "Published payload looks healthy", note)
        except json.JSONDecodeError as exc:
            check(BAD, "docs/data/detections.json is not valid JSON", str(exc))
    else:
        check(WARN, "Nothing published yet", "docs/data/detections.json does not exist")

    # ------------------------------------------------------------------- env
    if os.environ.get("GITHUB_ACTIONS"):
        mode = os.environ.get("PAGES_MODE") or "branch (default)"
        check(INFO, "Running in GitHub Actions", f"PAGES_MODE={mode}")

    return report(args)


def report(args) -> int:
    bad = sum(1 for lv, _, _ in results if lv == BAD)
    warn = sum(1 for lv, _, _ in results if lv == WARN)

    lines = ["## Preflight", ""]
    for lv, title, detail in results:
        lines.append(f"- **{MARK[lv]}** {title}" + (f" — {detail}" if detail else ""))
    lines += ["", f"{bad} failing, {warn} warning."]
    if bad:
        lines.append("")
        lines.append("Failures above will produce an empty or stale map regardless of the detectors.")
    text = "\n".join(lines)

    path = os.environ.get("GITHUB_STEP_SUMMARY") if args.ci else None
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")

    for lv, title, detail in results:
        print(f"[{MARK[lv]}] {title}" + (f"\n         {detail}" if detail else ""))
    print(f"\n{bad} failing, {warn} warning.")

    # Never gate CI on a diagnosis.
    return 0 if args.ci else (1 if bad else 0)


if __name__ == "__main__":
    raise SystemExit(main())
