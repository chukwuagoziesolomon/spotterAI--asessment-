"""Thin wrappers around the free APIs (OSRM for routing, Nominatim for geocoding).

Call budget per request:  geocode(start) + geocode(finish) + ONE OSRM route call = 3 calls max.
If the client passes "lat,lng" for either end, that geocode call is skipped (down to 1 call).
Everything is cached, so repeating a request costs 0 external calls.
"""
import hashlib
import re

import requests
from django.conf import settings
from django.core.cache import cache

COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class RoutingError(Exception):
    pass


def _key(prefix, value):
    return prefix + hashlib.md5(value.lower().encode()).hexdigest()


def in_usa(lat, lng):
    # Contiguous US bounding box (OSRM public data has no ferry/overseas roads anyway)
    return 24.0 <= lat <= 50.0 and -125.5 <= lng <= -66.5


def geocode(query: str) -> dict:
    m = COORD_RE.match(query)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        label = f"{lat:.5f}, {lng:.5f}"
    else:
        key = _key("geo:", query)
        hit = cache.get(key)
        if hit:
            return hit
        try:
            r = requests.get(
                f"{settings.NOMINATIM_URL}/search",
                params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": settings.GEO_USER_AGENT},
                timeout=settings.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise RoutingError(f"Geocoding service unavailable: {e}")
        if not data:
            raise RoutingError(f"Could not find a USA location for '{query}'")
        lat, lng, label = float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"]
    if not in_usa(lat, lng):
        raise RoutingError(f"'{query}' is outside the supported USA area")
    out = {"query": query, "lat": lat, "lng": lng, "label": label}
    if not m:
        cache.set(_key("geo:", query), out, settings.CACHE_TTL)
    return out


def get_route(start: dict, finish: dict) -> dict:
    """ONE OSRM call returning the full route geometry as GeoJSON coords [lng, lat]."""
    key = _key("route:", f"{start['lat']},{start['lng']}|{finish['lat']},{finish['lng']}")
    hit = cache.get(key)
    if hit:
        return hit
    coords = f"{start['lng']},{start['lat']};{finish['lng']},{finish['lat']}"
    try:
        r = requests.get(
            f"{settings.OSRM_URL}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=settings.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise RoutingError(f"Routing service unavailable: {e}")
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"No drivable route found ({data.get('code')})")
    route = data["routes"][0]
    out = {
        "coords": route["geometry"]["coordinates"],
        "distance_miles": route["distance"] / 1609.344,
        "duration_minutes": route["duration"] / 60,
    }
    cache.set(key, out, settings.CACHE_TTL)
    return out
