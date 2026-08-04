"""Track quality: what a position jump actually means.

Shipping `position_jump` as a detection was wrong, and wrong in a way that
mattered. It sat in the same feed as anchor_drag, implying it was evidence of
something, when the overwhelming majority of impossible-speed legs in an
aggregated AIS feed have nothing to do with any vessel's behaviour:

  MMSI COLLISION. Two hulls transmitting the same MMSI. Endemic - cheap or
  misconfigured transponders ship with factory defaults, small craft clone
  numbers, and some operators simply reuse them. In a feed that aggregates
  receivers worldwide, one MMSI in New York and the "same" one in Singapore is
  a certainty, not an anomaly. This is by far the most common cause.

  DECODE AND AGGREGATION ARTEFACTS. A corrupted NMEA sentence with a valid
  checksum, or out-of-order delivery across receivers, produces one wild fix
  and then normality resumes.

  ACTUAL GNSS SPOOFING. Real, and interesting, but rare next to the above -
  and it does not usually look like a jump. It looks like a vessel sitting in
  an airport, or tracing circles, which are different signatures entirely.

None of those is a cable threat. So a jump is not a finding; it is a warning
that the track is unreliable. That matters more than it sounds, because every
behavioural detector computes statistics over a track. Run `anchor_drag` across
a leg that teleports 400 km and you get a fabricated 400 km "drag" at an
implausible average speed - a false positive manufactured by the corruption
itself. Splitting tracks at impossible legs BEFORE detection is therefore not
tidiness, it is a correctness requirement.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .geom import haversine_m

log = logging.getLogger(__name__)

CLEAN = "CLEAN"
SUSPECT_DECODE = "SUSPECT_DECODE"
PROBABLE_MMSI_COLLISION = "PROBABLE_MMSI_COLLISION"

#: Above this implied speed between consecutive fixes, no displacement hull and
#: no realistic small craft can have made the passage. Deliberately generous:
#: fast ferries reach 45 kn, and hydrofoils and some naval craft exceed it.
IMPOSSIBLE_KN = 60.0

#: Two position clusters further apart than this, alternating in time, are two
#: different vessels rather than one that moved.
COLLISION_SEPARATION_M = 200_000.0
COLLISION_MIN_LEGS = 4


@dataclass
class TrackQuality:
    mmsi: int
    verdict: str
    n_positions: int
    n_impossible_legs: int
    max_implied_kn: float | None
    n_segments: int
    usable_for_behaviour: bool
    notes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _impossible_mask(g: pd.DataFrame, limit_kn: float) -> np.ndarray:
    """Boolean over legs (length n-1): True where the leg is not achievable."""
    if len(g) < 2:
        return np.zeros(0, dtype=bool)
    lat, lon = g["lat"].to_numpy("float64"), g["lon"].to_numpy("float64")
    dtsec = g["ts"].diff().dt.total_seconds().to_numpy("float64")[1:]
    d = haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        kn = (d / 1852.0) / (dtsec / 3600.0)
    # A zero-duration leg is a duplicate timestamp, not a teleport.
    return np.isfinite(kn) & (dtsec > 0) & (kn > limit_kn), kn


def assess(g: pd.DataFrame, limit_kn: float = IMPOSSIBLE_KN) -> TrackQuality:
    """Classify why a track contains impossible legs, if it does."""
    mmsi = int(g["mmsi"].iloc[0])
    if len(g) < 2:
        return TrackQuality(mmsi, CLEAN, len(g), 0, None, 1, True)

    bad, kn = _impossible_mask(g, limit_kn)
    n_bad = int(bad.sum())
    max_kn = float(np.nanmax(kn)) if len(kn) and np.isfinite(kn).any() else None

    if n_bad == 0:
        return TrackQuality(mmsi, CLEAN, len(g), 0, max_kn, 1, True)

    # Segment the track at every impossible leg, then ask whether the segments
    # occupy distinct places. Repeated alternation between distant clusters is
    # the fingerprint of two hulls, not one.
    seg_id = np.concatenate([[0], np.cumsum(bad)])
    centroids = []
    for _, part in g.groupby(seg_id):
        centroids.append((part["lat"].mean(), part["lon"].mean(), len(part)))

    max_sep = 0.0
    for i in range(len(centroids)):
        for j in range(i + 1, len(centroids)):
            max_sep = max(max_sep, float(haversine_m(
                centroids[i][0], centroids[i][1], centroids[j][0], centroids[j][1])))

    notes = []
    if n_bad >= COLLISION_MIN_LEGS and max_sep >= COLLISION_SEPARATION_M:
        verdict = PROBABLE_MMSI_COLLISION
        usable = False
        notes.append(
            f"{n_bad} impossible legs alternating between locations up to "
            f"{max_sep/1000:.0f} km apart. That is two or more hulls sharing this MMSI, "
            "not one vessel moving. Behaviour cannot be attributed to a hull here, so "
            "this MMSI is excluded from behavioural detection entirely.")
    else:
        verdict = SUSPECT_DECODE
        usable = True
        notes.append(
            f"{n_bad} impossible leg(s), peak implied speed {max_kn:.0f} kn. Most likely a "
            "corrupted fix or out-of-order delivery across receivers. The track is split at "
            "these legs and each clean segment is analysed separately; statistics are never "
            "computed across a break.")

    return TrackQuality(mmsi, verdict, len(g), n_bad, max_kn, len(centroids), usable, notes)


def split_on_impossible(g: pd.DataFrame, limit_kn: float = IMPOSSIBLE_KN):
    """Yield contiguous sub-tracks containing no impossible legs.

    This is the correctness step. Without it a single corrupted fix produces a
    fabricated multi-hundred-kilometre "drag" with a beautifully stable course,
    because two distant points always describe a straight line.
    """
    if len(g) < 2:
        yield g
        return
    bad, _ = _impossible_mask(g, limit_kn)
    seg_id = np.concatenate([[0], np.cumsum(bad)])
    for _, part in g.groupby(seg_id, sort=True):
        yield part.reset_index(drop=True)


def assess_all(positions: pd.DataFrame, limit_kn: float = IMPOSSIBLE_KN) -> dict:
    """Quality verdict per MMSI across a whole position frame."""
    out = {}
    if positions.empty:
        return out
    for mmsi, g in positions.groupby("mmsi", sort=False):
        q = assess(g.sort_values("ts"), limit_kn)
        if q.verdict != CLEAN:
            out[int(mmsi)] = q
    if out:
        collisions = sum(1 for q in out.values() if q.verdict == PROBABLE_MMSI_COLLISION)
        log.info("track quality: %d MMSIs with impossible legs (%d probable collisions, "
                 "excluded from detection)", len(out), collisions)
    return out
