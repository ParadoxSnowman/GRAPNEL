"""Corroboration against non-AIS evidence that something actually broke.

This module is the reason GRAPNEL is worth building rather than being another
proximity alarm. Behavioural detection on its own has an unusable base rate: in
the Gulf of Finland in winter, dozens of hulls a week satisfy "slow over a
cable". What converts one of those into a finding is an *independent* observation,
made by an instrument that has no idea any vessel was present, that a cable
failed inside the same window.

Four kinds of independent evidence, in descending order of strength:

  Operator notice. The cable owner announces a fault. Unambiguous, but slow and
  often never published at all for commercial reasons.

  Grid telemetry. For power interconnectors, transfer capacity is public on
  ENTSO-E's transparency platform in near real time. Estlink 2 dropping from
  1,016 MW to 358 MW on 25 December 2024 is visible there with no reference to
  any ship.

  Active measurement. RIPE Atlas and Cloudflare Radar observe reachability and
  latency continuously. A cable cut shows as a latency step or a path change on
  the affected routes even when nobody announces anything.

  Passive observation. IODA at Georgia Tech fuses BGP, active probing and
  darknet traffic into per-country and per-ASN outage signals.

The join is deliberately conservative: corroboration only attaches when the
detection window overlaps the fault window, and it never raises confidence past
MODERATE on display-class cable geometry.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

from .detect import CONFIDENCE_HIGH, CONFIDENCE_MODERATE, CONFIDENCE_LOW, Detection
from .cables import CHARTED

log = logging.getLogger(__name__)

IODA_ALERTS = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"


@dataclass
class Fault:
    fault_id: str
    start: str  # ISO8601 UTC
    end: str
    kind: str  # operator_notice | grid_capacity | active_measurement | passive_outage
    asset: str
    detail: str
    source_label: str
    source_url: str
    lat: float | None = None
    lon: float | None = None
    radius_km: float = 150.0

    def to_dict(self):
        return asdict(self)


def load_manual_faults(path: Path) -> list[Fault]:
    """Curated faults: operator notices, regulator statements, press reporting.

    Format is a JSON list of Fault fields. Kept manual because the authoritative
    signal - "the owner says this cable broke at this time" - has no API.
    """
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("faults", raw) if isinstance(raw, dict) else raw
    out = []
    for r in items:
        try:
            out.append(Fault(**r))
        except TypeError as exc:
            log.warning("skipping malformed fault record: %s", exc)
    return out


def fetch_ioda(country_codes: list[str], start: dt.datetime, end: dt.datetime) -> list[Fault]:
    """Pull outage alerts from IODA for the given ISO country codes.

    Country-level granularity is coarse for a single cable, so these come back
    as weak corroboration. Useful mainly for small, poorly-connected territories
    where one cable is a large share of total capacity - the Matsu case being
    the obvious example.
    """
    out = []
    for cc in country_codes:
        params = {
            "from": int(start.timestamp()),
            "until": int(end.timestamp()),
            "entityType": "country",
            "entityCode": cc,
        }
        try:
            r = requests.get(IODA_ALERTS, params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("IODA fetch failed for %s: %s", cc, exc)
            continue

        for a in (payload.get("data") or []):
            try:
                t0 = dt.datetime.fromtimestamp(int(a["time"]), tz=dt.timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                Fault(
                    fault_id=f"ioda:{cc}:{a.get('time')}:{a.get('datasource','')}",
                    start=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    end=(t0 + dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    kind="passive_outage",
                    asset=f"{cc} national connectivity",
                    detail=f"IODA {a.get('datasource','')} alert, level {a.get('level','')}",
                    source_label="IODA (Georgia Tech)",
                    source_url="https://ioda.inetintel.cc.gatech.edu/",
                    radius_km=400.0,
                )
            )
    log.info("IODA returned %d alerts", len(out))
    return out


def _overlap(a0, a1, b0, b1) -> bool:
    return a0 <= b1 and b0 <= a1


def _parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def corroborate(detections: list[Detection], faults: list[Fault], slack_hours: float = 12.0) -> list[Detection]:
    """Attach overlapping faults to detections and re-grade confidence.

    slack_hours widens the fault window on both sides. Faults are usually
    detected some time after they occur - a telecom operator notices when
    monitoring alarms, a grid operator when capacity drops - so a strict
    interval join would miss the vessel that caused it.
    """
    if not faults:
        return detections

    windows = []
    for f in faults:
        try:
            f0 = _parse(f.start) - dt.timedelta(hours=slack_hours)
            f1 = _parse(f.end) + dt.timedelta(hours=slack_hours)
        except ValueError:
            continue
        windows.append((f, f0, f1))

    from .geom import haversine_m

    for d in detections:
        try:
            d0, d1 = _parse(d.start_ts), _parse(d.end_ts)
        except ValueError:
            continue
        hits = []
        for f, f0, f1 in windows:
            if not _overlap(d0, d1, f0, f1):
                continue
            if f.lat is not None and f.lon is not None:
                km = float(haversine_m(d.lat, d.lon, f.lat, f.lon)) / 1000.0
                if km > f.radius_km:
                    continue
                rec = f.to_dict() | {"distance_km": round(km, 1)}
            else:
                rec = f.to_dict()
            hits.append(rec)

        if not hits:
            continue
        d.corroboration = hits

        strong = any(h["kind"] in ("operator_notice", "grid_capacity") for h in hits)
        if d.cable_positional_class == CHARTED:
            d.confidence = CONFIDENCE_HIGH if (strong and d.kind == "anchor_drag") else CONFIDENCE_MODERATE
        else:
            # Display-only geometry: corroboration lifts it off the floor but
            # cannot make the underlying route position trustworthy.
            d.confidence = CONFIDENCE_MODERATE if strong else CONFIDENCE_LOW
        d.evidence["corroboration_note"] = (
            "Confidence reflects an independently observed fault in the same window. "
            "Temporal coincidence is not causation: verify the vessel's track against "
            "the charted route position and the operator's stated fault location before "
            "drawing any conclusion."
        )
    return detections
