"""Global AIS via aisstream.io.

The only realistic free route to near-worldwide coverage. Free API key from
https://aisstream.io — register, paste the key into the AISSTREAM_API_KEY
environment variable (or a GitHub Actions secret of the same name).

WHY THIS IS BETTER THAN A SNAPSHOT API. Digitraffic answers "where is everyone
right now" and returns one fix per vessel, which is useless to a detector that
needs a track. aisstream is a *stream*: it pushes every message as it arrives.
Collecting for three minutes yields several fixes for every moving vessel in
range, so short tracks exist from the very first run instead of after hours of
polling. That is the single biggest practical difference between the two.

WHAT "GLOBAL" HONESTLY MEANS. aisstream aggregates a network of terrestrial
receivers. Terrestrial AIS is line-of-sight VHF, so coverage is good near
populated coasts, thin near empty ones, and absent mid-ocean. There is no free
satellite AIS. A vessel that vanishes 300 nm offshore has not gone dark, it has
gone over the horizon, and no amount of code fixes that. The good news for this
project is that cables are most vulnerable in shallow, busy, coastal water,
which is exactly where the coverage is.

Protocol notes that matter:
  - Subscription must be sent within 3 seconds of the socket opening or the
    server closes the connection.
  - Bounding boxes are [[lat, lon], [lat, lon]] corner pairs. LAT FIRST. This
    is the opposite order from GeoJSON and from the rest of this codebase.
  - MetaData.time_utc is Go's time format, e.g.
    "2024-05-20 09:21:31.781972101 +0000 UTC" — nine-digit fractional seconds
    and a trailing literal "UTC" that no standard parser accepts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re

import pandas as pd

from .base import AISSource, POSITION_COLUMNS, STATIC_COLUMNS, conform, empty_positions, empty_static

log = logging.getLogger(__name__)

ENDPOINT = "wss://stream.aisstream.io/v0/stream"

NAV_STATUS = {
    0: "Under way using engine", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught", 5: "Moored",
    6: "Aground", 7: "Engaged in fishing", 8: "Under way sailing",
    11: "Towing astern", 12: "Pushing ahead", 14: "AIS-SART / MOB / EPIRB",
    15: "Undefined",
}
SHIP_TYPE = {2: "WIG", 3: "Special craft", 4: "High-speed craft", 5: "Special craft",
             6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other"}

_GO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*([+-]\d{4})?")


def parse_go_time(s):
    """Parse Go's default time layout, which nothing in the stdlib accepts."""
    if not s:
        return None
    m = _GO_TIME.match(str(s).strip())
    if not m:
        return None
    base, frac, off = m.groups()
    iso = base.replace(" ", "T")
    if frac:
        iso += "." + frac[:6]          # datetime tops out at microseconds
    iso += (off[:3] + ":" + off[3:]) if off else "+00:00"
    try:
        return pd.Timestamp(dt.datetime.fromisoformat(iso)).tz_convert("UTC")
    except (ValueError, TypeError):
        return None


def _num(v, sentinel=None):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (sentinel is not None and f >= sentinel) else f


def _clean(s):
    if s is None:
        return None
    s = str(s).strip().strip("@").strip()
    return s or None


