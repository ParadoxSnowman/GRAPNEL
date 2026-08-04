"""Behavioural detectors over AIS tracks near cable corridors.

Design rules, in priority order:

1. A detection is an OBSERVATION WITH EVIDENCE ATTACHED, never an accusation.
   Every Detection carries the track that produced it, the thresholds it
   crossed, and why it scored the way it did. The reader decides what it means.

2. Confidence is capped by the worst input. A behavioural signal computed
   against a hand-drawn cable route cannot exceed LOW no matter how clean the
   drag signature is, because the geometry underneath it is not survey grade.

3. Corroboration outranks behaviour. A 2.1-knot corridor transit is background
   noise in the Baltic. The same transit inside the window of an independently
   observed fault is a finding. outages.corroborate() does that join; nothing
   here may claim HIGH confidence on its own.

Thresholds are seeded from Global Fishing Watch's published parameters where
they overlap (loitering: sub-2-knot mean speed sustained over hours), so the
numbers are defensible against an existing baseline rather than invented.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from shapely.geometry import Point
from shapely.strtree import STRtree

from .cables import CHARTED, CableRoute
from .quality import assess, split_on_impossible
from .geom import angular_diff, haversine_m, initial_bearing, track_stats

log = logging.getLogger(__name__)

CONFIDENCE_LOW = "LOW"
CONFIDENCE_MODERATE = "MODERATE"
CONFIDENCE_HIGH = "HIGH"
_RANK = {CONFIDENCE_LOW: 0, CONFIDENCE_MODERATE: 1, CONFIDENCE_HIGH: 2}


@dataclass
class Thresholds:
    corridor_m: float = 3000.0
    """Half-width of the detection corridor either side of the cable.

    3 km is a compromise. Charted CBLSUB lines are far tighter than that, but a
    dragging anchor sits astern of the reported GPS antenna by the scope paid
    out plus the vessel's LOA - several hundred metres at Baltic depths before
    you account for the AIS position being taken at the bridge.
    """

    drag_max_sog: float = 5.0
    drag_min_sog: float = 0.3
    drag_min_duration_s: float = 1800.0
    drag_min_distance_m: float = 3000.0
    drag_min_cog_stability: float = 0.94
    drag_speed_ratio: float = 0.55
    """Fires when corridor speed falls below this fraction of the vessel's own
    transit speed. Self-referencing beats an absolute threshold: a coaster that
    never exceeds 9 knots and a Panamax doing 14 have very different definitions
    of slow."""

    drag_baseline_min_kn: float = 6.0
    """Below this, the self-reference is not trustworthy and the ratio test is
    dropped. Two situations produce a low baseline and they are indistinguishable
    from inside one window: the hull is genuinely slow (a small fishing vessel
    whose normal speed IS two knots, which must not be flagged), or the drag is
    so long that it dominates the window and the vessel becomes its own
    reference (which must not be silently suppressed). We resolve it by falling
    back to the stricter geometric test below rather than guessing."""

    drag_strict_cog_stability: float = 0.985
    drag_strict_distance_m: float = 5000.0

    loiter_max_sog: float = 2.0
    loiter_min_duration_s: float = 7200.0

    gap_min_s: float = 3600.0
    gap_max_s: float = 172800.0
    gap_max_entry_dist_m: float = 25000.0

    survey_min_legs: int = 4
    survey_min_duration_s: float = 10800.0
    survey_reciprocal_tolerance_deg: float = 35.0
    survey_min_lobe_share: float = 0.55
    survey_min_lobe_balance: float = 0.30
    survey_buffer_m: float = 15000.0
    """Survey runs on a wider buffer than the other detectors. A lawnmower
    pattern is executed *around* a route, not on top of it: the turns happen
    outside the corridor and only the mid-leg crossings fall inside. Scoring it
    on the tight corridor chops the pattern into three-point fragments."""
    survey_min_sog: float = 2.5
    """Floor that separates surveying from drifting. A vessel at anchor in a
    seaway reports wildly unstable COG, which reads as dozens of course
    reversals unless you require it to actually be making way."""
    survey_min_extent_m: float = 5000.0

    impossible_kn: float = 60.0
    """Above this implied speed between fixes the leg is not achievable and the
    track is split there. Generous on purpose: fast ferries touch 45 kn."""

    max_gap_for_continuity_s: float = 1800.0
    """Positions further apart than this start a new segment. A vessel that
    vanished for an hour did not travel in a straight line for that hour, and
    pretending otherwise manufactures fake drag signatures."""


@dataclass
class Detection:
    detection_id: str
    kind: str
    mmsi: int
    cable_id: str
    cable_name: str
    cable_positional_class: str
    cable_source: str
    start_ts: str
    end_ts: str
    duration_s: float
    lat: float
    lon: float
    confidence: str
    score: float
    summary: str
    evidence: dict = field(default_factory=dict)
    track: list = field(default_factory=list)   # [[lon, lat, iso_ts, sog, cog], ...]
    vessel: dict = field(default_factory=dict)
    corroboration: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _cap(confidence: str, route: CableRoute) -> str:
    """Display-only cable geometry cannot support better than LOW."""
    if route.positional_class != CHARTED and _RANK[confidence] > _RANK[CONFIDENCE_LOW]:
        return CONFIDENCE_LOW
    return confidence


def _segments(g: pd.DataFrame, max_gap_s: float):
    """Split one vessel's positions into temporally contiguous segments."""
    if len(g) < 2:
        yield g
        return
    dtsec = g["ts"].diff().dt.total_seconds().fillna(0.0)
    for _, part in g.groupby((dtsec > max_gap_s).cumsum(), sort=True):
        yield part


