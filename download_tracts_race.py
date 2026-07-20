#!/usr/bin/env python3
"""
Download MA census tract shapefile, fetch ACS race/ethnicity data from the
Census API, join them, and output a single GeoJSON with race percentages
on every tract feature.

Also spatial-joins tracts to towns so the town-level race aggregation works.

Requires a free Census API key: https://api.census.gov/data/key_signup.html
Set it as CENSUS_API_KEY env var, or the script will prompt for it.
"""

import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections import defaultdict

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ma_tracts_race.geojson")
TOWN_GEOJSON = os.path.join(OUTPUT_DIR, "ma_towns.geojson")
TRACT_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_25_tract_500k.zip"

# ── ACS B03002 variable mapping ──
# B03002 — Hispanic or Latino Origin by Race
ACS_VARIABLES = {
    "B03002_001E": "total_pop",
    "B03002_003E": "white_nh",       # White alone, Not Hispanic
    "B03002_004E": "black_nh",       # Black alone, Not Hispanic
    "B03002_006E": "asian_nh",       # Asian alone, Not Hispanic
    "B03002_009E": "multi_nh",       # Two or more races, Not Hispanic
    "B03002_012E": "hispanic",       # Hispanic or Latino (any race)
}

# Categories we expose as percentages
RACE_CATEGORIES = ["white", "black", "hispanic", "asian", "multi"]

RACE_LABELS = {
    "white": "White (non-Hispanic)",
    "black": "Black / African American (non-Hispanic)",
    "hispanic": "Hispanic or Latino",
    "asian": "Asian (non-Hispanic)",
    "multi": "Two or More Races (non-Hispanic)",
}


def get_api_key():
    """Get Census API key from env var or prompt."""
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if key:
        return key
    print("A free Census API key is required for ACS data.")
    print("Sign up at: https://api.census.gov/data/key_signup.html")
    print()
    key = input("Enter your Census API key: ").strip()
    if not key:
        print("ERROR: No API key provided.")
        sys.exit(1)
    return key


def fetch_acs_data(api_key):
    """Fetch ACS 2022 5-year B03002 data for all MA census tracts."""
    vars_str = ",".join(ACS_VARIABLES.keys())
    url = (
        f"https://api.census.gov/data/2022/acs/acs5"
        f"?get=NAME,{vars_str}"
        f"&for=tract:*"
        f"&in=state:25"
        f"&key={api_key}"
    )
    print(f"\nFetching ACS race data from Census API...")
    try:
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ERROR: API request failed: {e}")
        print(f"  URL: {url.replace(api_key, '***')}")
        sys.exit(1)

    data = json.loads(body)
    headers = data[0]
    rows = data[1:]

    tract_data = {}
    for row in rows:
        record = dict(zip(headers, row))
        tract_geoid = record.get("tract", "")
        state = record.get("state", "")
        county = record.get("county", "")
        full_geoid = state + county + tract_geoid  # e.g., 25017350101

        total = int(record.get("B03002_001E", 0))
        if total == 0:
            continue

        entry = {"name": record.get("NAME", ""), "total_pop": total}
        for var, key in ACS_VARIABLES.items():
            val = int(record.get(var, 0))
            entry[key] = val
            pct_key = key.replace("_nh", "").replace("hispanic", "hispanic") + "_pct"
            # Build pct keys: white_pct, black_pct, asian_pct, multi_pct, hispanic_pct
            pct_key = pct_key.replace("total_pop", "total")
            entry[pct_key] = round(val / total * 100, 2)

        # Map internal keys to the final pct keys expected by index.html
        entry["white_pct"] = round(entry["white_nh"] / total * 100, 2)
        entry["black_pct"] = round(entry["black_nh"] / total * 100, 2)
        entry["hispanic_pct"] = round(entry["hispanic"] / total * 100, 2)
        entry["asian_pct"] = round(entry["asian_nh"] / total * 100, 2)
        entry["multi_pct"] = round(entry["multi_nh"] / total * 100, 2)

        tract_data[full_geoid] = entry

    print(f"  Fetched race data for {len(tract_data)} MA census tracts")
    return tract_data


