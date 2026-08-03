"""Finnish Transport Infrastructure Agency AIS via Digitraffic.

Free, no key, no registration, licensed CC BY 4.0 - a materially better licence
than the cable data. Covers Finnish waters including the Gulf of Finland, where
a disproportionate share of the confirmed Baltic incidents occurred.

    /api/ais/v1/locations   current positions, GeoJSON
    /api/ais/v1/vessels     static and voyage data keyed by MMSI

This is a LIVE source with no history: you get what is on the wire now. Running
the poller on a schedule and accumulating to Parquet is how you build an
archive. Anything before you started polling has to come from DMA or another
historical source.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import requests

from .base import AISSource, POSITION_COLUMNS, STATIC_COLUMNS, conform, empty_positions, empty_static

log = logging.getLogger(__name__)

BASE = "https://meri.digitraffic.fi/api/ais/v1"

# ITU-R M.1371 navigational status. Worth keeping: status 1 "at anchor"
# reported while making 8 knots is itself a signal.
NAV_STATUS = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught", 5: "Moored",
    6: "Aground", 7: "Engaged in fishing", 8: "Under way sailing",
    11: "Towing astern", 12: "Pushing ahead", 14: "AIS-SART / MOB / EPIRB",
    15: "Undefined",
}

SHIP_TYPE = {
    2: "WIG", 3: "Special craft", 4: "High-speed craft", 5: "Special craft",
    6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other",
}


class DigitrafficSource(AISSource):
    name = "digitraffic"
    terrestrial_only = True

    def __init__(self, session: requests.Session | None = None, timeout: int = 60):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept-Encoding": "gzip", "Digitraffic-User": "grapnel/0.1"})
        self.timeout = timeout

    def _get(self, path: str):
        r = self.session.get(f"{BASE}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def positions(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        try:
            payload = self._get("/locations")
        except requests.RequestException as exc:
            log.error("digitraffic locations failed: %s", exc)
            return empty_positions()

        rows = []
        for feat in payload.get("features", []):
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if bbox:
                minx, miny, maxx, maxy = bbox
                if not (minx <= lon <= maxx and miny <= lat <= maxy):
                    continue
            p = feat.get("properties") or {}
            ms = p.get("timestampExternal") or p.get("timestamp")
            rows.append({
                "ts": pd.to_datetime(ms, unit="ms", utc=True) if ms else pd.NaT,
                "mmsi": feat.get("mmsi") or p.get("mmsi"),
                "lat": lat,
                "lon": lon,
                "sog": p.get("sog"),
                "cog": p.get("cog"),
                "heading": p.get("heading"),
                "nav_status": NAV_STATUS.get(p.get("navStat"), str(p.get("navStat"))),
                "source": self.name,
            })
        if not rows:
            return empty_positions()
        df = pd.DataFrame(rows)
        df = df[df["mmsi"].notna() & df["ts"].notna()]
        if start is not None:
            df = df[df["ts"] >= start]
        if end is not None:
            df = df[df["ts"] <= end]
        if df.empty:
            return empty_positions()
        return conform(df, POSITION_COLUMNS).sort_values(["mmsi", "ts"]).reset_index(drop=True)

    def static(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        try:
            payload = self._get("/vessels")
        except requests.RequestException as exc:
            log.error("digitraffic vessels failed: %s", exc)
            return empty_static()

        items = payload if isinstance(payload, list) else payload.get("features", [])
        rows = []
        for v in items:
            if not isinstance(v, dict):
                continue
            p = v.get("properties", v)
            mmsi = v.get("mmsi") or p.get("mmsi")
            if not mmsi:
                continue
            st = p.get("shipType")
            draught = p.get("draught")
            rows.append({
                "mmsi": mmsi,
                "imo": str(p.get("imo") or "") or None,
                "callsign": (p.get("callSign") or "").strip() or None,
                "name": (p.get("name") or "").strip() or None,
                "ship_type": SHIP_TYPE.get(int(st) // 10, str(st)) if st else None,
                "cargo_type": str(st) if st else None,
                "length": _dims(p, "referencePointA", "referencePointB"),
                "width": _dims(p, "referencePointC", "referencePointD"),
                # Digitraffic reports draught in decimetres.
                "draught": (float(draught) / 10.0) if draught else None,
                "destination": (p.get("destination") or "").strip() or None,
                "eta": str(p.get("eta") or "") or None,
                "source": self.name,
            })
        if not rows:
            return empty_static()
        return conform(pd.DataFrame(rows), STATIC_COLUMNS)


def _dims(p, k1, k2):
    """Overall length or beam from the AIS reference-point offsets."""
    a, b = p.get(k1, p.get(k1[-1].lower())), p.get(k2, p.get(k2[-1].lower()))
    try:
        v = float(a) + float(b)
        return v or None
    except (TypeError, ValueError):
        return None
