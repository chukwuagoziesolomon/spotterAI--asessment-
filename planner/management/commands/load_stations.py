"""python manage.py load_stations data/fuel-prices.csv

Imports fuel prices and locates each station by its (city, state):
  1. offline US cities gazetteer (data/us_cities.csv) - instant, ~99.8% match on the assessment file
  2. Nominatim fallback for the few misses (1 req/sec, cached in data/geocode_cache.json)
Data cleaning: non-US rows (Canadian provinces) are dropped, and the many rows per OPIS station ID
(one per price listing) are collapsed to ONE station keeping the cheapest price.
After this one-off step the API never geocodes stations.
"""
import csv
import json
import re
import time
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.models import FuelStation

ALIASES = {
    "opis_id": ["opistruckstopid", "id", "stationid"],
    "name": ["truckstopname", "name", "stationname"],
    "address": ["address", "street"],
    "city": ["city"],
    "state": ["state", "st"],
    "price": ["retailprice", "price", "fuelprice", "pricepergallon"],
    "lat": ["latitude", "lat"],
    "lng": ["longitude", "lng", "lon"],
}


US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
                "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def norm(h):
    return re.sub(r"[^a-z]", "", h.lower())


def city_key(city, state):
    c = str(city).lower().strip()
    for a, b in (("saint ", "st "), ("st. ", "st "), ("mount ", "mt "), ("mt. ", "mt "), ("fort ", "ft "), ("ft. ", "ft ")):
        c = c.replace(a, b)
    return re.sub(r"[^a-z0-9 ]", "", c).strip() + "|" + str(state).strip().upper()


def load_gazetteer(path):
    table = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                table.setdefault(city_key(r["CITY"], r["STATE_CODE"]), [float(r["LATITUDE"]), float(r["LONGITUDE"])])
    return table


class Command(BaseCommand):
    help = "Load fuel stations from CSV and geocode their cities"

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--keep", action="store_true", help="do not wipe existing stations")

    def handle(self, *args, csv_path, keep, **opts):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"{path} not found")
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = {}
            for h in reader.fieldnames:
                for key, names in ALIASES.items():
                    if norm(h) in names and key not in cols:
                        cols[key] = h
            for req in ("name", "city", "state", "price"):
                if req not in cols:
                    raise CommandError(f"CSV is missing a '{req}' column. Headers: {reader.fieldnames}")
            rows = list(reader)

        cache_file = Path(settings.BASE_DIR) / "data" / "geocode_cache.json"
        gcache = json.loads(cache_file.read_text()) if cache_file.exists() else {}

        gazetteer = load_gazetteer(Path(settings.BASE_DIR) / "data" / "us_cities.csv")
        stats = {"gazetteer": 0, "nominatim": 0}

        def geocode(city, state):
            gk = city_key(city, state)
            if gk in gazetteer:
                stats["gazetteer"] += 1
                return gazetteer[gk]
            key = f"{city.strip().lower()}|{state.strip().upper()}"
            if key not in gcache:
                stats["nominatim"] += 1
                try:
                    r = requests.get(f"{settings.NOMINATIM_URL}/search", params={
                        "city": city, "state": state, "country": "us", "format": "jsonv2", "limit": 1},
                        headers={"User-Agent": settings.GEO_USER_AGENT}, timeout=settings.HTTP_TIMEOUT)
                    data = r.json() if r.ok else []
                except requests.RequestException:
                    return None  # fail soft: station is skipped, not cached, retried on next run
                gcache[key] = [float(data[0]["lat"]), float(data[0]["lon"])] if data else None
                cache_file.write_text(json.dumps(gcache))
                time.sleep(1.1)  # Nominatim usage policy
            return gcache[key]

        # Clean: US only; one station per OPIS id (else name+address+city), keeping the cheapest price
        best, skipped, non_us = OrderedDict(), 0, 0
        for row in rows:
            if row[cols["state"]].strip().upper() not in US_STATES:
                non_us += 1
                continue
            try:
                price = Decimal(row[cols["price"]].replace("$", "").strip())
            except (InvalidOperation, AttributeError):
                skipped += 1
                continue
            ident = (row.get(cols["opis_id"], "") if "opis_id" in cols else "").strip() or \
                "|".join(row[cols[k]].strip().lower() for k in ("name", "city", "state"))
            if ident not in best or price < best[ident][0]:
                best[ident] = (price, row)

        stations = []
        for i, (price, row) in enumerate(best.values(), 1):
            row[cols["city"]] = row[cols["city"]].strip()
            if "lat" in cols and row.get(cols["lat"]):
                ll = [float(row[cols["lat"]]), float(row[cols["lng"]])]
            else:
                ll = geocode(row[cols["city"]], row[cols["state"]])
            if not ll:
                skipped += 1
                continue
            stations.append(FuelStation(
                opis_id=row.get(cols.get("opis_id", ""), "") or "",
                name=row[cols["name"]].strip(), address=(row.get(cols.get("address", "")) or "").strip(),
                city=row[cols["city"]].strip(), state=row[cols["state"]].strip().upper(),
                price=price, lat=ll[0], lng=ll[1]))
            if i % 1000 == 0:
                self.stdout.write(f"  processed {i}/{len(best)}")

        if not keep:
            FuelStation.objects.all().delete()
        FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(stations)} stations from {len(rows)} rows "
            f"(non-US dropped: {non_us}, unlocatable/invalid skipped: {skipped}; "
            f"located via gazetteer: {stats['gazetteer']} lookups, nominatim: {stats['nominatim']})"))