def _slow_runs(g: pd.DataFrame, th: "Thresholds"):
    """Contiguous runs where the vessel held a speed in the drag band.

    Yielded whole, regardless of where they sit relative to any cable, so that
    distance and course statistics describe the actual manoeuvre rather than
    whatever fraction of it happened to fall inside a buffer.
    """
    for part in _segments(g, th.max_gap_for_continuity_s):
        sog = part["sog"]
        inband = (sog >= th.drag_min_sog) & (sog <= th.drag_max_sog)
        if not inband.any():
            continue
        for _, run in part[inband].groupby((~inband[inband.index]).cumsum(), sort=True):
            if len(run) >= 3:
                yield run.reset_index(drop=True)


def _iso(x) -> str:
    return pd.Timestamp(x).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _track_payload(part: pd.DataFrame, stride: int = 1) -> list:
    """Compact track for the map. Full precision stays in the Parquet archive."""
    return [
        [round(float(r.lon), 5), round(float(r.lat), 5), _iso(r.ts),
         None if pd.isna(r.sog) else round(float(r.sog), 1),
         None if pd.isna(r.cog) else round(float(r.cog), 1)]
        for r in part.iloc[::stride].itertuples()
    ]


class CorridorIndex:
    """Spatial index over cable corridors, plus per-route nearest distance."""

    def __init__(self, routes: list[CableRoute], corridor_m: float):
        self.routes = routes
        self.corridor_m = corridor_m
        self.polys = [r.corridor(corridor_m) for r in routes]
        self.tree = STRtree(self.polys) if self.polys else None
        self._coords = [np.asarray(r.line.coords, dtype="float64") for r in routes]
        # Second index over the route lines themselves. Brute-forcing every
        # vessel against every route's vertices is O(V x R x N) - fine for a
        # gulf, hopeless for 1,378 routes worldwide.
        self._lines = [r.line for r in routes]
        self._line_tree = STRtree(self._lines) if self._lines else None

    def hits(self, lon: float, lat: float) -> list[int]:
        if self.tree is None:
            return []
        pt = Point(lon, lat)
        return [int(i) for i in self.tree.query(pt) if self.polys[int(i)].contains(pt)]

    def mask(self, lons, lats) -> np.ndarray:
        """Boolean matrix, routes x positions."""
        out = np.zeros((len(self.routes), len(lons)), dtype=bool)
        if self.tree is None or len(lons) == 0:
            return out
        for j in range(len(lons)):
            for i in self.hits(float(lons[j]), float(lats[j])):
                out[i, j] = True
        return out

    def nearest(self, lat: float, lon: float):
        """(route_index, geodesic_metres) for the closest route to a point.

        The tree ranks candidates in planar degrees, which is not distance, so
        we take a handful of candidates and re-rank them geodesically. Planar
        ranking alone picks the wrong cable at high latitude, where a degree of
        longitude is half what it is at the equator - and the Baltic and the
        Norwegian Sea are exactly where this tool is most used.
        """
        if self._line_tree is None:
            return None, None
        pt = Point(lon, lat)
        try:
            idxs = self._line_tree.query_nearest(pt, max_distance=None, exclusive=False, all_matches=False)
            cand = [int(i) for i in np.atleast_1d(idxs)]
        except (AttributeError, TypeError):
            cand = [int(self._line_tree.nearest(pt))]
        # Widen with a small planar envelope so the geodesic re-rank has
        # something to choose between.
        try:
            cand += [int(i) for i in self._line_tree.query(pt.buffer(0.6))]
        except Exception:
            pass
        best_i, best_d = None, float("inf")
        for i in dict.fromkeys(cand):
            dm = float(self.distance_m(i, [lat], [lon])[0])
            if dm < best_d:
                best_i, best_d = i, dm
        return best_i, best_d

    def distance_m(self, route_idx: int, lat, lon) -> np.ndarray:
        """Approximate cross-track distance to the densified route line."""
        coords = self._coords[route_idx]
        vlat = np.asarray(lat, dtype="float64").reshape(-1, 1)
        vlon = np.asarray(lon, dtype="float64").reshape(-1, 1)
        return haversine_m(vlat, vlon, coords[None, :, 1], coords[None, :, 0]).min(axis=1)


