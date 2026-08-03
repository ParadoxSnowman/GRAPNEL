#!/usr/bin/env python3
"""Generate a synthetic demo dataset so the site renders before you have data.

Everything produced here is FABRICATED. MMSIs are drawn from the ITU test range
(970-979 prefixes are reserved for SART/MOB/EPIRB devices, so no real hull can
collide) and vessel names are obviously fictional. The purpose is to exercise
every detector and to give the frontend something to draw. Delete docs/data and
run the real pipeline before publishing anything.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grapnel import cables, detect, dossier, outages  # noqa: E402
from grapnel.config import Config  # noqa: E402
from grapnel.pipeline import publish  # noqa: E402
from grapnel.sources.base import STATIC_COLUMNS, conform  # noqa: E402

random.seed(7)
np.random.seed(7)

NOW = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)

# Two fabricated "charted" routes across the Gulf of Finland, roughly where the
# real Helsinki-Tallinn bundle runs. Positions are invented, not surveyed.
DEMO_ROUTES = [
    ("demo:gof-1", "DEMO Helsinki-Tallinn A", [(24.90, 60.13), (24.86, 60.02), (24.80, 59.88), (24.75, 59.72), (24.72, 59.55)]),
    ("demo:gof-2", "DEMO Gulf of Finland East", [(26.60, 60.30), (26.30, 60.10), (26.00, 59.90), (25.60, 59.70)]),
]


def route_objects():
    from shapely.geometry import LineString
    from grapnel.geom import densify

    return [
        cables.CableRoute(
            cable_id=cid, name=name, positional_class=cables.CHARTED,
            source="DEMO synthetic geometry - not a real cable",
            line=densify(LineString(coords), 2000.0), stated_accuracy_m=50.0,
        )
        for cid, name, coords in DEMO_ROUTES
    ]


def leg(mmsi, t0, points, minutes_per_point, sog, jitter_deg=0.0, nav="Under way using engine"):
    """Build position rows along a polyline at a fixed reporting interval."""
    rows = []
    for i, (lon, lat) in enumerate(points):
        if i + 1 < len(points):
            nlon, nlat = points[i + 1]
            cog = (math.degrees(math.atan2(nlon - lon, nlat - lat)) + 360) % 360
        else:
            cog = rows[-1]["cog"] if rows else 0.0
        rows.append({
            "ts": t0 + dt.timedelta(minutes=minutes_per_point * i),
            "mmsi": mmsi,
            "lat": lat + random.gauss(0, 0.0004),
            "lon": lon + random.gauss(0, 0.0004),
            "sog": max(0.0, sog + random.gauss(0, 0.15)),
            "cog": (cog + random.gauss(0, jitter_deg)) % 360,
            "heading": (cog + random.gauss(0, jitter_deg)) % 360,
            "nav_status": nav,
            "source": "demo",
        })
    return rows


def interp(a, b, n):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


def build_positions():
    rows = []

    # 1) ANCHOR DRAG. Enters at transit speed, slows to 2.4 kn and holds a
    #    dead-straight course for 14 km straight across route A, then resumes.
    m = 970100001
    t = NOW - dt.timedelta(hours=30)
    rows += leg(m, t, interp((24.55, 59.95), (24.74, 59.93), 12), 6, 11.5, jitter_deg=3.0)
    t += dt.timedelta(minutes=72)
    rows += leg(m, t, interp((24.74, 59.93), (24.95, 59.90), 40), 4, 2.4, jitter_deg=0.35)
    t += dt.timedelta(minutes=160)
    rows += leg(m, t, interp((24.95, 59.90), (25.30, 59.86), 12), 6, 11.0, jitter_deg=3.0)

    # 2) LOITER. Sits nearly stationary over route B for 9 hours.
    m = 970100002
    t = NOW - dt.timedelta(hours=20)
    pts = [(26.15 + random.gauss(0, 0.004), 59.99 + random.gauss(0, 0.004)) for _ in range(55)]
    rows += leg(m, t, pts, 10, 0.4, jitter_deg=60.0, nav="At anchor")

    # 3) SURVEY PATTERN. Lawnmower across route A over 12 hours.
    m = 970100003
    t = NOW - dt.timedelta(hours=44)
    pts = []
    for k in range(8):
        lat = 59.80 + k * 0.012
        legpts = interp((24.70, lat), (24.86, lat), 9)
        pts += legpts if k % 2 == 0 else legpts[::-1]
    rows += leg(m, t, pts, 10, 4.2, jitter_deg=2.0)

    # 4) CORRIDOR GAP. Approaches route B, goes silent 7 hours, reappears past it.
    m = 970100004
    t = NOW - dt.timedelta(hours=60)
    rows += leg(m, t, interp((25.30, 59.60), (25.55, 59.68), 10), 12, 10.0, jitter_deg=3.0)
    t += dt.timedelta(hours=7, minutes=48)
    rows += leg(m, t, interp((26.35, 60.14), (26.70, 60.28), 10), 12, 10.0, jitter_deg=3.0)

    # 5) BENIGN TRANSIT. Crosses both routes at full speed. Must NOT fire.
    m = 970100005
    t = NOW - dt.timedelta(hours=12)
    rows += leg(m, t, interp((24.30, 59.70), (26.90, 60.20), 40), 5, 13.5, jitter_deg=2.5)

    # 6) BACKGROUND TRAFFIC. Ordinary hulls scattered across the area, some of
    #    which happen to sit inside a corridor right now. This is what the map
    #    actually looks like in service: mostly unremarkable, and the watchlist
    #    is mostly innocent. A demo showing only detections teaches the wrong
    #    base rate.
    for k in range(48):
        mm = 970200000 + k
        lon0 = 24.2 + random.random() * 2.6
        lat0 = 59.5 + random.random() * 0.9
        brg = random.random() * 2 * math.pi
        span = 0.18 + random.random() * 0.25
        pts = interp((lon0, lat0), (lon0 + span * math.sin(brg), lat0 + span * math.cos(brg) * 0.5), 9)
        rows += leg(mm, NOW - dt.timedelta(hours=3), pts, 12,
                    random.choice([0.2, 0.6, 5.5, 9.0, 11.0, 12.5, 13.0, 14.5]),
                    jitter_deg=3.0,
                    nav=random.choice(["Under way using engine", "Under way using engine",
                                       "At anchor", "Moored", "Engaged in fishing"]))

    # 7) POSITION JUMP. Two positions 240 km apart, ten minutes.
    m = 970100006
    t = NOW - dt.timedelta(hours=8)
    rows += leg(m, t, interp((24.80, 59.90), (24.83, 59.91), 4), 10, 6.0, jitter_deg=4.0)
    rows += leg(m, t + dt.timedelta(minutes=40), interp((27.60, 60.60), (27.63, 60.61), 4), 10, 6.0, jitter_deg=4.0)

    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["mmsi"] = df["mmsi"].astype("int64")
    return df.sort_values(["mmsi", "ts"]).reset_index(drop=True)


DEMO_STATIC = [
    # imo values are deliberately mixed: one valid checksum, one that fails.
    (970100001, "9074729", "DEMO1", "DEMO VESSEL ALFA", "Cargo", 189.0, 30.0, 10.4, "HAIFA"),
    (970100002, "",        "DEMO2", "DEMO VESSEL BRAVO", "Tanker", 144.0, 22.0, 7.9, "TALLINN"),
    (970100003, "9074730", "DEMO3", "DEMO VESSEL CHARLIE", "Other", 88.0, 16.0, 5.1, "RESEARCH"),
    (970100004, "9074720", "DEMO4", "DEMO VESSEL DELTA", "Cargo", 132.0, 20.0, 8.2, "BUSAN"),
    (970100005, "9074731", "DEMO5", "DEMO VESSEL ECHO", "Passenger", 212.0, 29.0, 6.4, "HELSINKI"),
    (970100006, "9074732", "DEMO6", "DEMO VESSEL FOXTROT", "Cargo", 99.0, 18.0, 6.0, "RIGA"),
]


BG_NAMES = ["NORDKAP", "SUOMI STAR", "BALTIC TRADER", "VIRE", "KALLAVESI", "AURORA LINE",
           "PORVOO", "SAIMAA", "HANKO EXPRESS", "MERIKARHU", "TALLINK VOYAGER", "OSTSEE",
           "KOTKA BAY", "LOVIISA", "VAASA CARRIER", "PELLINKI"]
BG_TYPES = ["Cargo", "Tanker", "Passenger", "Other"]


def build_background_static():
    rows = []
    for k in range(48):
        rows.append({
            "mmsi": 970200000 + k,
            "imo": None, "callsign": f"DM{k:03d}",
            "name": f"DEMO {BG_NAMES[k % len(BG_NAMES)]}",
            "ship_type": BG_TYPES[k % len(BG_TYPES)], "cargo_type": None,
            "length": 90 + (k * 7) % 140, "width": 14 + (k * 3) % 18,
            "draught": round(4 + (k % 9) * 0.6, 1),
            "destination": ["HELSINKI", "TALLINN", "KOTKA", "ST PETERSBURG", "RIGA"][k % 5],
            "eta": None, "source": "demo",
        })
    return rows


def build_static():
    rows = [{
        "mmsi": m, "imo": imo or None, "callsign": cs, "name": nm, "ship_type": st,
        "cargo_type": None, "length": ln, "width": wd, "draught": dr,
        "destination": dest, "eta": None, "source": "demo",
    } for m, imo, cs, nm, st, ln, wd, dr, dest in DEMO_STATIC]
    # Give one hull a second reported name so identity churn has something to catch.
    rows.append(dict(rows[3], name="DEMO VESSEL DELTA-2", callsign="DEMO4B"))
    rows += build_background_static()
    return conform(pd.DataFrame(rows), STATIC_COLUMNS)


DEMO_FAULTS = [{
    "fault_id": "demo-operator-notice-1",
    "start": (NOW - dt.timedelta(hours=27)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "end": (NOW - dt.timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "kind": "operator_notice",
    "asset": "DEMO Helsinki-Tallinn A",
    "detail": "Synthetic operator fault notice used to demonstrate the corroboration join.",
    "source_label": "DEMO - not a real notice",
    "source_url": "https://example.invalid/demo",
    "lat": 59.91, "lon": 24.85, "radius_km": 120.0,
}]


def main():
    cfg = Config.load()
    cfg.use_telegeography = False
    routes = route_objects()

    positions = build_positions()
    static = build_static()

    dets = detect.run(positions, routes, cfg.thresholds)
    dets = detect.flag_repeat_presence(dets)
    faults = [outages.Fault(**f) for f in DEMO_FAULTS]
    dets = outages.corroborate(dets, faults)

    publish(cfg, routes, dets, faults, positions, static,
            positions["ts"].min().to_pydatetime(), positions["ts"].max().to_pydatetime())

    # Stamp the payload so the UI can shout that this is not real data.
    p = Path(cfg.site_data_dir) / "detections.json"
    payload = json.loads(p.read_text())
    payload["demo"] = True
    payload["demo_notice"] = ("Synthetic data. Vessels, cables, tracks and the fault notice are "
                              "fabricated to exercise the detectors. Nothing here refers to any "
                              "real vessel or cable.")
    p.write_text(json.dumps(payload, separators=(",", ":")))

    by_kind = {}
    for d in dets:
        by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
    wl = json.loads((Path(cfg.site_data_dir) / "watchlist.json").read_text())
    vl = json.loads((Path(cfg.site_data_dir) / "vessels.geojson").read_text())
    print(f"positions   {len(positions)}")
    print(f"vessels     {positions['mmsi'].nunique()}  (map layer: {len(vl['features'])})")
    print(f"in corridor {wl['count']}")
    print(f"detections  {len(dets)}  {by_kind}")
    for d in dets:
        print(f"  [{d.confidence:8}] {d.kind:16} mmsi={d.mmsi} score={d.score:5.3f} {d.summary[:74]}")


if __name__ == "__main__":
    main()
