from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import FuelStation
from .services import geo, optimizer, planner


def S(mile, price):
    return {"mile": mile, "price": price, "name": f"s{mile}"}


class OptimizerTests(TestCase):
    def test_short_trip_needs_no_stops(self):
        self.assertEqual(optimizer.plan_fuel_stops([S(100, 3)], 400), [])

    def test_buys_only_enough_to_reach_cheaper_station(self):
        # 800mi trip, full tank at start. Expensive @300, cheap @600.
        stops = optimizer.plan_fuel_stops([S(300, 4.0), S(600, 3.0)], 800)
        self.assertEqual([s.station["mile"] for s in stops], [300, 600])
        # at 300 (fuel left 200): need 300 to reach 600 -> buy 100mi = 10gal
        self.assertAlmostEqual(stops[0].gallons, 10)
        # at 600 (fuel 0): need 200 -> 20 gal @3
        self.assertAlmostEqual(stops[1].gallons, 20)
        self.assertAlmostEqual(sum(s.cost for s in stops), 10 * 4 + 20 * 3)

    def test_fills_up_at_cheap_station_before_expensive_ones(self):
        stops = optimizer.plan_fuel_stops([S(400, 2.0), S(800, 5.0)], 1200)
        self.assertEqual(stops[0].station["mile"], 400)
        self.assertAlmostEqual(stops[0].gallons, 40)  # 400mi used, refill to full = 400mi of fuel

    def test_zero_distance_trip(self):
        self.assertEqual(optimizer.plan_fuel_stops([S(0, 3)], 0), [])

    def test_infeasible(self):
        with self.assertRaises(optimizer.NoFeasibleRoute):
            optimizer.plan_fuel_stops([S(100, 3)], 1000)


class GeoTests(TestCase):
    def test_nearest_on_route(self):
        pts = np.array([[40.0, -100.0 + i * 0.1] for i in range(100)])
        cum = geo.cumulative_miles(pts)
        d, m = geo.nearest_on_route(np.array([[40.05, -95.0]]), pts, cum, 10)
        self.assertLess(d[0], 5)
        self.assertAlmostEqual(m[0], cum[50], delta=1)


class ApiTests(TestCase):
    def setUp(self):
        planner._cache.clear()
        cache.clear()
        # straight east-west "route" ~ 1100 miles
        self.coords = [[-100.0 + i * 0.05, 40.0] for i in range(0, 500)]
        for lng, price in [(-98.0, 3.9), (-93.0, 3.2), (-89.0, 3.6), (-84.0, 3.0), (-79.0, 3.4), (-75.5, 3.3)]:
            FuelStation.objects.create(name=f"Stn{lng}", city="X", state="NE", price=price, lat=40.01, lng=lng)

    def test_plan(self):
        route = {"coords": self.coords, "distance_miles": 1400.0, "duration_minutes": 1200}
        with mock.patch("planner.services.providers.get_route", return_value=route) as gr:
            r = APIClient().get("/api/route/", {"start": "40.0,-100.0", "finish": "40.0,-75.0"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(gr.call_count, 1)  # exactly one routing call
        body = r.json()
        self.assertTrue(body["fuel_stops"])
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertAlmostEqual(body["total_fuel_cost"], sum(s["cost"] for s in body["fuel_stops"]), places=1)

    def test_validation(self):
        self.assertEqual(APIClient().get("/api/route/").status_code, 400)

    @mock.patch("rest_framework.throttling.AnonRateThrottle.THROTTLE_RATES", {"anon": "2/minute"})
    def test_anonymous_rate_limit_returns_429(self):
        params = {"start": "40.0,-100.0", "finish": "40.0,-75.0"}
        route = {"coords": self.coords, "distance_miles": 1400.0, "duration_minutes": 1200}
        client = APIClient()
        with mock.patch("planner.services.providers.get_route", return_value=route):
            self.assertEqual(client.get("/api/route/", params).status_code, 200)
            self.assertEqual(client.get("/api/route/", params).status_code, 200)
            response = client.get("/api/route/", params)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def _get(self, params):
        route = {"coords": self.coords, "distance_miles": 1400.0, "duration_minutes": 1200}
        with mock.patch("planner.services.providers.get_route", return_value=route) as gr:
            r = APIClient().get("/api/route/", params)
        return r, gr

    def test_repeat_request_served_from_plan_cache(self):
        p = {"start": "40.0,-100.0", "finish": "40.0,-75.0"}
        r1, gr1 = self._get(p)
        r2, gr2 = self._get(p)
        self.assertEqual(gr1.call_count, 1)
        self.assertEqual(gr2.call_count, 0)  # no routing call, no re-optimising
        self.assertEqual(r1.json()["total_fuel_cost"], r2.json()["total_fuel_cost"])

    def test_cache_invalidated_when_stations_change(self):
        p = {"start": "40.0,-100.0", "finish": "40.0,-75.0"}
        self._get(p)
        FuelStation.objects.create(name="New", city="X", state="NE", price=1.0, lat=40.01, lng=-96.0)
        _, gr = self._get(p)
        self.assertEqual(gr.call_count, 1)  # data changed -> recomputed

    def test_same_start_and_finish(self):
        route = {"coords": [[-100.0, 40.0], [-100.0, 40.0]], "distance_miles": 0.0, "duration_minutes": 0}
        with mock.patch("planner.services.providers.get_route", return_value=route):
            r = APIClient().get("/api/route/", {"start": "40.0,-100.0", "finish": "40.0,-100.0"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["fuel_stops"], [])

    def test_point_outside_usa_rejected(self):
        r, gr = self._get({"start": "55.0,-100.0", "finish": "40.0,-75.0"})  # central Canada
        self.assertEqual(r.status_code, 422)
        self.assertIn("outside the supported", r.json()["error"])
        self.assertEqual(gr.call_count, 0)  # rejected before any routing call

    def test_map_url_is_encoded(self):
        r, _ = self._get({"start": "40.0,-100.0", "finish": "40.0,-75.0"})
        self.assertIn("start=40.0%2C-100.0", r.json()["map_url"])
