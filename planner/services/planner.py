import hashlib

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max

from ..models import FuelStation
from . import geo, optimizer, providers

_cache = {}


def station_version():
    """Changes whenever stations are re-imported (load_stations wipes + recreates rows -> new max id)."""
    agg = FuelStation.objects.aggregate(n=Count("id"), m=Max("id"))
    return f"{agg['n']}-{agg['m']}"


def _station_arrays():
    """Load all stations once into numpy arrays; reload only if the data version changed."""
    version = station_version()
    if _cache.get("version") != version:
        rows = list(FuelStation.objects.values(
            "id", "name", "address", "city", "state", "price", "lat", "lng"))
        _cache.update(version=version, rows=rows,
                      ll=np.array([[r["lat"], r["lng"]] for r in rows]).reshape(-1, 2))
    return _cache["rows"], _cache["ll"]


def build_plan(start_q: str, finish_q: str, corridor=None, range_miles=None, mpg=None):
    corridor = corridor or settings.CORRIDOR_MILES
    range_miles = range_miles or settings.VEHICLE_RANGE_MILES
    mpg = mpg or settings.VEHICLE_MPG

    start, finish = providers.geocode(start_q), providers.geocode(finish_q)

    # Whole-plan cache: same endpoints + same station data + same settings => skip routing AND optimising.
    raw = (f"{start['lat']:.4f},{start['lng']:.4f}|{finish['lat']:.4f},{finish['lng']:.4f}|"
           f"{station_version()}|{corridor}|{range_miles}|{mpg}")
    key = "plan:" + hashlib.md5(raw.encode()).hexdigest()
    plan = cache.get(key)
    if plan is None:
        plan = _compute_plan(start, finish, corridor, range_miles, mpg)
        cache.set(key, plan, settings.CACHE_TTL)
    return plan


def _compute_plan(start, finish, corridor, range_miles, mpg):
    route = providers.get_route(start, finish)

    pts = np.array([[lat, lng] for lng, lat in route["coords"]])  # -> [lat, lng]
    cum = geo.cumulative_miles(pts)
    cum *= route["distance_miles"] / cum[-1] if cum[-1] else 1.0  # align with OSRM road distance
    total = float(route["distance_miles"])
    dpts, dcum = geo.downsample(pts, cum, 1.0)

    rows, ll = _station_arrays()
    candidates = []
    if len(rows):
        pad = corridor / 60.0  # cheap bounding-box pre-filter (degrees)
        box = ((ll[:, 0] >= pts[:, 0].min() - pad) & (ll[:, 0] <= pts[:, 0].max() + pad) &
               (ll[:, 1] >= pts[:, 1].min() - pad * 1.5) & (ll[:, 1] <= pts[:, 1].max() + pad * 1.5))
        idx = np.flatnonzero(box)
        if len(idx):
            dist, marker = geo.nearest_on_route(ll[idx], dpts, dcum, corridor)
            for k in np.flatnonzero(dist <= corridor):
                r = rows[idx[k]]
                candidates.append({**r, "price": float(r["price"]), "mile": float(marker[k]),
                                   "off_route_miles": float(dist[k])})

    stops = optimizer.plan_fuel_stops(candidates, total, range_miles, mpg)
    total_cost = sum(s.cost for s in stops)
    total_gallons = sum(s.gallons for s in stops)

    stop_json = [{
        "name": s.station["name"], "address": s.station["address"],
        "city": s.station["city"], "state": s.station["state"],
        "lat": s.station["lat"], "lng": s.station["lng"],
        "price_per_gallon": round(s.station["price"], 3),
        "mile_marker": round(s.station["mile"], 1),
        "off_route_miles": round(s.station["off_route_miles"], 1),
        "gallons_purchased": round(s.gallons, 2),
        "cost": round(s.cost, 2),
    } for s in stops]

    return {
        "start": start, "finish": finish,
        "distance_miles": round(total, 1),
        "duration_minutes": round(route["duration_minutes"]),
        "fuel_stops": stop_json,
        "total_fuel_gallons_purchased": round(total_gallons, 2),
        "total_fuel_cost": round(total_cost, 2),
        "assumptions": {
            "vehicle_range_miles": range_miles, "mpg": mpg,
            "starts_with_full_tank_unbilled": True,
            "station_corridor_miles": corridor,
            "trip_fuel_gallons": round(total / mpg, 2),
        },
        "route": {"type": "Feature", "properties": {},
                  "geometry": {"type": "LineString", "coordinates": route["coords"]}},
    }
