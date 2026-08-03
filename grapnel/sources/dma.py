"""Danish Maritime Authority historical AIS.

Free daily dumps at https://web.ais.dk/aisdata/ published under the Danish PSI
act, going back to 2006. This is the backbone for retrospective Baltic work: no
key, no rate limit, and no terms forbidding redistribution of derived findings.

Cost of admission is size. One day is 1.5-2.6 GB uncompressed, roughly ten
million rows. We stream it in chunks and filter to the area of interest on the
way past, so a whole day never lands in memory.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
import requests

from .base import AISSource, POSITION_COLUMNS, STATIC_COLUMNS, conform, empty_positions, empty_static

log = logging.getLogger(__name__)

BASE_URL = "https://web.ais.dk/aisdata"
CHUNK_ROWS = 500_000

COLMAP = {
    "# Timestamp": "ts", "Timestamp": "ts", "MMSI": "mmsi",
    "Latitude": "lat", "Longitude": "lon", "SOG": "sog", "COG": "cog",
    "Heading": "heading", "Navigational status": "nav_status", "IMO": "imo",
    "Callsign": "callsign", "Name": "name", "Ship type": "ship_type",
    "Cargo type": "cargo_type", "Width": "width", "Length": "length",
    "Draught": "draught", "Destination": "destination", "ETA": "eta",
    "Type of mobile": "mobile_type",
}
USECOLS = set(COLMAP)


class DMASource(AISSource):
    name = "dma"
    terrestrial_only = True

    def __init__(self, cache_dir: Path, keep_downloads: bool = True):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.keep_downloads = keep_downloads
        self._static_buffer: list[pd.DataFrame] = []

    def _day_zip(self, day: dt.date) -> Path | None:
        fn = f"aisdk-{day:%Y-%m-%d}.zip"
        dest = self.cache_dir / fn
        if dest.exists() and dest.stat().st_size > 1024:
            return dest
        url = f"{BASE_URL}/{fn}"
        log.info("downloading %s", url)
        try:
            with requests.get(url, stream=True, timeout=900) as r:
                if r.status_code == 404:
                    log.warning("no DMA file for %s (gap, or not yet published)", day)
                    return None
                r.raise_for_status()
                tmp = dest.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for block in r.iter_content(1 << 20):
                        fh.write(block)
                tmp.rename(dest)
        except requests.RequestException as exc:
            log.error("DMA download failed for %s: %s", day, exc)
            return None
        return dest

    def _iter_day(self, day: dt.date, bbox):
        path = self._day_zip(day)
        if path is None:
            return
        minx, miny, maxx, maxy = bbox
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not members:
                log.error("no CSV inside %s", path)
                return
            with zf.open(members[0]) as raw:
                stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                for chunk in pd.read_csv(stream, chunksize=CHUNK_ROWS,
                                         usecols=lambda c: c in USECOLS,
                                         dtype=str, low_memory=False):
                    chunk = chunk.rename(columns=COLMAP)
                    if "lat" not in chunk or "lon" not in chunk:
                        continue
                    lat = pd.to_numeric(chunk["lat"], errors="coerce")
                    lon = pd.to_numeric(chunk["lon"], errors="coerce")
                    keep = lat.between(miny, maxy) & lon.between(minx, maxx)
                    # Base stations and aids to navigation report positions but
                    # are not vessels.
                    if "mobile_type" in chunk:
                        keep &= chunk["mobile_type"].fillna("").str.startswith("Class")
                    chunk = chunk[keep]
                    if chunk.empty:
                        continue
                    chunk = chunk.assign(lat=lat[keep], lon=lon[keep])
                    yield chunk

    def positions(self, start: dt.datetime, end: dt.datetime, bbox) -> pd.DataFrame:
        frames = []
        self._static_buffer = []
        day = start.date()
        while day <= end.date():
            for chunk in self._iter_day(day, bbox):
                chunk = chunk.assign(
                    ts=pd.to_datetime(chunk["ts"], format="%d/%m/%Y %H:%M:%S", errors="coerce", utc=True)
                )
                chunk = chunk[chunk["ts"].between(start, end)]
                if chunk.empty:
                    continue
                mmsi = pd.to_numeric(chunk["mmsi"], errors="coerce")
                chunk = chunk[mmsi.notna()].assign(mmsi=mmsi[mmsi.notna()].astype("int64"))
                for c in ("sog", "cog", "heading", "length", "width", "draught"):
                    if c in chunk:
                        chunk[c] = pd.to_numeric(chunk[c], errors="coerce")
                chunk["source"] = self.name
                self._static_buffer.append(conform(chunk, STATIC_COLUMNS))
                frames.append(conform(chunk, POSITION_COLUMNS))
            day += dt.timedelta(days=1)

        if not frames:
            return empty_positions()
        return pd.concat(frames, ignore_index=True).sort_values(["mmsi", "ts"], kind="stable").reset_index(drop=True)

    def static(self, start=None, end=None, bbox=None) -> pd.DataFrame:
        """Static rows harvested during the last positions() call.

        DMA repeats voyage fields on every position row. We take the LAST
        non-null value per MMSI, not the first: destination and draught change
        mid-voyage, and the later value is the one relevant to a detection that
        happened at the end of a transit.
        """
        if not self._static_buffer:
            return empty_static()
        allrows = pd.concat(self._static_buffer, ignore_index=True)
        allrows = allrows.replace({"": pd.NA, "Unknown": pd.NA, "Undefined": pd.NA})
        return conform(allrows.groupby("mmsi", as_index=False).last(), STATIC_COLUMNS)