def _base(kind, part, route, st, conf, score, summary, evidence, track):
    return Detection(
        detection_id="",
        kind=kind,
        mmsi=int(part["mmsi"].iloc[0]),
        cable_id=route.cable_id,
        cable_name=route.name,
        cable_positional_class=route.positional_class,
        cable_source=route.source,
        start_ts=_iso(part["ts"].iloc[0]),
        end_ts=_iso(part["ts"].iloc[-1]),
        duration_s=st.duration_s,
        lat=round(st.mean_lat, 5),
        lon=round(st.mean_lon, 5),
        confidence=_cap(conf, route),
        score=score,
        summary=summary,
        evidence=evidence,
        track=track,
    )


# --------------------------------------------------------------------- drag

def detect_anchor_drag(part, route, ridx, index, th, baseline_sog, in_corridor_m=None):
    """Sustained sub-transit speed on an unnaturally straight course.

    The Yi Peng 3 / Eagle S / Fitburg signature. The discriminating feature is
    not slowness on its own - plenty of traffic slows down - but slowness
    combined with unusually low course variance.
    """
    st = track_stats(list(part["ts"]), part["lat"].values, part["lon"].values,
                     part["sog"].values, part["cog"].values)

    if st.duration_s < th.drag_min_duration_s or st.distance_m < th.drag_min_distance_m:
        return None
    if not (th.drag_min_sog <= st.median_sog <= th.drag_max_sog):
        return None
    if not math.isfinite(st.cog_stability) or st.cog_stability < th.drag_min_cog_stability:
        return None

    have_baseline = bool(baseline_sog) and math.isfinite(baseline_sog) and baseline_sog >= th.drag_baseline_min_kn
    ratio = (st.median_sog / baseline_sog) if (baseline_sog and math.isfinite(baseline_sog) and baseline_sog > 0) else None

    if not have_baseline:
        basis = "geometric_only"
    if have_baseline:
        # Clean case: the hull demonstrably transits faster elsewhere in the
        # window, so a slow straight corridor crossing is anomalous for it.
        if ratio > th.drag_speed_ratio:
            return None
        basis = "self_referenced"
    elif True:
        # No usable self-reference. Demand a near-perfect course hold over real
        # distance instead, which no drifting or manoeuvring hull produces.
        if st.cog_stability < th.drag_strict_cog_stability or st.distance_m < th.drag_strict_distance_m:
            return None
        basis = "geometric_only"

    s_speed = (th.drag_speed_ratio - ratio) / th.drag_speed_ratio if (have_baseline and ratio is not None) else 0.4
    s_stab = (st.cog_stability - th.drag_min_cog_stability) / (1.0 - th.drag_min_cog_stability + 1e-9)
    s_len = min(1.0, st.distance_m / 30_000.0)
    score = round(0.40 * min(1.0, s_stab) + 0.35 * s_len + 0.25 * max(0.0, min(1.0, s_speed)), 3)

    dists = index.distance_m(ridx, part["lat"].values, part["lon"].values)
    conf = CONFIDENCE_LOW if basis == "geometric_only" else (
        CONFIDENCE_MODERATE if score >= 0.5 else CONFIDENCE_LOW)
    return _base(
        "anchor_drag", part, route, st, conf, score,
        f"Held {st.median_sog:.1f} kn on a near-constant course for {st.distance_m/1000:.1f} km"
        + (f", of which {in_corridor_m/1000:.1f} km inside the corridor" if in_corridor_m else "")
        + f" (course stability {st.cog_stability:.3f}).",
        {
            "median_sog_kn": round(st.median_sog, 2),
            "mean_sog_kn": round(st.mean_sog, 2),
            "max_sog_kn": round(st.max_sog, 2),
            "baseline_sog_kn": round(baseline_sog, 2) if baseline_sog and math.isfinite(baseline_sog) else None,
            "speed_ratio_vs_own_baseline": round(ratio, 3) if ratio is not None else None,
            "test_basis": basis,
            "test_basis_note": (
                "Compared against this hull's own upper-quartile transit speed."
                if basis == "self_referenced" else
                "No usable transit baseline in this window - the hull was slow throughout. "
                "Fired on course-hold and distance alone, which is weaker: verify the vessel "
                "type before reading anything into it, as a genuinely slow hull working its "
                "normal trade can look like this."
            ),
            "cog_stability": round(st.cog_stability, 4),
            "slow_run_distance_km": round(st.distance_m / 1000, 2),
            "in_corridor_distance_km": round(in_corridor_m / 1000, 2) if in_corridor_m else None,
            "closest_approach_m": int(np.nanmin(dists)) if len(dists) else None,
            "positions": st.n,
            "nav_status_reported": sorted({str(x) for x in part["nav_status"].dropna().unique()}),
            "thresholds": {
                "corridor_m": th.corridor_m,
                "max_sog_kn": th.drag_max_sog,
                "min_cog_stability": th.drag_min_cog_stability,
                "max_speed_ratio": th.drag_speed_ratio,
            },
        },
        _track_payload(part),
    )


