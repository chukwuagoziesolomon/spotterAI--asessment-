"""Cheapest-refuelling planner.

Classic "gas station problem" with continuous fuel purchase, solved greedily (provably optimal):
  * at a station, if a CHEAPER station is reachable on a full tank -> buy only enough to reach it
  * otherwise -> fill the tank (everything ahead in range costs the same or more),
    then drive to the cheapest station in range
  * if the destination is reachable -> buy only what is needed to arrive
The vehicle starts with `initial_miles` of fuel (default: a full tank) which is not billed.
"""
from dataclasses import dataclass


class NoFeasibleRoute(Exception):
    pass


@dataclass
class Stop:
    station: dict
    gallons: float
    cost: float


def plan_fuel_stops(stations, total_miles, range_miles=500.0, mpg=10.0, initial_miles=None):
    """stations: list of dicts with at least 'mile' (distance along route) and 'price' ($/gal)."""
    eps = 1e-6
    if total_miles <= eps:  # start == finish: nothing to drive
        return []
    dest = {"mile": total_miles, "price": 0.0, "_dest": True}  # destination = a free "station" at the end
    stations = sorted(stations, key=lambda s: s["mile"]) + [dest]
    fuel = range_miles if initial_miles is None else min(initial_miles, range_miles)
    pos, cur, stops = 0.0, None, []

    def buy(station, miles):
        if station is not None and miles > eps:
            gallons = miles / mpg
            stops.append(Stop(station, gallons, gallons * station["price"]))

    while cur is not dest:
        reach = pos + (range_miles if cur else fuel)
        ahead = [s for s in stations if pos + eps < s["mile"] <= reach + eps]
        if not ahead:
            raise NoFeasibleRoute(
                f"No fuel station within range after mile {pos:.0f}; cannot complete the trip")
        if cur:
            cheaper = next((s for s in ahead if s["price"] < cur["price"]), None)
            if cheaper:
                target, need = cheaper, max(0.0, (cheaper["mile"] - pos) - fuel)
            else:
                target = min(ahead, key=lambda s: (s["price"], -s["mile"]))
                need = range_miles - fuel
        else:
            target, need = min(ahead, key=lambda s: (s["price"], -s["mile"])), 0.0
        buy(cur, need)
        fuel = max(0.0, fuel + need - (target["mile"] - pos))
        pos, cur = target["mile"], target
    return stops
