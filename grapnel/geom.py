"""Geodesic helpers.

WGS84 degrees on the outside, metres on the inside. We deliberately avoid a
single global projected CRS: cable corridors sit at high latitude (the Gulf of
Finland is at 60N) where a naive degree-based buffer is wrong by a factor of two
in longitude.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import transform

EARTH_R = 6_371_008.8  # mean radius, metres


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Vectorised; broadcasts."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype="float64")) for x in (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def initial_bearing(lat1, lon1, lat2, lon2):
    """Forward azimuth in degrees, 0-360. Vectorised."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype="float64")) for x in (lat1, lon1, lat2, lon2))
    dlon = lon2 - lon1
    y = np.sin(dlon) * np.cos(lat2)
    x = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def angular_diff(a, b):
    """Smallest absolute difference between two bearings, 0-180 degrees."""
    d = np.abs(np.asarray(a, dtype="float64") - np.asarray(b, dtype="float64")) % 360.0
    return np.where(d > 180.0, 360.0 - d, d)


def local_metric_crs(lat: float, lon: float) -> str:
    """Azimuthal equidistant CRS centred on the geometry.

    Buffering in this frame gives a true metre buffer at any latitude. Cheaper
    and less lossy than picking a UTM zone for a corridor that crosses several.
    """
    return f"+proj=aeqd +lat_0={lat:.6f} +lon_0={lon:.6f} +units=m +ellps=WGS84 +no_defs"


def buffer_line_metres(line: LineString, metres: float):
    """Buffer a lon/lat LineString by a true metre distance; return lon/lat."""
    cx, cy = line.centroid.x, line.centroid.y
    crs = local_metric_crs(cy, cx)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return transform(inv, transform(fwd, line).buffer(metres, quad_segs=8))


def densify(line: LineString, max_seg_m: float = 2000.0) -> LineString:
    """Insert vertices so no segment exceeds max_seg_m.

    Cable GeoJSON is drawn for display and routinely has 50-200 km straight
    legs. Nearest-vertex distance on such a line is meaningless; densifying
    makes point-to-line distance well behaved without a projected CRS.
    """
    coords = list(line.coords)
    if len(coords) < 2:
        return line
    out = [coords[0]]
    for (x1, y1), (x2, y2) in zip(coords[:-1], coords[1:]):
        seg = float(haversine_m(y1, x1, y2, x2))
        n = max(1, int(math.ceil(seg / max_seg_m)))
        for i in range(1, n + 1):
            f = i / n
            out.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
    return LineString(out)


@dataclass
class TrackStats:
    """Summary of a contiguous run of AIS positions for one vessel."""

    n: int
    duration_s: float
    distance_m: float
    mean_sog: float
    median_sog: float
    max_sog: float
    cog_stability: float  # 0-1; 1 = perfectly straight course
    mean_lat: float
    mean_lon: float


def track_stats(ts, lat, lon, sog, cog) -> TrackStats:
    lat = np.asarray(lat, dtype="float64")
    lon = np.asarray(lon, dtype="float64")
    sog = np.asarray(sog, dtype="float64")
    cog = np.asarray(cog, dtype="float64")

    dist = float(np.nansum(haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:]))) if len(lat) > 1 else 0.0

    # Circular resultant length of COG. Ground tackle in the seabed acts as a
    # drogue: it damps yaw and holds the hull on a line. A vessel manoeuvring,
    # drifting or fishing at the same speed wanders; one dragging does not.
    valid = cog[np.isfinite(cog) & (cog < 360.0)]
    if len(valid) >= 3:
        rad = np.radians(valid)
        stability = float(min(1.0, math.hypot(float(np.mean(np.cos(rad))), float(np.mean(np.sin(rad))))))
    else:
        stability = float("nan")

    try:
        dur = float((ts[-1] - ts[0]).total_seconds())
    except (AttributeError, TypeError, IndexError):
        dur = 0.0

    finite = sog[np.isfinite(sog)]
    return TrackStats(
        n=len(lat),
        duration_s=dur,
        distance_m=dist,
        mean_sog=float(np.mean(finite)) if len(finite) else float("nan"),
        median_sog=float(np.median(finite)) if len(finite) else float("nan"),
        max_sog=float(np.max(finite)) if len(finite) else float("nan"),
        cog_stability=stability,
        mean_lat=float(np.mean(lat)),
        mean_lon=float(np.mean(lon)),
    )


def point_in_any(polys, lat, lon) -> bool:
    p = Point(lon, lat)
    return any(poly.contains(p) for poly in polys)
