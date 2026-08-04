"""Where vessels normally stop, and therefore where stopping means nothing.

This module exists because the loiter detector was flagging moored ships. That
is not a tuning problem, it is a category error: cable routes terminate at
landing stations, landing stations sit beside ports, and a 3 km corridor
swallows whole harbours. Every berthed hull inside one satisfies "under two
knots for two hours" perfectly. The detector was working exactly as specified
and the specification was wrong.

Three layers, weakest to strongest:

  SEEDED. Natural Earth ports (1,081) and cable landing points (1,335). Cheap,
  offline, available on the first run. Covers major harbours and nothing else -
  there are well over ten thousand ports worldwide and uncounted anchorages.

  LEARNED. The real answer, and it needs no dataset at all. A place where many
  DIFFERENT vessels have sat still is an anchorage, by definition. We
  accumulate a grid of distinct MMSIs observed nearly stationary in each cell
  and treat dense cells as normal stopping places. This self-calibrates to
  every berth, roadstead, waiting area and fishing ground on earth, and it gets
  better the longer the deployment runs. Forty stationary ships in one cell are
  a harbour, not forty saboteurs.

  GEOMETRIC. Distinguishing moored from anchored from dragging by the shape of
  the position cloud, which needs no external data whatsoever. See
  `stationarity` below.

The learned layer has one honest weakness: a location where sabotage happens
repeatedly would eventually be learned as normal. Given that requires many
distinct hulls loitering in the same cell over weeks, it is a theoretical
concern rather than a practical one, but the counts are published so anyone can
audit what has been suppressed and why.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .geom import haversine_m

log = logging.getLogger(__name__)

#: Grid cell for the learned layer, in degrees. 0.02 deg is ~2.2 km north-south
#: and less east-west at latitude, which is the right order for an anchorage.
CELL_DEG = 0.02

#: Distinct vessels seen stationary in a cell before it counts as a normal
#: stopping place. Low enough to learn quiet anchorages, high enough that a
#: couple of coincidental stops do not blind the detector.
LEARN_THRESHOLD = 4

#: A fix at or below this speed counts as "stopped" for learning purposes.
STOPPED_KN = 1.0

PORT_RADIUS_M = 4000.0
LANDING_RADIUS_M = 2500.0


def _cell(lat: float, lon: float) -> str:
    return f"{math.floor(lat / CELL_DEG)}:{math.floor(lon / CELL_DEG)}"


@dataclass
class Stationarity:
    """Shape of a position cloud. The discriminator that needs no dataset."""

    n: int
    spread_m: float          # radius containing the cloud
    net_displacement_m: float  # first fix to last
    drift_ratio: float       # net displacement / path length; 1 = straight line
    verdict: str             # BERTHED | SWINGING_AT_ANCHOR | DRIFTING | UNDER_WAY

    def to_dict(self):
        return {"positions": self.n, "spread_m": round(self.spread_m),
                "net_displacement_m": round(self.net_displacement_m),
                "drift_ratio": round(self.drift_ratio, 3), "verdict": self.verdict}


def stationarity(lat, lon) -> Stationarity:
    """Classify a stationary-ish track by geometry alone.

    A moored vessel does not move: its fixes scatter only by GPS noise, tens of
    metres. A vessel at anchor swings through an arc as the tide and wind turn
    it, tracing a rough circle a few hundred metres across, but ends up where it
    started. A vessel dragging its anchor goes somewhere: net displacement is a
    large fraction of the path it travelled.

    That last ratio is the useful one, and it separates the case this project
    cares about from the two it does not, without any port database.
    """
    lat = np.asarray(lat, dtype="float64")
    lon = np.asarray(lon, dtype="float64")
    n = len(lat)
    if n < 2:
        return Stationarity(n, 0.0, 0.0, 0.0, "BERTHED")

    clat, clon = float(np.mean(lat)), float(np.mean(lon))
    spread = float(np.max(haversine_m(lat, lon, clat, clon)))
    net = float(haversine_m(lat[0], lon[0], lat[-1], lon[-1]))
    path = float(np.sum(haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])))
    ratio = (net / path) if path > 1.0 else 0.0

    # Only the berthed case is decidable from geometry alone, and it is decided
    # by scatter: a hull made fast to a pier moves by GPS noise and nothing else.
    #
    # Everything looser is genuinely ambiguous. A vessel constrained to a circle
    # is holding station - which is a correctly anchored ship, and is also
    # exactly what Hong Tai 58 was doing over the Taiwan-Penghu cable before it
    # was cut. The geometry is identical. Only LOCATION separates them: holding
    # station in a roadstead is routine, holding station over a cable in open
    # water is not. That call belongs to StoppingPlaces, not here.
    if spread < 60.0:
        verdict = "BERTHED"
    elif ratio < 0.25:
        verdict = "HOLDING_STATION"     # anchored, hove to, or waiting. Ambiguous.
    elif net > 500.0:
        verdict = "MAKING_GROUND"       # went somewhere: the drag-shaped case
    else:
        verdict = "MANOEUVRING"
    return Stationarity(n, spread, net, ratio, verdict)


class StoppingPlaces:
    """Seeded + learned index of locations where stopping is unremarkable."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "stopping-places.json"
        self.learned: dict[str, set] = {}
        self.seed_points: list[tuple] = []   # (lat, lon, radius_m, label)
        self._load()

    # ----------------------------------------------------------------- state

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.learned = {k: set(v) for k, v in raw.get("cells", {}).items()}
                log.info("loaded %d learned stopping cells", len(self.learned))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("stopping-places file unreadable (%s); starting fresh", exc)

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Cap the memory per cell: once a cell is well past the threshold the
        # exact roster of MMSIs stops mattering and only bloats the file.
        cells = {k: sorted(v)[:40] for k, v in self.learned.items() if v}
        self.path.write_text(json.dumps({
            "cell_deg": CELL_DEG,
            "learn_threshold": LEARN_THRESHOLD,
            "cells": cells,
            "note": ("Cells where this many distinct vessels have been observed nearly "
                     "stationary. Loiter detections here are suppressed as ordinary port, "
                     "anchorage or fishing behaviour. Published so suppression is auditable."),
        }, separators=(",", ":")), encoding="utf-8")

    # ------------------------------------------------------------------ seed

    def seed(self, ports_file: Path | None = None, landings_file: Path | None = None):
        for f, radius, label in ((ports_file, PORT_RADIUS_M, "port"),
                                 (landings_file, LANDING_RADIUS_M, "cable landing")):
            if not f or not Path(f).exists():
                continue
            try:
                data = json.loads(Path(f).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for feat in data.get("features", []):
                c = (feat.get("geometry") or {}).get("coordinates")
                if not c or len(c) < 2:
                    continue
                name = (feat.get("properties") or {}).get("name") or label
                self.seed_points.append((float(c[1]), float(c[0]), radius, f"{label}: {name}"))
        if self.seed_points:
            self._seed_lat = np.array([p[0] for p in self.seed_points])
            self._seed_lon = np.array([p[1] for p in self.seed_points])
            self._seed_rad = np.array([p[2] for p in self.seed_points])
            log.info("seeded %d ports and landing points as stopping places", len(self.seed_points))

    # ----------------------------------------------------------------- learn

    def learn(self, positions: pd.DataFrame):
        """Record which vessels sat still where. Call once per run."""
        if positions.empty or "sog" not in positions:
            return
        slow = positions[positions["sog"].fillna(99) <= STOPPED_KN]
        if slow.empty:
            return
        for r in slow.itertuples():
            self.learned.setdefault(_cell(float(r.lat), float(r.lon)), set()).add(int(r.mmsi))
        log.info("learned from %d stationary fixes; %d cells tracked", len(slow), len(self.learned))

    # ----------------------------------------------------------------- query

    def why_normal(self, lat: float, lon: float):
        """Return a reason string if stopping here is unremarkable, else None."""
        # Learned layer, including the eight neighbouring cells so a berth
        # straddling a cell boundary is not half-suppressed.
        best = 0
        for dlat in (-CELL_DEG, 0.0, CELL_DEG):
            for dlon in (-CELL_DEG, 0.0, CELL_DEG):
                seen = self.learned.get(_cell(lat + dlat, lon + dlon))
                if seen:
                    best = max(best, len(seen))
        if best >= LEARN_THRESHOLD:
            return (f"{best} distinct vessels have been observed stationary in this cell. "
                    "That is an anchorage, berth or fishing ground, not an anomaly.")

        if self.seed_points:
            d = haversine_m(lat, lon, self._seed_lat, self._seed_lon)
            hit = np.where(d <= self._seed_rad)[0]
            if len(hit):
                i = int(hit[np.argmin(d[hit])])
                return (f"Within {d[i]/1000:.1f} km of {self.seed_points[i][3]}. "
                        "Vessels stop here routinely.")
        return None

    def stats(self) -> dict:
        dense = sum(1 for v in self.learned.values() if len(v) >= LEARN_THRESHOLD)
        return {"cells_tracked": len(self.learned), "cells_suppressing": dense,
                "seed_points": len(self.seed_points), "learn_threshold": LEARN_THRESHOLD}
