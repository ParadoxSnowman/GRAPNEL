"""Canonical AIS schema.

Every source normalises into this frame so detection never has to care where a
position came from. Columns stay close to the ITU-R M.1371 message fields,
because an analyst pivoting off a detection expects to see the raw self-reported
values, not a prettified summary.
"""

from __future__ import annotations

import abc
import datetime as dt

import pandas as pd

POSITION_COLUMNS = {
    "ts": "datetime64[ns, UTC]",
    "mmsi": "int64",
    "lat": "float64",
    "lon": "float64",
    "sog": "float64",      # knots
    "cog": "float64",      # degrees true; 360 = not available
    "heading": "float64",  # degrees true; 511 = not available
    "nav_status": "string",
    "source": "string",
}

STATIC_COLUMNS = {
    "mmsi": "int64",
    "imo": "string",
    "callsign": "string",
    "name": "string",
    "ship_type": "string",
    "cargo_type": "string",
    "length": "float64",
    "width": "float64",
    "draught": "float64",
    "destination": "string",
    "eta": "string",
    "source": "string",
}


def empty_positions() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in POSITION_COLUMNS.items()})


def empty_static() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in STATIC_COLUMNS.items()})


def conform(df: pd.DataFrame, columns: dict) -> pd.DataFrame:
    """Coerce a frame to the canonical column set and dtypes, dropping extras."""
    out = pd.DataFrame(index=df.index)
    for col, dtype in columns.items():
        s = df[col] if col in df.columns else pd.Series([pd.NA] * len(df), index=df.index)
        try:
            out[col] = s.astype(dtype)
        except (TypeError, ValueError):
            if dtype.startswith(("float", "int")):
                num = pd.to_numeric(s, errors="coerce")
                out[col] = num.fillna(0).astype("int64") if dtype == "int64" else num.astype("float64")
            else:
                out[col] = s.astype("string")
    return out


class AISSource(abc.ABC):
    """A source of AIS positions for a bounding box and time window."""

    name: str = "base"
    #: True if coverage is limited to terrestrial VHF range (~40-70 nm from a
    #: receiver). Matters because absence of a track in a coastal feed is not
    #: evidence that a transponder was switched off.
    terrestrial_only: bool = True

    @abc.abstractmethod
    def positions(self, start: dt.datetime, end: dt.datetime, bbox) -> pd.DataFrame:
        """Return canonical position rows. May be empty."""

    def static(self, start: dt.datetime, end: dt.datetime, bbox) -> pd.DataFrame:
        """Return canonical static/voyage rows. Default: none."""
        return empty_static()
