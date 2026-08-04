"""Global Fishing Watch Events API — the biggest free coverage upgrade available.

Free token from https://globalfishingwatch.org/our-apis/ (register, agree to the
terms, attribute them in anything you publish). Set GFW_API_TOKEN.

WHY THIS MATTERS MORE THAN ANOTHER POSITION FEED. Every terrestrial AIS source
in this project shares one ceiling: line-of-sight VHF, so 40-70 nm from a
receiver and nothing beyond. GFW fuses terrestrial with SATELLITE AIS and
publishes derived events computed over the whole ocean. Two of those events are
exactly what this project needs and cannot otherwise get:

  GAP — AIS-disabling. GFW work out when a transponder went off rather than out
  of range, which is the distinction our own corridor_gap detector fundamentally
  cannot make from a coastal feed. A gap in Digitraffic might be a vessel over
  the horizon; a GFW gap event is an assessment that the transmitter stopped.

  LOITERING — sub-2-knot behaviour, computed globally, with no archive of your
  own required. Available from the first run instead of after hours of polling.

These arrive as events, not positions, so they do not feed the behavioural
detectors. They are joined to cable corridors here and emitted as detections in
their own right, tagged with GFW as the source so provenance is never ambiguous.

TWO HONEST LIMITS.

  GFW's loitering definition requires the vessel to be at least ~20 nm from
  shore on average, because the metric was built for fishing transshipment.
  Cables are most vulnerable in shallow coastal water — inside that exclusion.
  So GFW loitering complements the local detectors offshore and does not
  replace them inshore.

  Events lag roughly 72 hours. This is a forensic source, not a tripwire.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import requests

from ..detect import CONFIDENCE_LOW, Detection
from ..geom import haversine_m

log = logging.getLogger(__name__)

BASE = "https://gateway.api.globalfishingwatch.org/v3"

EVENT_KINDS = {
    "GAP": "gfw_ais_off",
    "LOITERING": "gfw_loitering",
    "ENCOUNTER": "gfw_encounter",
}


class GFWEvents:
    def __init__(self, token: str | None = None, timeout: int = 90):
        self.token = token or os.environ.get("GFW_API_TOKEN", "")
        self.timeout = timeout

    def _get(self, path, params):
        r = requests.get(f"{BASE}{path}", params=params, timeout=self.timeout,
                         headers={"Authorization": f"Bearer {self.token}",
                                  "Accept": "application/json"})
        if r.status_code == 401:
            raise PermissionError("GFW token rejected (401). Check GFW_API_TOKEN.")
        r.raise_for_status()
        return r.json()

    def fetch(self, start: dt.datetime, end: dt.datetime, bbox,
              kinds=("GAP", "LOITERING"), limit: int = 1000) -> list[dict]:
        if not self.token:
            log.error(
                "GFW_API_TOKEN is empty, so no Global Fishing Watch events will be fetched.\n"
                "  Free token: https://globalfishingwatch.org/our-apis/\n"
                "  Actions:    map it into the step, secrets are not ambient:\n"
                "                env:\n"
                "                  GFW_API_TOKEN: ${{ secrets.GFW_API_TOKEN }}")
            return []

        minx, miny, maxx, maxy = bbox
        geometry = {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }
        out = []
        for kind in kinds:
            try:
                payload = self._get("/events", {
                    "datasets[0]": "public-global-gaps-events:latest" if kind == "GAP"
                                   else "public-global-loitering-events:latest",
                    "start-date": start.strftime("%Y-%m-%d"),
                    "end-date": end.strftime("%Y-%m-%d"),
                    "geometry": __import__("json").dumps(geometry),
                    "limit": limit,
                    "offset": 0,
                })
            except (requests.RequestException, PermissionError) as exc:
                log.error("GFW %s fetch failed: %s", kind, exc)
                continue
            entries = payload.get("entries", payload.get("data", []))
            log.info("GFW %s: %d events", kind, len(entries))
            for e in entries:
                e["_kind"] = kind
                out.append(e)
        return out


def to_detections(events: list[dict], routes, index, corridor_m: float) -> list[Detection]:
    """Keep only events that fall near a cable corridor, as detections."""
    made = []
    for e in events:
        pos = e.get("position") or {}
        lat, lon = pos.get("lat"), pos.get("lon")
        if lat is None or lon is None:
            continue
        ridx, dist = index.nearest(float(lat), float(lon))
        if ridx is None or dist is None or dist > corridor_m:
            continue

        route = routes[ridx]
        kind = EVENT_KINDS.get(e.get("_kind"), "gfw_event")
        vessel = (e.get("vessel") or {})
        mmsi = vessel.get("ssvid") or vessel.get("mmsi") or 0
        try:
            mmsi = int(str(mmsi)[:9] or 0)
        except (TypeError, ValueError):
            mmsi = 0

        start = str(e.get("start") or "")[:19].replace(" ", "T") + "Z"
        end = str(e.get("end") or e.get("start") or "")[:19].replace(" ", "T") + "Z"
        hours = (e.get("gap") or {}).get("durationHours") or e.get("durationHours")

        made.append(Detection(
            detection_id="", kind=kind, mmsi=mmsi,
            cable_id=route.cable_id, cable_name=route.name,
            cable_positional_class=route.positional_class, cable_source=route.source,
            start_ts=start, end_ts=end,
            duration_s=float(hours) * 3600 if hours else 0.0,
            lat=round(float(lat), 5), lon=round(float(lon), 5),
            confidence=CONFIDENCE_LOW,
            score=round(min(1.0, (float(hours) / 24.0) if hours else 0.3), 3),
            summary=(f"Global Fishing Watch assessed an AIS-disabling event lasting "
                     f"{float(hours):.1f} h within {dist/1000:.1f} km of the corridor."
                     if kind == "gfw_ais_off" and hours else
                     f"Global Fishing Watch {e.get('_kind','').lower()} event "
                     f"{dist/1000:.1f} km from the corridor."),
            evidence={
                "source": "Global Fishing Watch Events API v3",
                "event_type": e.get("_kind"),
                "event_id": e.get("id"),
                "distance_to_route_m": int(dist),
                "vessel_name": vessel.get("name"),
                "vessel_flag": vessel.get("flag"),
                "attribution_required": "Global Fishing Watch",
                "why_this_beats_our_own_gap_detector": (
                    "GFW fuse satellite with terrestrial AIS, so they can distinguish a "
                    "transponder being switched off from a vessel sailing out of coastal "
                    "receiver range. Our corridor_gap detector cannot make that distinction "
                    "from a shore feed and never claims to."),
                "caveat": (
                    "Events lag roughly 72 hours, so this is forensic rather than a tripwire. "
                    "GFW loitering additionally requires ~20 nm from shore, which excludes the "
                    "shallow coastal water where cables are most exposed."),
            },
        ))
    log.info("GFW: %d of %d events fell near a cable corridor", len(made), len(events))
    return made