def build_town_spatial_index(town_geojson_path):
    """Load town boundaries for spatial join."""
    from shapely.geometry import shape as shapely_shape

    if not os.path.exists(town_geojson_path):
        print(f"  WARNING: Town GeoJSON not found at {town_geojson_path}")
        return []

    with open(town_geojson_path) as f:
        data = json.load(f)

    town_geoms = []
    for feature in data["features"]:
        town_name = feature["properties"].get("town", "")
        try:
            geom = shapely_shape(feature["geometry"])
            if geom.is_valid:
                town_geoms.append((town_name, geom))
            else:
                town_geoms.append((town_name, geom.buffer(0)))
        except Exception:
            continue

    print(f"  Loaded {len(town_geoms)} town boundaries for spatial join")
    return town_geoms


def find_town_for_point(point, town_geoms):
    """Find which town contains the given point. Returns town name or None."""
    from shapely.geometry import Point as ShapelyPoint

    pt = ShapelyPoint(point[0], point[1])
    for town_name, geom in town_geoms:
        if geom.contains(pt) or geom.touches(pt):
            return town_name
        if geom.distance(pt) < 0.0001:
            return town_name
    return None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Get API key ──
    api_key = get_api_key()

    # ── Fetch ACS race data ──
    tract_data = fetch_acs_data(api_key)

    # ── Download and extract tract shapefile ──
    tmpdir = tempfile.mkdtemp(prefix="ma_tracts_")
    zip_path = os.path.join(tmpdir, "tracts.zip")

    print(f"\nDownloading Census tract shapefile...")
    print(f"  {TRACT_URL}")
    try:
        urllib.request.urlretrieve(TRACT_URL, zip_path)
    except Exception as e:
        print(f"  ERROR: Download failed: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmpdir)

    shp_path = None
    for f in os.listdir(tmpdir):
        if f.endswith(".shp"):
            shp_path = os.path.join(tmpdir, f)
            break

    if not shp_path:
        print("ERROR: No .shp file found in zip")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    # ── Load town boundaries for spatial join ──
    print("\nLoading town boundaries for spatial join...")
    town_geoms = build_town_spatial_index(TOWN_GEOJSON)

    # ── Parse tracts, join with ACS data ──
    print("\nJoining tracts to ACS race data...")
    import fiona
    from shapely.geometry import shape as shapely_shape

    features_out = []
    matched = 0
    unmatched_data = 0
    unmatched_spatial = 0

    with fiona.open(shp_path) as src:
        for feature in src:
            props = dict(feature["properties"])
            geom = feature["geometry"]

            # Convert geometry to plain dict
            if hasattr(geom, "__geo_interface__"):
                geom_dict = geom.__geo_interface__
            elif isinstance(geom, dict):
                geom_dict = geom
            else:
                geom_dict = dict(geom)

            geoid = props.get("GEOID", "")
            tract_name = props.get("NAME", "")
            namelsad = props.get("NAMELSAD", "")  # e.g., "Census Tract 3501.01"

            # Match to ACS data
            acs = tract_data.get(geoid)
            if acs is None:
                unmatched_data += 1
                continue

            # Spatial join: find which town this tract is in
            try:
                tract_geom = shapely_shape(geom_dict)
                centroid = tract_geom.centroid
                town = find_town_for_point((centroid.x, centroid.y), town_geoms)
            except Exception:
                town = None

            if town is None:
                unmatched_spatial += 1
                town = ""

            # Build output properties
            feat_props = {
                "geoid": geoid,
                "tract": tract_name,
                "namelsad": namelsad,
                "town": town,
                "total_pop": acs["total_pop"],
            }
            for cat in RACE_CATEGORIES:
                feat_props[f"{cat}_pct"] = acs.get(f"{cat}_pct", 0)

            features_out.append({
                "type": "Feature",
                "properties": feat_props,
                "geometry": geom_dict,
            })
            matched += 1

    geojson = {
        "type": "FeatureCollection",
        "features": features_out,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(geojson, f)

    size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    print(f"\n── Results ──")
    print(f"  Tracts with ACS data: {matched}")
    print(f"  Unmatched (no ACS data): {unmatched_data}")
    print(f"  Unmatched (no town via spatial join): {unmatched_spatial}")
    print(f"  Output features: {len(features_out)}")
    print(f"  File: {OUTPUT_FILE} ({size_mb:.1f} MB)")

    # ── Sample output ──
    if features_out:
        print(f"\n  Sample tract:")
        sample = features_out[0]["properties"]
        print(f"    GEOID: {sample['geoid']}")
        print(f"    Town: {sample['town'] or '(none)'}")
        print(f"    Total pop: {sample['total_pop']:,}")
        for cat in RACE_CATEGORIES:
            print(f"    {RACE_LABELS[cat]}: {sample[f'{cat}_pct']}%")

    # ── Cleanup ──
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