# ------------------------------------------------------------------- loiter

def detect_loiter(part, route, ridx, index, th):
    """Near-stationary presence over a cable, sustained.

    Mirrors Global Fishing Watch's loitering parameterisation rather than
    inventing a threshold. Benign causes are everywhere, so this fires often and
    is only useful once joined to corroboration or repeat presence.
    """
    st = track_stats(list(part["ts"]), part["lat"].values, part["lon"].values,
                     part["sog"].values, part["cog"].values)
    if st.duration_s < th.loiter_min_duration_s:
        return None
    if not math.isfinite(st.mean_sog) or st.mean_sog > th.loiter_max_sog:
        return None

    score = round(min(1.0, st.duration_s / 86400.0) * 0.7
                  + (1.0 - min(1.0, st.mean_sog / th.loiter_max_sog)) * 0.3, 3)
    dists = index.distance_m(ridx, part["lat"].values, part["lon"].values)
    return _base(
        "loiter", part, route, st, CONFIDENCE_LOW, score,
        f"Near-stationary for {st.duration_s/3600:.1f} h over the corridor at {st.mean_sog:.1f} kn mean.",
        {
            "mean_sog_kn": round(st.mean_sog, 2),
            "hours": round(st.duration_s / 3600, 2),
            "closest_approach_m": int(np.nanmin(dists)) if len(dists) else None,
            "positions": st.n,
            "nav_status_reported": sorted({str(x) for x in part["nav_status"].dropna().unique()}),
            "benign_explanations": [
                "designated anchorage or waiting area",
                "fishing on grounds that overlie the route",
                "weather or ice hold",
                "drifting with machinery breakdown",
            ],
        },
        _track_payload(part),
    )


# ------------------------------------------------------------------- survey

