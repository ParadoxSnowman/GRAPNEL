"""Detector regression tests.

The tests that matter here are the NEGATIVE ones. A cable-monitoring tool that
fires on everything is worse than useless: it trains the reader to ignore it,
and it puts innocent hulls on a public map. test_benign_transit_is_silent and
test_display_geometry_caps_confidence are the ones to keep green.
"""

import datetime as dt
import math
import sys
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grapnel import detect  # noqa: E402
from grapnel.cables import CHARTED, DISPLAY, CableRoute  # noqa: E402
from grapnel.dossier import decode_mid, validate_imo  # noqa: E402
from grapnel.geom import densify, haversine_m, track_stats  # noqa: E402

T0 = dt.datetime(2026, 1, 15, 0, 0, tzinfo=dt.timezone.utc)


def route(pclass=CHARTED):
    return CableRoute(
        cable_id="t1", name="Test cable", positional_class=pclass,
        source="test", line=densify(LineString([(24.80, 59.60), (24.80, 60.10)]), 2000.0),
    )


def frame(rows):
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["mmsi"] = df["mmsi"].astype("int64")
    return df.sort_values(["mmsi", "ts"]).reset_index(drop=True)


def crossing(mmsi, sog, n=60, minutes=4, cog=90.0, lat=59.85, span=0.30, nav="Under way using engine"):
    """East-west track crossing the test cable at a constant speed and course."""
    return [{
        "ts": T0 + dt.timedelta(minutes=minutes * i),
        "mmsi": mmsi, "lat": lat, "lon": 24.65 + span * i / (n - 1),
        "sog": sog, "cog": cog, "heading": cog, "nav_status": nav, "source": "test",
    } for i in range(n)]


# ------------------------------------------------------------------ geometry

def test_densify_bounds_segment_length():
    line = densify(LineString([(24.0, 59.0), (24.0, 60.0)]), 2000.0)
    coords = list(line.coords)
    longest = max(
        float(haversine_m(a[1], a[0], b[1], b[0]))
        for a, b in zip(coords[:-1], coords[1:])
    )
    assert longest <= 2100.0


def test_cog_stability_separates_straight_from_wandering():
    ts = [T0 + dt.timedelta(minutes=5 * i) for i in range(20)]
    lat = [59.8] * 20
    lon = [24.7 + 0.005 * i for i in range(20)]
    straight = track_stats(ts, lat, lon, [3.0] * 20, [90.0] * 20)
    wander = track_stats(ts, lat, lon, [3.0] * 20, [(i * 47) % 360 for i in range(20)])
    assert straight.cog_stability > 0.99
    assert wander.cog_stability < 0.5


# ------------------------------------------------------------------- drag

def test_anchor_drag_fires_on_slow_straight_crossing():
    rows = crossing(111111111, sog=12.0, n=20, span=0.10)          # transit in
    rows += [dict(r, ts=r["ts"] + dt.timedelta(hours=2), lon=r["lon"] + 0.10, sog=2.2)
             for r in crossing(111111111, sog=2.2, n=60, span=0.12)]
    rows += [dict(r, ts=r["ts"] + dt.timedelta(hours=8), lon=r["lon"] + 0.30, sog=12.0)
             for r in crossing(111111111, sog=12.0, n=20, span=0.10)]
    dets = detect.run(frame(rows), [route()])
    assert any(d.kind == "anchor_drag" for d in dets)


def test_benign_transit_is_silent():
    """A hull crossing at normal speed must produce nothing at all."""
    dets = detect.run(frame(crossing(222222222, sog=13.0)), [route()])
    assert [d.kind for d in dets] == []


def test_slow_but_wandering_is_not_a_drag():
    """Slow alone is not the signal. Course stability is."""
    rows = crossing(333333333, sog=2.2, n=60)
    for i, r in enumerate(rows):
        r["cog"] = (90 + 60 * math.sin(i / 2.0)) % 360
        r["lat"] = 59.85 + 0.004 * math.sin(i / 2.0)
    dets = detect.run(frame(rows), [route()])
    assert not any(d.kind == "anchor_drag" for d in dets)


def test_baseline_uses_upper_quartile_not_median():
    """Regression: when the drag dominates the window, a median baseline makes
    the vessel its own reference and silently suppresses the detection."""
    rows = crossing(444444444, sog=12.0, n=8, span=0.04)
    rows += [dict(r, ts=r["ts"] + dt.timedelta(hours=1), lon=r["lon"] + 0.04, sog=2.0)
             for r in crossing(444444444, sog=2.0, n=70, span=0.14)]
    dets = detect.run(frame(rows), [route()])
    assert any(d.kind == "anchor_drag" for d in dets)


# ------------------------------------------------------------- confidence

def test_display_geometry_caps_confidence():
    """Display-grade cable geometry must never yield better than LOW."""
    rows = crossing(555555555, sog=12.0, n=20, span=0.10)
    rows += [dict(r, ts=r["ts"] + dt.timedelta(hours=2), lon=r["lon"] + 0.10, sog=2.2)
             for r in crossing(555555555, sog=2.2, n=60, span=0.12)]
    dets = detect.run(frame(rows), [route(DISPLAY)])
    assert dets, "expected at least one detection to test the cap"
    assert all(d.confidence == detect.CONFIDENCE_LOW for d in dets)


# -------------------------------------------------------------------- gap

def test_corridor_gap_requires_proximity():
    """A gap far from any cable is not a corridor gap."""
    rows = [
        {"ts": T0, "mmsi": 666666666, "lat": 55.0, "lon": 15.0, "sog": 11.0,
         "cog": 45.0, "heading": 45.0, "nav_status": "Under way using engine", "source": "test"},
        {"ts": T0 + dt.timedelta(hours=6), "mmsi": 666666666, "lat": 55.4, "lon": 15.6,
         "sog": 11.0, "cog": 45.0, "heading": 45.0, "nav_status": "Under way using engine", "source": "test"},
    ]
    assert not detect.run(frame(rows), [route()])


# ---------------------------------------------------------------- identity

@pytest.mark.parametrize("mmsi,flag", [
    (273123456, "Russian Federation"),
    (375123456, "St Vincent & the Grenadines"),
    (613123456, "Cameroon"),
    (230123456, "Finland"),
    (477123456, "Hong Kong"),
])
def test_mid_decoding(mmsi, flag):
    assert decode_mid(mmsi)["flag_from_mid"] == flag


def test_low_transparency_registry_is_flagged_as_context():
    assert decode_mid(613123456)["low_transparency_registry"] is True
    assert decode_mid(230123456)["low_transparency_registry"] is False


def test_imo_check_digit():
    assert validate_imo("9074729")["valid"] is True     # valid checksum
    assert validate_imo("1234567")["valid"] is True     # coincidentally valid: 77 % 10 == 7
    assert validate_imo("9074720")["valid"] is False    # fails checksum
    assert validate_imo("907472")["valid"] is False     # wrong length
    assert validate_imo("")["present"] is False
