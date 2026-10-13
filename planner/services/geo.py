"""Geometry helpers (vectorised with numpy)."""
import numpy as np

EARTH_MILES = 3958.8


def haversine_miles(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_MILES * np.arcsin(np.sqrt(a))


def cumulative_miles(points: np.ndarray) -> np.ndarray:
    """points: (n, 2) array of [lat, lng]. Returns miles from the start for every point."""
    if len(points) < 2:
        return np.zeros(len(points))
    seg = haversine_miles(points[:-1, 0], points[:-1, 1], points[1:, 0], points[1:, 1])
    return np.concatenate([[0.0], np.cumsum(seg)])


def downsample(points: np.ndarray, cum: np.ndarray, step_miles: float = 1.0):
    """Keep ~1 point per `step_miles` so nearest-point search stays cheap."""
    targets = np.arange(0, cum[-1], step_miles)
    idx = np.unique(np.concatenate([np.searchsorted(cum, targets), [len(cum) - 1]]))
    idx = np.clip(idx, 0, len(cum) - 1)
    return points[idx], cum[idx]


def nearest_on_route(stations_ll: np.ndarray, route_pts: np.ndarray, route_cum: np.ndarray,
                     max_miles: float, chunk: int = 400):
    """For each station return (distance_to_route, mile_marker). Uses a fast
    equirectangular approximation which is accurate to well under 1% at these scales."""
    lat0 = np.radians(route_pts[:, 0].mean())
    k = np.array([69.0, 69.172 * np.cos(lat0)])
    rp = route_pts * k
    sp = stations_ll * k
    dist = np.empty(len(sp))
    marker = np.empty(len(sp))
    for i in range(0, len(sp), chunk):
        block = sp[i:i + chunk]
        d2 = ((block[:, None, :] - rp[None, :, :]) ** 2).sum(axis=2)
        j = d2.argmin(axis=1)
        dist[i:i + chunk] = np.sqrt(d2[np.arange(len(block)), j])
        marker[i:i + chunk] = route_cum[j]
    return dist, marker