def detect_survey_pattern(part, route, ridx, index, th):
    """Reciprocal-leg 'lawnmower' track near a corridor.

    Detected by the *shape of the heading distribution*, not by counting turns.
    A survey track spends most of its time on two courses roughly 180 degrees
    apart, with brief crosslines between them. Counting individual turns fails
    on real geometry: a lawnmower turns 90 degrees twice per line change, so a
    single-turn threshold never trips, while a hull drifting at anchor produces
    dozens of large turns and trips it constantly. The bimodal-heading test
    inverts both errors.

    Over a cable corridor this is either legitimate route survey or
    reconnaissance, and AIS alone cannot distinguish them. It is the closest a
    surface feed gets to a tapping precursor, so it is flagged regardless.
    """
    st = track_stats(list(part["ts"]), part["lat"].values, part["lon"].values,
                     part["sog"].values, part["cog"].values)
    if st.duration_s < th.survey_min_duration_s or len(part) < 12:
        return None
    if not math.isfinite(st.mean_sog) or st.mean_sog < th.survey_min_sog:
        return None

    lat, lon = part["lat"].values, part["lon"].values
    # A drifting hull produces heading spread without covering ground.
    extent = float(haversine_m(lat.min(), lon.min(), lat.max(), lon.max()))
    if extent < th.survey_min_extent_m:
        return None

    # Course made good between fixes, which is more reliable than reported COG
    # at low speed and is unaffected by a stuck or missing COG field.
    brg = initial_bearing(lat[:-1], lon[:-1], lat[1:], lon[1:])
    step = haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])
    brg = brg[step > 50.0]  # ignore fixes where the hull barely moved
    if len(brg) < 10:
        return None

    # Dominant course: circular mean of the largest 10-degree bin cluster.
    hist, edges = np.histogram(brg, bins=36, range=(0.0, 360.0))
    dom_bin = int(np.argmax(hist))
    dom = (dom_bin + 0.5) * 10.0
    opp = (dom + 180.0) % 360.0

    tol = th.survey_reciprocal_tolerance_deg
    on_dom = angular_diff(brg, dom) <= tol
    on_opp = angular_diff(brg, opp) <= tol
    n_dom, n_opp = int(on_dom.sum()), int(on_opp.sum())
    pair = n_dom + n_opp
    if pair == 0:
        return None

    share = pair / len(brg)                       # how much of the track is on the two lobes
    balance = min(n_dom, n_opp) / max(n_dom, n_opp)  # 1.0 = equal time each way
    if share < th.survey_min_lobe_share or balance < th.survey_min_lobe_balance:
        return None

    # Count legs as transitions between the two lobes.
    lobe = np.where(on_dom, 1, np.where(on_opp, -1, 0))
    lobe = lobe[lobe != 0]
    legs = int(np.sum(lobe[1:] != lobe[:-1])) + 1
    if legs < th.survey_min_legs:
        return None

    score = round(min(1.0, legs / 10.0) * 0.45 + balance * 0.30 + min(1.0, share) * 0.25, 3)
    return _base(
        "survey_pattern", part, route, st, CONFIDENCE_LOW, score,
        f"{legs} reciprocal legs on {dom:.0f}/{opp:.0f} degrees over {st.duration_s/3600:.1f} h "
        f"covering {extent/1000:.1f} km near the corridor.",
        {
            "legs": legs,
            "primary_course_deg": round(dom, 1),
            "reciprocal_course_deg": round(opp, 1),
            "lobe_share": round(share, 3),
            "lobe_balance": round(balance, 3),
            "hours": round(st.duration_s / 3600, 2),
            "mean_sog_kn": round(st.mean_sog, 2),
            "pattern_extent_km": round(extent / 1000, 2),
            "buffer_m": th.survey_buffer_m,
            "benign_explanations": ["cable route or pre-lay survey", "seismic or hydrographic work",
                                    "trawling on a set of tows", "search and rescue pattern"],
        },
        _track_payload(part),
    )


# ---------------------------------------------------------------------- gap