class AISStreamSource(AISSource):
    name = "aisstream"
    terrestrial_only = True   # aggregated shore receivers; no satellite feed

    def __init__(self, api_key: str | None = None, collect_seconds: int = 180,
                 max_messages: int = 200_000):
        self.api_key = api_key or os.environ.get("AISSTREAM_API_KEY", "")
        self.collect_seconds = collect_seconds
        self.max_messages = max_messages

    # ------------------------------------------------------------------ core

    def _collect(self, bbox):
        """Open the socket, drain for collect_seconds, return raw messages."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.error("aisstream needs the 'websockets' package: pip install websockets")
            return [], []
        if not self.api_key:
            log.error(
                "AISSTREAM_API_KEY is empty, so no AIS can be collected.\n"
                "  Local:   export AISSTREAM_API_KEY=your_key\n"
                "  Actions: adding the repository secret is NOT enough - secrets are not\n"
                "           ambient environment variables. The step must map it:\n"
                "               env:\n"
                "                 AISSTREAM_API_KEY: ${{ secrets.AISSTREAM_API_KEY }}\n"
                "  Free key: https://aisstream.io")
            return [], []

        minx, miny, maxx, maxy = bbox
        # Their corners are [lat, lon]. Ours are (lon, lat). Getting this
        # backwards silently subscribes to the wrong half of the planet.
        boxes = [[[miny, minx], [maxy, maxx]]]

        async def run():
            import websockets

            positions, statics, errors = [], [], []
            deadline = asyncio.get_event_loop().time() + self.collect_seconds
            try:
                async with websockets.connect(ENDPOINT, ping_interval=20, close_timeout=5) as ws:
                    # Their published examples disagree on the casing of the
                    # key field - the JSON reference says "APIKey", the
                    # JavaScript sample says "Apikey". Send both; the server
                    # ignores the one it does not recognise, and guessing wrong
                    # costs a silent disconnect three seconds later.
                    await ws.send(json.dumps({
                        "APIKey": self.api_key,
                        "Apikey": self.api_key,
                        "BoundingBoxes": boxes,
                        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                    }))
                    while True:
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0 or len(positions) >= self.max_messages:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
                        except asyncio.TimeoutError:
                            continue
                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # The server reports auth and subscription problems as
                        # plain messages, not as a socket error, so they have to
                        # be read out or they vanish.
                        if isinstance(msg, dict) and ("error" in msg or "Error" in msg):
                            errors.append(str(msg.get("error") or msg.get("Error")))
                            continue
                        kind = msg.get("MessageType")
                        if kind == "PositionReport":
                            positions.append(msg)
                        elif kind == "ShipStaticData":
                            statics.append(msg)
            except Exception as exc:
                # A dropped socket mid-collection still leaves usable data, but
                # an immediate close almost always means the key was rejected.
                elapsed = self.collect_seconds - max(0.0, deadline - asyncio.get_event_loop().time())
                if not positions and elapsed < 10:
                    log.error("aisstream closed the connection after %.1fs with no data: %s: %s. "
                              "That is the signature of a rejected API key. Verify the key at "
                              "https://aisstream.io and, in GitHub Actions, that the workflow step "
                              "maps it: env: AISSTREAM_API_KEY: ${{ secrets.AISSTREAM_API_KEY }}",
                              elapsed, type(exc).__name__, exc)
                else:
                    log.warning("aisstream stream ended early after %.1fs with %d positions: %s: %s",
                                elapsed, len(positions), type(exc).__name__, exc)
            if errors:
                for e in dict.fromkeys(errors[:5]):
                    log.error("aisstream server error: %s", e)
            return positions, statics

        try:
            return asyncio.run(run())
        except RuntimeError:
            # Already inside a loop (notebook, some CI harnesses).
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(run())
            finally:
                loop.close()

    # ------------------------------------------------------------------- api

    def positions(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        bbox = bbox or (-180.0, -90.0, 180.0, 90.0)
        log.info("aisstream: collecting for %ds over %s", self.collect_seconds, bbox)
        raw_pos, raw_static = self._collect(bbox)
        self._static_cache = raw_static

        rows = []
        for m in raw_pos:
            pr = (m.get("Message") or {}).get("PositionReport") or {}
            md = m.get("MetaData") or {}
            lat = pr.get("Latitude", md.get("latitude"))
            lon = pr.get("Longitude", md.get("longitude"))
            mmsi = pr.get("UserID") or md.get("MMSI")
            if lat is None or lon is None or not mmsi:
                continue
            if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
                continue
            rows.append({
                "ts": parse_go_time(md.get("time_utc")) or pd.Timestamp.now(tz="UTC"),
                "mmsi": mmsi,
                "lat": float(lat),
                "lon": float(lon),
                "sog": _num(pr.get("Sog"), sentinel=102.3),
                "cog": _num(pr.get("Cog"), sentinel=360.0),
                "heading": _num(pr.get("TrueHeading"), sentinel=511.0),
                "nav_status": NAV_STATUS.get(pr.get("NavigationalStatus"),
                                             f"Unknown ({pr.get('NavigationalStatus')})"),
                "source": self.name,
            })

        if not rows:
            log.error("aisstream returned no usable positions from %d raw messages. "
                      "If raw messages is also 0, the connection was rejected or the "
                      "bounding box covers no covered water.", len(raw_pos))
            return empty_positions()

        df = pd.DataFrame(rows).drop_duplicates(subset=["mmsi", "ts"])
        n = df["mmsi"].nunique()
        log.info("aisstream: %d positions, %d vessels (%.1f fixes each)", len(df), n, len(df) / n)
        return conform(df, POSITION_COLUMNS).sort_values(["mmsi", "ts"]).reset_index(drop=True)

    def static(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        raw = getattr(self, "_static_cache", None)
        if not raw:
            return empty_static()

        rows = []
        for m in raw:
            sd = (m.get("Message") or {}).get("ShipStaticData") or {}
            md = m.get("MetaData") or {}
            mmsi = sd.get("UserID") or md.get("MMSI")
            if not mmsi:
                continue
            dim = sd.get("Dimension") or {}
            st = sd.get("Type")
            imo = sd.get("ImoNumber")
            eta = sd.get("Eta") or {}
            rows.append({
                "mmsi": mmsi,
                "imo": str(imo) if imo not in (None, 0, "0") else None,
                "callsign": _clean(sd.get("CallSign")),
                "name": _clean(sd.get("Name") or md.get("ShipName")),
                "ship_type": SHIP_TYPE.get(int(st) // 10, f"Type {st}") if st else None,
                "cargo_type": str(st) if st else None,
                "length": (dim.get("A") or 0) + (dim.get("B") or 0) or None,
                "width": (dim.get("C") or 0) + (dim.get("D") or 0) or None,
                "draught": _num(sd.get("MaximumStaticDraught")),
                "destination": _clean(sd.get("Destination")),
                "eta": (f"{eta.get('Month')}-{eta.get('Day')} {eta.get('Hour')}:{eta.get('Minute')}"
                        if eta.get("Month") else None),
                "source": self.name,
            })

        if not rows:
            return empty_static()
        df = pd.DataFrame(rows).groupby("mmsi", as_index=False).last()
        log.info("aisstream: %d static records", len(df))
        return conform(df, STATIC_COLUMNS)
