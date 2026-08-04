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


# ------------------------------------------------------- source field schema

def test_digitraffic_timestamp_units():
    """Location timestamps are SECONDS, metadata timestamps are MILLISECONDS.

    Fintraffic call this out explicitly because it bites everyone. Mixing them
    puts fixes in 1970 or 54000 AD and every duration the detectors compute
    silently becomes garbage, with no error anywhere.
    """
    from grapnel.sources.digitraffic import _timestamp
    assert _timestamp({"time": 1668075025}).year == 2022          # seconds
    assert _timestamp({"timestamp": 1668075026035}).year == 2022  # milliseconds
    assert _timestamp({"timestampExternal": 1668075026035}).year == 2022
    assert _timestamp({}) is None


def test_digitraffic_dimensions_and_sentinels():
    from grapnel.sources.digitraffic import _dim, _ship_type, _sog
    # Length is refA+refB, beam is refC+refD. There is no "length" field.
    assert _dim({"refA": 160, "refB": 33}, "refA", "refB") == 193.0
    assert _dim({"refC": 20, "refD": 12}, "refC", "refD") == 32.0
    assert _ship_type(70) == "Cargo"
    assert _ship_type(80) == "Tanker"
    # 102.3 kn is the AIS "speed not available" sentinel, not a speed.
    assert _sog(102.3) is None
    assert _sog(10.7) == 10.7


# ------------------------------------------------------------ track quality

def test_mmsi_collision_is_excluded_not_reported_as_a_detection():
    """Two hulls sharing an MMSI must produce no detection at all.

    This is the common case in any aggregated feed, and the old code reported it
    as `position_jump` in the same feed as anchor_drag - implying a cable threat
    where there was only a duplicated transponder.
    """
    from grapnel.quality import PROBABLE_MMSI_COLLISION, assess
    rows = []
    for i in range(12):                       # alternates Baltic <-> Med
        rows.append({"ts": T0 + dt.timedelta(minutes=20 * i), "mmsi": 777777777,
                     "lat": 59.8 if i % 2 == 0 else 35.9,
                     "lon": 24.8 if i % 2 == 0 else 14.5,
                     "sog": 8.0, "cog": 90.0, "heading": 90.0,
                     "nav_status": "Under way using engine", "source": "test"})
    g = frame(rows)
    q = assess(g)
    assert q.verdict == PROBABLE_MMSI_COLLISION
    assert q.usable_for_behaviour is False
    assert not any(d.kind == "position_jump" for d in detect.run(g, [route()]))


def test_corrupt_fix_cannot_manufacture_a_drag():
    """A single teleport must not become a fabricated long, straight, slow run.

    Two distant points always describe a straight line at a constant speed, so
    an unsplit track hands anchor_drag a perfect signature built from nothing.
    """
    from grapnel.quality import split_on_impossible
    rows = crossing(888888888, sog=2.0, n=30, span=0.10)
    rows.append({"ts": rows[-1]["ts"] + dt.timedelta(minutes=4), "mmsi": 888888888,
                 "lat": 59.85, "lon": 31.0,   # ~340 km in 4 minutes
                 "sog": 2.0, "cog": 90.0, "heading": 90.0,
                 "nav_status": "Under way using engine", "source": "test"})
    rows += [dict(r, ts=r["ts"] + dt.timedelta(hours=3), lon=31.0 + (r["lon"] - 24.65))
             for r in crossing(888888888, sog=2.0, n=30, span=0.10)]
    g = frame(rows)
    segs = list(split_on_impossible(g))
    assert len(segs) >= 2, "track must be split at the impossible leg"
    for d in detect.run(g, [route()]):
        km = d.evidence.get("slow_run_distance_km")
        if km is not None:
            assert km < 100, f"fabricated {km} km run spanning a teleport"


def test_clean_track_is_not_flagged():
    from grapnel.quality import CLEAN, assess
    assert assess(frame(crossing(999999999, sog=11.0))).verdict == CLEAN


# -------------------------------------------------- moored / anchored vessels

def _station(mmsi, mode, nav, n=60, lat=59.85, lon=24.80):
    """Synthesise the four states a nearly-stationary vessel can be in."""
    import numpy as np
    rng = np.random.default_rng(3)
    rows = []
    for i in range(n):
        if mode == "berth":     dlat, dlon = rng.normal(0, 0.00015), rng.normal(0, 0.00015)
        elif mode == "swing":   a = 2 * np.pi * i / n; dlat, dlon = 0.0018 * np.sin(a), 0.0030 * np.cos(a)
        elif mode == "drag":    dlat, dlon = 0.00010 * i, 0.00016 * i
        else:                   dlat, dlon = rng.normal(0, 0.0004), rng.normal(0, 0.0004)
        rows.append({"ts": T0 + dt.timedelta(minutes=5 * i), "mmsi": mmsi,
                     "lat": lat + dlat, "lon": lon + dlon, "sog": 0.4,
                     "cog": float(rng.uniform(0, 359)), "heading": 511.0,
                     "nav_status": nav, "source": "test"})
    return frame(rows)


def _places(tmp_path):
    from grapnel.anchorages import StoppingPlaces
    return StoppingPlaces(tmp_path)


def test_berthed_vessel_never_fires(tmp_path):
    """The bug that made the tool unusable: cable routes end at landing
    stations, landing stations sit beside ports, and a 3 km corridor swallows
    whole harbours. Every ship at a berth satisfied 'slow for two hours'."""
    g = _station(101010101, "berth", "Moored")
    assert not detect.run(g, [route()], places=_places(tmp_path))


def test_vessel_riding_to_anchor_never_fires(tmp_path):
    g = _station(202020202, "swing", "At anchor")
    assert not detect.run(g, [route()], places=_places(tmp_path))


def test_dragging_anchor_still_fires(tmp_path):
    """The suppression must not swallow the case the project exists for.
    A hull dragging its anchor MOVES, so it fails the confinement test even
    while reporting 'At anchor'."""
    g = _station(303030303, "drag", "At anchor")
    assert detect.run(g, [route()], places=_places(tmp_path))


def test_holding_station_in_open_water_still_fires(tmp_path):
    """Hong Tai 58 held station over the Taiwan-Penghu cable before it was cut.
    Geometrically identical to an anchored ship; only location distinguishes
    them, which is why suppression is location-based rather than geometric."""
    g = _station(404040404, "hold", "Under way using engine")
    assert detect.run(g, [route()], places=_places(tmp_path))


def test_learned_anchorage_suppresses(tmp_path):
    """A place where many DIFFERENT vessels sit still is an anchorage by
    definition, and self-calibrates without any port database."""
    from grapnel.anchorages import StoppingPlaces
    places = StoppingPlaces(tmp_path)
    crowd = pd.concat([_station(500000000 + i, "hold", "At anchor", n=8) for i in range(6)])
    assert places.why_normal(59.85, 24.80) is None
    places.learn(crowd)
    assert places.why_normal(59.85, 24.80) is not None
    assert not detect.run(_station(606060606, "hold", "Under way using engine"),
                          [route()], places=places)


def test_seeded_port_suppresses(tmp_path):
    import json
    from grapnel.anchorages import StoppingPlaces
    p = tmp_path / "ports.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [24.80, 59.85]},
         "properties": {"name": "Test Harbour"}}]}))
    places = StoppingPlaces(tmp_path)
    places.seed(ports_file=p)
    assert "Test Harbour" in (places.why_normal(59.85, 24.80) or "")
    assert places.why_normal(59.85, 20.00) is None