def detect_corridor_gap(g, routes, index, th):
    """AIS silence that begins on one side of a corridor and ends on the other.

    Absence of signal is weak evidence on a terrestrial feed: the vessel may
    simply have sailed out of VHF range. We require proximity to the corridor at
    both ends and record the implied straight-line speed so a reviewer can see
    whether the gap is even consistent with crossing.

    The Shunxing-39 case is why this exists: a hull reported operating two
    transponder identities and switching between them presents, in a
    single-MMSI view, as a clean disappearance.
    """
    out = []
    if len(g) < 2:
        return out
    ts = g["ts"]
    lat, lon = g["lat"].values, g["lon"].values
    gaps = ts.diff().dt.total_seconds().fillna(0.0).values

    for i in range(1, len(g)):
        gs = float(gaps[i])
        if not (th.gap_min_s <= gs <= th.gap_max_s):
            continue
        a_hits, b_hits = index.hits(float(lon[i - 1]), float(lat[i - 1])), index.hits(float(lon[i]), float(lat[i]))
        near = set(a_hits) | set(b_hits)
        if not near:
            for ridx in range(len(routes)):
                d = index.distance_m(ridx, [lat[i - 1], lat[i]], [lon[i - 1], lon[i]])
                if float(np.nanmin(d)) <= th.gap_max_entry_dist_m:
                    near.add(ridx)
        if not near:
            continue

        span_m = float(haversine_m(lat[i - 1], lon[i - 1], lat[i], lon[i]))
        implied_kn = (span_m / 1852.0) / (gs / 3600.0) if gs > 0 else 0.0
        crossed = bool(a_hits) != bool(b_hits) or (bool(a_hits) and bool(b_hits) and set(a_hits) != set(b_hits))

        for ridx in sorted(near):
            route = routes[ridx]
            out.append(Detection(
                detection_id="", kind="corridor_gap", mmsi=int(g["mmsi"].iloc[0]),
                cable_id=route.cable_id, cable_name=route.name,
                cable_positional_class=route.positional_class, cable_source=route.source,
                start_ts=_iso(ts.iloc[i - 1]), end_ts=_iso(ts.iloc[i]), duration_s=gs,
                lat=round(float((lat[i - 1] + lat[i]) / 2), 5),
                lon=round(float((lon[i - 1] + lon[i]) / 2), 5),
                confidence=_cap(CONFIDENCE_LOW, route),
                score=round(min(1.0, gs / 43200.0) * 0.6 + (0.4 if crossed else 0.1), 3),
                summary=(f"AIS silent for {gs/3600:.1f} h; reappeared {span_m/1000:.1f} km away "
                         f"{'on the far side of' if crossed else 'near'} the corridor."),
                evidence={
                    "gap_hours": round(gs / 3600, 2),
                    "reappearance_distance_km": round(span_m / 1000, 2),
                    "implied_mean_speed_kn": round(implied_kn, 2),
                    "corridor_crossed": crossed,
                    "last_seen": [round(float(lon[i - 1]), 5), round(float(lat[i - 1]), 5)],
                    "next_seen": [round(float(lon[i]), 5), round(float(lat[i]), 5)],
                    "caveat": ("Terrestrial AIS receivers reach roughly 40-70 nm. A gap in a coastal "
                               "feed is not proof the transponder was switched off. Corroborate with "
                               "satellite AIS or Sentinel-1 SAR before treating this as dark activity."),
                },
                track=_track_payload(g.iloc[max(0, i - 6): i + 6]),
            ))
    return out




def _detect_segment(g, routes, index, survey_index, th, baseline, detections):
    """Run every behavioural detector over one uncorrupted track segment."""
    mask = index.mask(g["lon"].values, g["lat"].values)
    smask = survey_index.mask(g["lon"].values, g["lat"].values)

    detections.extend(detect_corridor_gap(g, routes, index, th))

    # Drag is a property of a contiguous slow run, evaluated end to end. The
    # corridor only decides whether that run is worth reporting. Scoring the
    # corridor slice instead would bound distance by corridor width, which
    # for a perpendicular crossing is twice the buffer no matter how far the
    # anchor was actually dragged.
    for srun in _slow_runs(g, th):
        touched = set()
        for lon_, lat_ in zip(srun["lon"].values, srun["lat"].values):
            touched.update(index.hits(float(lon_), float(lat_)))
        for ridx in sorted(touched):
            inside = srun[index.distance_m(ridx, srun["lat"].values, srun["lon"].values) <= th.corridor_m]
            in_m = float(track_stats(list(inside["ts"]), inside["lat"].values, inside["lon"].values,
                                     inside["sog"].values, inside["cog"].values).distance_m) if len(inside) > 1 else 0.0
            det = detect_anchor_drag(srun, routes[ridx], ridx, index, th, baseline, in_m)
            if det:
                detections.append(det)

    for ridx, route in enumerate(routes):
        if mask[ridx].any():
            for part in _segments(g[mask[ridx]], th.max_gap_for_continuity_s):
                if len(part) < 3:
                    continue
                det = detect_loiter(part, route, ridx, index, th)
                if det:
                    detections.append(det)

        if smask[ridx].any():
            for part in _segments(g[smask[ridx]], th.max_gap_for_continuity_s):
                if len(part) < 12:
                    continue
                det = detect_survey_pattern(part, route, ridx, survey_index, th)
                if det:
                    detections.append(det)


