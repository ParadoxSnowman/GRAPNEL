"""Finnish Transport Infrastructure Agency AIS via Digitraffic.

Free, no key, no registration, CC BY 4.0 - a materially better licence than the
cable data. Covers Finnish waters including the Gulf of Finland, where a
disproportionate share of the confirmed Baltic incidents occurred.

    /api/ais/v1/locations   current positions, GeoJSON FeatureCollection
    /api/ais/v1/vessels     static and voyage data, JSON array keyed by MMSI

Field names below are taken from Fintraffic's published message schema, not
guessed. Two of them are traps:

  Location timestamps are in SECONDS ("time"), metadata timestamps are in
  MILLISECONDS ("timestamp"). Fintraffic call this out explicitly in their docs
  because it bites everyone. Mixing them puts positions in 1970 or 54000 AD,
  and every duration in the detector silently becomes garbage.

  Dimensions come as the four AIS reference-point offsets refA/refB (bow and
  stern of the GPS antenna) and refC/refD (port and starboard). Length is
  refA+refB, beam is refC+refD. There is no "length" field.

THIS IS A LIVE SOURCE WITH NO HISTORY. One call returns one position per
vessel. Detection needs a track, so a single poll can never produce a
detection - you need several polls before any vessel has two fixes to compare.
Use scripts/bootstrap.py against the DMA archive if you want detections on day
one rather than in a few hours.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import requests

from .base import AISSource, POSITION_COLUMNS, STATIC_COLUMNS, conform, empty_positions, empty_static

log = logging.getLogger(__name__)

BASE = "https://meri.digitraffic.fi/api/ais/v1"

# ITU-R M.1371 navigational status. Worth keeping verbatim: status 1 "at anchor"
# reported while making eight knots is itself a signal.
NAV_STATUS = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught", 5: "Moored",
    6: "Aground", 7: "Engaged in fishing", 8: "Under way sailing",
    9: "Reserved (HSC)", 10: "Reserved (WIG)", 11: "Towing astern",
    12: "Pushing ahead or towing alongside", 13: "Reserved",
    14: "AIS-SART / MOB / EPIRB", 15: "Undefined",
}

SHIP_TYPE = {
    2: "WIG", 3: "Special craft", 4: "High-speed craft", 5: "Special craft",
    6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other",
}

# Second digit of the ITU type carries cargo class for types 7x and 8x.
CARGO_HAZARD = {1: "Category A (hazardous)", 2: "Category B", 3: "Category C", 4: "Category D"}


class DigitrafficSource(AISSource):
    name = "digitraffic"
    terrestrial_only = True

    def __init__(self, session: requests.Session | None = None, timeout: int = 90):
        self.session = session or requests.Session()
        # Fintraffic ask for an identifying header and gzip. Both are in their
        # terms of use; omitting them risks getting the client throttled.
        self.session.headers.update({
            "Accept-Encoding": "gzip",
            "Digitraffic-User": "grapnel/openrepo",
        })
        self.timeout = timeout

    def _get(self, path: str):
        r = self.session.get(f"{BASE}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------- positions

    def positions(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        try:
            payload = self._get("/locations")
        except requests.RequestException as exc:
            log.error("digitraffic /locations failed: %s", exc)
            return empty_positions()

        feats = payload.get("features", []) if isinstance(payload, dict) else payload
        rows, skipped = [], 0
        for feat in feats:
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                skipped += 1
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if bbox:
                minx, miny, maxx, maxy = bbox
                if not (minx <= lon <= maxx and miny <= lat <= maxy):
                    continue

            p = feat.get("properties") or {}
            ts = _timestamp(p)
            if ts is None:
                skipped += 1
                continue

            cog = p.get("cog")
            heading = p.get("heading")
            rows.append({
                "ts": ts,
                "mmsi": feat.get("mmsi") or p.get("mmsi"),
                "lat": lat,
                "lon": lon,
                "sog": _sog(p.get("sog")),
                # 360 and 511 are the AIS "not available" sentinels. Leaving
                # them in makes a stationary vessel look like it is heading
                # north-north-west at all times and wrecks course statistics.
                "cog": None if cog is None or float(cog) >= 360.0 else float(cog),
                "heading": None if heading is None or float(heading) >= 511 else float(heading),
                "nav_status": NAV_STATUS.get(p.get("navStat"), f"Unknown ({p.get('navStat')})"),
                "source": self.name,
            })

        log.info("digitraffic: %d positions in bbox (%d unusable)", len(rows), skipped)
        if not rows:
            return empty_positions()

        df = pd.DataFrame(rows)
        df = df[df["mmsi"].notna() & df["ts"].notna()]
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        if df.empty:
            return empty_positions()
        return conform(df, POSITION_COLUMNS).sort_values(["mmsi", "ts"]).reset_index(drop=True)

    # ---------------------------------------------------------------- static

    def static(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        try:
            payload = self._get("/vessels")
        except requests.RequestException as exc:
            log.error("digitraffic /vessels failed: %s", exc)
            return empty_static()

        items = payload if isinstance(payload, list) else payload.get("features", payload.get("vessels", []))
        rows = []
        for v in items:
            if not isinstance(v, dict):
                continue
            p = v.get("properties", v)
            mmsi = v.get("mmsi") or p.get("mmsi")
            if not mmsi:
                continue

            st = p.get("type")
            imo = p.get("imo")
            draught = p.get("draught")
            rows.append({
                "mmsi": mmsi,
                # imo 0 means "not supplied", which is different from a wrong one.
                "imo": str(imo) if imo not in (None, 0, "0") else None,
                "callsign": _clean(p.get("callSign")),
                "name": _clean(p.get("name")),
                "ship_type": _ship_type(st),
                "cargo_type": _cargo(st),
                "length": _dim(p, "refA", "refB"),
                "width": _dim(p, "refC", "refD"),
                # Reported in decimetres: 68 means 6.8 m.
                "draught": (float(draught) / 10.0) if draught else None,
                "destination": _clean(p.get("destination")),
                "eta": str(p.get("eta")) if p.get("eta") else None,
                "source": self.name,
            })

        log.info("digitraffic: %d static records", len(rows))
        if not rows:
            return empty_static()
        return conform(pd.DataFrame(rows), STATIC_COLUMNS)


# ------------------------------------------------------------------ helpers

def _timestamp(p: dict):
    """Resolve a position timestamp across the three shapes Fintraffic emit.

    'time' is SECONDS; 'timestamp' and 'timestampExternal' are MILLISECONDS.
    Rather than trust the key name alone we sanity-check the magnitude, because
    a silent unit error here corrupts every duration the detectors compute.
    """
    for key, unit in (("timestampExternal", "ms"), ("timestamp", "ms"), ("time", "s")):
        raw = p.get(key)
        if raw in (None, 0):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        # ~2001-09-09 in seconds is 1e9; the same instant in ms is 1e12.
        if val > 1e11:
            unit = "ms"
        elif val > 1e8:
            unit = "s"
        else:
            continue
        return pd.to_datetime(val, unit=unit, utc=True)
    return None


def _sog(v):
    """AIS speed over ground. 102.3 is the 'not available' sentinel."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f >= 102.3 else f


def _ship_type(st):
    try:
        return SHIP_TYPE.get(int(st) // 10, f"Type {int(st)}")
    except (TypeError, ValueError):
        return None


def _cargo(st):
    try:
        st = int(st)
    except (TypeError, ValueError):
        return None
    if st // 10 not in (7, 8):
        return str(st)
    haz = CARGO_HAZARD.get(st % 10)
    return f"{st} - {haz}" if haz else str(st)


def _dim(p, k1, k2):
    """Length or beam from the AIS reference-point offsets."""
    try:
        v = float(p.get(k1) or 0) + float(p.get(k2) or 0)
    except (TypeError, ValueError):
        return None
    return v or None


def _clean(s):
    if s is None:
        return None
    s = str(s).strip().strip("@").strip()
    return s or None
