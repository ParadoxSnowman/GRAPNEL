#!/usr/bin/env python3
"""Write the Actions run summary and decide whether the payload actually changed.

Two jobs, both about not wasting anything.

Run summary: every run prints its detections as a markdown table on the Actions
run page, so you can triage from the Actions tab without loading the site. If a
run finds nothing it says so plainly, including why - a thin archive is the
usual reason on a young deployment and it is not a fault.

Change gate: at a 30-minute cadence, most runs produce a byte-identical
detection set and differ only in the generated_at timestamp. Committing those
is 48 commits a day of pure noise, so we hash the meaningful content and emit
changed=true only when it moves. This is what lets the same workflow serve both
Pages modes: branch-mode deployments need the commit, Actions-mode deployments
do not, and neither wants the churn.

Emits GitHub Actions outputs: detections, vessels, in_corridor, changed.
Safe to run locally; without GITHUB_OUTPUT it just prints.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

DATA = Path("docs/data")
DIGEST = DATA / ".digest"


def out(**kw):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in kw.items():
            print(f"::output {k}={v}", file=sys.stderr)
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in kw.items():
            fh.write(f"{k}={v}\n")


def summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(text)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def load(name, default=None):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def main() -> int:
    dets = load("detections.json", {})
    watch = load("watchlist.json", {})
    vessels = load("vessels.geojson", {"features": []})

    rows = dets.get("detections", [])
    counts = dets.get("counts", {})
    n_vessels = len(vessels.get("features", []))
    n_watch = watch.get("count", 0)

    lines = [f"## {len(rows)} detections"]
    if dets.get("demo"):
        lines.append("")
        lines.append("> **Synthetic demo data.** Run the pipeline to replace it.")
    lines += [
        "",
        f"- vessels on the map: **{n_vessels}**",
        f"- inside a cable corridor right now: **{n_watch}**",
        f"- positions in window: {counts.get('positions_observed', 0)}",
        f"- routes: {counts.get('routes_charted', 0)} charted / {counts.get('routes', 0)} total",
        f"- window: {dets.get('window', {}).get('start', '?')} to {dets.get('window', {}).get('end', '?')}",
        "",
    ]

    if rows:
        lines += ["| confidence | kind | MMSI | vessel | summary |", "|---|---|---|---|---|"]
        for r in rows[:25]:
            name = ((r.get("vessel") or {}).get("self_reported") or {}).get("name") or "—"
            s = str(r.get("summary", "")).replace("|", "\\|")[:110]
            lines.append(f"| {r.get('confidence')} | {r.get('kind')} | {r.get('mmsi')} | {name} | {s} |")
        if len(rows) > 25:
            lines.append(f"| … | | | | {len(rows) - 25} more |")
    else:
        pos = counts.get("positions_observed", 0)
        obs = counts.get("vessels_observed", 0)
        per = (pos / obs) if obs else 0
        lines.append("No behavioural detections in this window.")
        lines.append("")
        if per < 3:
            lines.append(
                f"That is expected here: only **{per:.1f} fixes per vessel**. A live AIS feed "
                "returns one position per vessel per poll, and every detector needs a track, "
                "so nothing can fire until the archive fills — roughly three hours of polling. "
                "The live vessel layer and the in-corridor watchlist work immediately. "
                "Run `scripts/bootstrap.py` for real detections from historical data now."
            )
        else:
            lines.append(
                f"The archive is dense enough ({per:.1f} fixes per vessel), so this is a genuine "
                "quiet window rather than a bootstrap problem. Most windows should look like this."
            )

    summary("\n".join(lines))

    # Hash only the meaningful content. generated_at moves every run and is not
    # a change; the detection set and the corridor occupancy are.
    payload = json.dumps(
        {
            "detections": [
                {k: v for k, v in r.items() if k not in ("vessel",)} for r in rows
            ],
            "watch": sorted(str(v.get("mmsi")) for v in watch.get("vessels", [])),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    previous = DIGEST.read_text(encoding="utf-8").strip() if DIGEST.exists() else ""
    changed = digest != previous
    DIGEST.write_text(digest + "\n", encoding="utf-8")

    out(detections=len(rows), vessels=n_vessels, in_corridor=n_watch,
        changed=str(changed).lower())
    print(f"detections={len(rows)} vessels={n_vessels} in_corridor={n_watch} changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