# --------------------------------------------------------------------- run

def run(positions: pd.DataFrame, routes: list[CableRoute], th: Thresholds | None = None,
        quality_out: dict | None = None) -> list[Detection]:
    """Run every detector over a canonical position frame.

    quality_out, if given, is filled with per-MMSI TrackQuality for every track
    that had impossible legs. Those are data-quality findings, not detections,
    and they are published separately so nobody mistakes a broken transponder
    for a threat to a cable.
    """
    th = th or Thresholds()
    if positions.empty or not routes:
        return []

    index = CorridorIndex(routes, th.corridor_m)
    survey_index = CorridorIndex(routes, th.survey_buffer_m)
    detections: list[Detection] = []

    quality = {}
    for _, g in positions.groupby("mmsi", sort=True):
        g = g.sort_values("ts").reset_index(drop=True)
        if len(g) < 2:
            continue

        # Quality before behaviour, always. A track with impossible legs will
        # otherwise manufacture a perfect drag signature out of the corruption:
        # two distant fixes always describe a straight line at a constant speed.
        q = assess(g, th.impossible_kn)
        if q.verdict != "CLEAN":
            quality[int(g["mmsi"].iloc[0])] = q
        if not q.usable_for_behaviour:
            # Two hulls on one MMSI. Nothing observed here can be attributed to
            # a vessel, so there is nothing honest to report about it.
            continue

        # The vessel's own transit speed. Deliberately the 75th percentile of
        # its moving positions, not the median: if a drag or a long loiter
        # dominates the observation window then the median IS the anomaly, and
        # comparing the anomaly against itself gives a ratio of 1.0 and silently
        # suppresses the detection. The upper quartile recovers what the hull
        # does when it is actually going somewhere.
        sog = g["sog"].dropna()
        moving = sog[sog > 1.0]
        baseline = float(np.percentile(moving, 75)) if len(moving) >= 4 else (
            float(moving.max()) if len(moving) else float("nan"))


        for clean in split_on_impossible(g, th.impossible_kn):
            if len(clean) < 2:
                continue
            _detect_segment(clean, routes, index, survey_index, th, baseline, detections)

    for d in detections:
        q = quality.get(d.mmsi)
        if q:
            d.evidence["track_quality"] = q.to_dict()
            d.confidence = CONFIDENCE_LOW
            d.evidence["track_quality_note"] = (
                "This MMSI's track contains legs no vessel could travel. Confidence is "
                "held at LOW because the underlying positions are not trustworthy, "
                "however clean the behavioural signal looks.")

    for d in detections:
        d.detection_id = hashlib.sha1(
            f"{d.kind}|{d.mmsi}|{d.cable_id}|{d.start_ts}|{d.end_ts}".encode()
        ).hexdigest()[:12]

    if quality_out is not None:
        quality_out.update({k: v.to_dict() for k, v in quality.items()})

    detections.sort(key=lambda d: (-_RANK[d.confidence], -d.score, d.start_ts))
    log.info("emitted %d detections across %d routes (%d MMSIs flagged for track quality)",
             len(detections), len(routes), len(quality))
    return detections


def flag_repeat_presence(detections: list[Detection], min_events: int = 2) -> list[Detection]:
    """Annotate hulls that appear over the same cable more than once.

    One slow corridor transit is traffic. The same hull doing it repeatedly on
    the same segment across separate voyages is a pattern, and pattern is the
    only thing a single-source AIS tool can honestly contribute to attribution.
    """
    groups: dict = {}
    for d in detections:
        if d.cable_id:
            groups.setdefault((d.mmsi, d.cable_id), []).append(d)
    for group in groups.values():
        if len(group) < min_events:
            continue
        for d in group:
            d.evidence["repeat_presence"] = {
                "events_on_this_cable": len(group),
                "windows": sorted({g.start_ts[:10] for g in group}),
            }
            if d.confidence == CONFIDENCE_LOW and d.cable_positional_class == CHARTED:
                d.confidence = CONFIDENCE_MODERATE
    return detections
