# Fuel Route Planner (Django 6.1 + DRF)

Given a start and finish in the USA, returns the driving route (GeoJSON + a Leaflet map page), the
cheapest fuel stops along it (500-mile range), and total fuel spend at 10 MPG.

## Quick start

**Requirements:** Python 3.12+ (Django 6.1 needs it) and internet access (for the free OSRM/Nominatim APIs).

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate            # Mac/Linux
.venv\Scripts\activate               # Windows (cmd / PowerShell)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Tell Nominatim who you are (their usage policy requires a contact in the User-Agent)
export GEO_USER_AGENT="fuelroute-assessment/1.0 (your@email.com)"     # Mac/Linux
set GEO_USER_AGENT=fuelroute-assessment/1.0 (your@email.com)          # Windows cmd
$env:GEO_USER_AGENT="fuelroute-assessment/1.0 (your@email.com)"       # Windows PowerShell

# 4. Create the database table
python manage.py migrate

# 5. REQUIRED, one-off: import the fuel prices into the database (takes a few seconds)
python manage.py load_stations data/fuel-prices.csv
#    expected: "Loaded 6614 stations from 8151 rows ..."

# 6. Start the server
python manage.py runserver
#    now running at http://127.0.0.1:8000
```

Step 5 is required: the API reads stations from the database, not from the CSV, and the repo does not ship a
database (`db.sqlite3` is git-ignored). Without it, trips over 500 miles return a 422 "no fuel station" error.
It only needs to be done once; re-running it wipes and reloads the stations (add `--keep` to append instead).

Check it loaded: `python manage.py shell -c "from planner.models import FuelStation as F; print(F.objects.count())"` prints `6614`.

## Try it

**Browser:** http://127.0.0.1:8000/api/route/map/?start=Dallas,%20TX&finish=Seattle,%20WA

**curl:**
```bash
curl -G "http://127.0.0.1:8000/api/route/" --data-urlencode "start=Dallas, TX" --data-urlencode "finish=Seattle, WA"
```

**Postman:**
1. Method `GET`, URL `http://127.0.0.1:8000/api/route/`
2. Params tab: `start` = `Dallas, TX`, `finish` = `Seattle, WA` -> **Send**
3. Or `POST` the same URL with Body -> raw -> JSON: `{"start": "Los Angeles, CA", "finish": "New York, NY"}`
4. Copy `map_url` from the response into a browser to see the map.
5. Coordinates also work and skip geocoding: `start` = `32.78,-96.80`, `finish` = `47.61,-122.33`
6. Send the same request twice: the second is served from cache and should be much faster.

**Run the tests:** `python manage.py test` (no internet needed)

**Troubleshooting**
| Symptom | Cause / fix |
|---|---|
| `422 No fuel station within range` on a long trip | Stations not loaded: run step 5. Or the route genuinely passes through an area the data doesn't cover (see edge cases). |
| `422 Geocoding service unavailable` | No internet, or Nominatim blocked/rate-limited you. Set `GEO_USER_AGENT`, or use `lat,lng` input. |
| `422 Routing service unavailable` | Public OSRM demo server is slow/down. Retry, or point `OSRM_URL` at another OSRM instance. |
| First request slow, repeats fast | Expected: the first call hits OSRM/Nominatim, repeats are cached. |
| `ModuleNotFoundError` | Virtual environment not activated, or step 2 skipped. |

**Configuration (environment variables, all optional):** `GEO_USER_AGENT`, `OSRM_URL`, `NOMINATIM_URL`,
`CORRIDOR_MILES` (default 10), `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (default 1; set 0 in production).

## API
`GET /api/route/?start=Dallas, TX&finish=Seattle, WA`  (or `POST` JSON `{"start": "...", "finish": "..."}`; `"lat,lng"` also accepted)

`GET /api/route/map/?start=...&finish=...` renders the route and stops on a map in the browser.

Response: `distance_miles`, `duration_minutes`, `fuel_stops[]` (name, address, lat/lng, price, mile marker,
gallons, cost), `total_fuel_cost`, `total_fuel_gallons_purchased`, `assumptions`, `route` (GeoJSON), `map_url`.

## Design decisions
- **External calls**: OSRM public server for routing (no key), Nominatim for geocoding. One OSRM call per
  route; geocoding only for free-text inputs (so 3 calls max, 1 if "lat,lng" is passed). All results cached,
  so repeats cost 0 calls.
- **Stations are located once at import** (by city/state) using the bundled offline US cities gazetteer
  (`data/us_cities.csv`), with Nominatim only as a fallback for misses. Never at request time.
- **Data cleaning** (fuel-prices CSV: 8,151 rows): 620 Canadian rows dropped; rows are one-per-price-listing
  (e.g. 678 station IDs repeat with different prices), so they are collapsed to one station per OPIS ID keeping
  the cheapest price -> 6,614 stations. 12 stations whose city isn't in the gazetteer are skipped.
- **Speed**: the finished plan is cached too (key = endpoints + station-data version + settings), so a repeat
  request skips routing AND optimisation (~3 ms vs ~300 ms cold on a cross-country route, excluding the
  OSRM/Nominatim network time). Re-importing stations changes the version, so stale plans are never served.
  Stations live in memory as numpy arrays; bounding-box pre-filter, then vectorised
  nearest-point search against a ~1-mile-resolution downsample of the route. Typical latency is dominated
  by the single OSRM call.
- **Optimiser** (`planner/services/optimizer.py`): greedy gas-station algorithm, optimal for continuous
  fuel purchase. Buy just enough to reach a cheaper station ahead; otherwise fill up; destination is a
  free virtual station at the end.
- **Assumptions**: vehicle starts with a full 500-mile tank (not billed); only fuel bought en route is
  costed; stations within 10 miles of the route are candidates (`CORRIDOR_MILES`). Because stations are
  geocoded at city level, treat locations as approximate.
- Returns HTTP 422 if the trip is infeasible (no station within range).

## Scope, assumptions and known edge cases

**Supported area: the contiguous USA (48 states + DC).**

| Case | Behaviour |
|---|---|
| Alaska / Hawaii | Not supported. The input check is a contiguous-US bounding box, the fuel file has no AK/HI stations, and Hawaii cannot be driven to. Returns `422` ("outside the supported USA area"). |
| Free-text place outside the US (e.g. "London, UK") | Nominatim is restricted to `countrycodes=us`, so it returns `422`. |
| `lat,lng` input inside the box but in Canada/Mexico | **Known gap**: the box is rough, so border-area points can pass and OSRM will route them. Fix would be one cached reverse-geocode call per coordinate (adds a call to the fast path), so it is left out deliberately. |
| Missing `start` / `finish` | `400` with a validation message. |
| Start == finish | `200`, 0 miles, no stops, $0. |
| No drivable route (e.g. point in the ocean) | `422` from OSRM's `NoRoute`. |
| Gap between stations longer than 500 miles | `422` ("No fuel station within range after mile N"). |
| Geocoding/routing provider down or slow | `422` with a readable message (15 s timeout), no 500. |
| Inputs with spaces or `&` | `map_url` is URL-encoded. |

**Fuel-price data notes** (`fuel-prices.csv`, 8,151 rows):
- 620 Canadian rows are dropped; rows are one-per-price-listing, so they are collapsed to one station per OPIS ID
  keeping the cheapest price (6,614 stations). 12 stations whose city is not in the gazetteer are skipped.
- Stations are located by **city centre**, not exact address (the file has only highway-exit text), so a stop's
  pin can be a few miles off. This is why stations within 10 miles of the route are considered.
- **Coverage gap: the file has only 8 California stations**, all in the far south-east desert, and none near
  Los Angeles or San Francisco. Trips whose fuel stops would fall in California (for example along I-5 between
  Oregon and Southern California) can return `422`, because there is nothing within 500 miles to buy fuel from.
  This is a limit of the data, not of the algorithm. Trips that start in California on a full tank and leave
  towards Arizona/Nevada work normally.

**Pricing assumptions**
- The vehicle starts with a full 500-mile tank that is **not billed**; only fuel bought en route is costed.
  Trips under 500 miles therefore show $0 and no stops. (`initial_miles` in `optimizer.plan_fuel_stops` changes this.)
- The optimiser may buy small amounts at several stations when each is cheaper than the last. This is
  cost-optimal but a real driver might prefer fewer, larger purchases.

## Production notes
- `LocMemCache` is per process; use Redis (`django.core.cache.backends.redis.RedisCache`) when running several workers.
- The public OSRM/Nominatim servers are demo services. Self-host them or use a paid provider in production.
- Next speed-ups: KD-tree (`scipy.spatial.cKDTree`) for the station search, PostGIS `ST_DWithin` for the corridor
  query, route simplification / gzip for large GeoJSON responses, an async view with `httpx`.

## Tests and verification results

Run the automated suite with `python manage.py test` (no internet needed: routing is mocked).

**Automated tests: 13 run, 13 passed.**

| Area | Test | What it proves |
|---|---|---|
| Optimizer | short trip needs no stops | Trips within the 500-mile starting range cost $0 |
| Optimizer | buys only enough to reach a cheaper station | Exact gallons and cost: 10 gal @ $4.00 + 20 gal @ $3.00 |
| Optimizer | fills up at cheap station before expensive ones | Refills to full when nothing cheaper is in range |
| Optimizer | zero-distance trip | Start == finish returns no stops (this test caught a real bug) |
| Optimizer | infeasible trip | Raises an error when stations are more than 500 miles apart |
| Geometry | nearest point on route | Station distance and mile marker are computed correctly |
| API | plan | Returns stops, GeoJSON route, and exactly **one** routing call |
| API | validation | Missing `start`/`finish` returns 400 |
| API | repeat request served from plan cache | Second identical request makes 0 routing calls |
| API | cache invalidated when stations change | A data reload forces a recompute |
| API | same start and finish | Returns 200, no stops |
| API | point outside USA rejected | Returns 422 before any routing call |
| API | map_url is encoded | Spaces/commas are URL-encoded |

**Manual verification on the real CSV** (done with a straight-line stand-in route, because the development
sandbox could not reach OSRM; the live OSRM/Nominatim calls were not exercised there, so check them locally):

| Check | Result |
|---|---|
| Import | 8,151 rows read, 620 non-US dropped, duplicates collapsed, **6,614 stations loaded** (12 skipped: city not in gazetteer) |
| Import speed | Seconds, using the offline gazetteer (Nominatim for all 4,179 cities would take about 75 minutes) |
| Dallas to Seattle | 1,944 mi, 12 stops, $460.93 total |
| Los Angeles to New York | 2,841 mi, 17 stops, $714.03 total |
| Houston to Austin | 168 mi, 0 stops, $0 (starts with a full tank, see assumptions) |
| Cold request (cross-country) | about 280 ms of compute, excluding OSRM/Nominatim network time |
| Repeated request | about 3 ms, 0 additional routing calls |
| California coverage | Only 8 California stations in the file; stations near LA/SF do not exist (see edge cases) |
| San Francisco to Seattle, Seattle to Los Angeles (straight-line stand-in) | Returned 422 "no station within range". Straight-line routes are not road-accurate, so this is indicative only; real I-5 trips are likely affected by the same data gap |
