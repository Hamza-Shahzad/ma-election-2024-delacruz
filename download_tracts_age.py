#!/usr/bin/env python3
"""
Download MA census tract shapefile, fetch ACS age data (B01001) from the
Census API, join them, and output a single GeoJSON with age bracket percentages
on every tract feature.

Also spatial-joins tracts to towns so the town-level age aggregation works.

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
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ma_tracts_age.geojson")
TOWN_GEOJSON = os.path.join(OUTPUT_DIR, "ma_towns.geojson")
TRACT_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_25_tract_500k.zip"

# ── ACS B01001 variable mapping ──
# B01001 — Sex by Age
# We fetch all 49 variables and aggregate them into 7 standard age brackets.
ACS_VARIABLES = {
    # Total
    "B01001_001E": "total_pop",
    # Male
    "B01001_002E": "male_total",
    "B01001_003E": "male_under5",
    "B01001_004E": "male_5_9",
    "B01001_005E": "male_10_14",
    "B01001_006E": "male_15_17",
    "B01001_007E": "male_18_19",
    "B01001_008E": "male_20",
    "B01001_009E": "male_21",
    "B01001_010E": "male_22_24",
    "B01001_011E": "male_25_29",
    "B01001_012E": "male_30_34",
    "B01001_013E": "male_35_39",
    "B01001_014E": "male_40_44",
    "B01001_015E": "male_45_49",
    "B01001_016E": "male_50_54",
    "B01001_017E": "male_55_59",
    "B01001_018E": "male_60_61",
    "B01001_019E": "male_62_64",
    "B01001_020E": "male_65_66",
    "B01001_021E": "male_67_69",
    "B01001_022E": "male_70_74",
    "B01001_023E": "male_75_79",
    "B01001_024E": "male_80_84",
    "B01001_025E": "male_85plus",
    # Female
    "B01001_026E": "female_total",
    "B01001_027E": "female_under5",
    "B01001_028E": "female_5_9",
    "B01001_029E": "female_10_14",
    "B01001_030E": "female_15_17",
    "B01001_031E": "female_18_19",
    "B01001_032E": "female_20",
    "B01001_033E": "female_21",
    "B01001_034E": "female_22_24",
    "B01001_035E": "female_25_29",
    "B01001_036E": "female_30_34",
    "B01001_037E": "female_35_39",
    "B01001_038E": "female_40_44",
    "B01001_039E": "female_45_49",
    "B01001_040E": "female_50_54",
    "B01001_041E": "female_55_59",
    "B01001_042E": "female_60_61",
    "B01001_043E": "female_62_64",
    "B01001_044E": "female_65_66",
    "B01001_045E": "female_67_69",
    "B01001_046E": "female_70_74",
    "B01001_047E": "female_75_79",
    "B01001_048E": "female_80_84",
    "B01001_049E": "female_85plus",
}

# Age bracket definitions: (bracket_key, bracket_label, [male_var_keys], [female_var_keys])
AGE_BRACKETS = [
    ("under18_pct", "Under 18 years", ["male_under5", "male_5_9", "male_10_14", "male_15_17"], ["female_under5", "female_5_9", "female_10_14", "female_15_17"]),
    ("age18_24_pct", "18 to 24 years", ["male_18_19", "male_20", "male_21", "male_22_24"], ["female_18_19", "female_20", "female_21", "female_22_24"]),
    ("age25_34_pct", "25 to 34 years", ["male_25_29", "male_30_34"], ["female_25_29", "female_30_34"]),
    ("age35_44_pct", "35 to 44 years", ["male_35_39", "male_40_44"], ["female_35_39", "female_40_44"]),
    ("age45_54_pct", "45 to 54 years", ["male_45_49", "male_50_54"], ["female_45_49", "female_50_54"]),
    ("age55_64_pct", "55 to 64 years", ["male_55_59", "male_60_61", "male_62_64"], ["female_55_59", "female_60_61", "female_62_64"]),
    ("age65plus_pct", "65 years and over", ["male_65_66", "male_67_69", "male_70_74", "male_75_79", "male_80_84", "male_85plus"], ["female_65_66", "female_67_69", "female_70_74", "female_75_79", "female_80_84", "female_85plus"]),
]

AGE_SHORT_LABELS = {
    "under18_pct": "Under 18",
    "age18_24_pct": "18-24",
    "age25_34_pct": "25-34",
    "age35_44_pct": "35-44",
    "age45_54_pct": "45-54",
    "age55_64_pct": "55-64",
    "age65plus_pct": "65+",
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
    """Fetch ACS 2022 5-year B01001 data for all MA census tracts."""
    vars_str = ",".join(ACS_VARIABLES.keys())
    url = (
        f"https://api.census.gov/data/2022/acs/acs5"
        f"?get=NAME,{vars_str}"
        f"&for=tract:*"
        f"&in=state:25"
        f"&key={api_key}"
    )
    print(f"\nFetching ACS age data from Census API...")
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

        total = int(record.get("B01001_001E", 0))
        if total == 0:
            continue

        entry = {"name": record.get("NAME", ""), "total_pop": total}

        # Parse all raw ACS values into internal keys
        raw = {}
        for var, key in ACS_VARIABLES.items():
            raw[key] = int(record.get(var, 0))

        # Aggregate into age brackets (male + female)
        for bracket_key, bracket_label, male_keys, female_keys in AGE_BRACKETS:
            bracket_sum = sum(raw.get(k, 0) for k in male_keys) + sum(raw.get(k, 0) for k in female_keys)
            entry[bracket_key] = round(bracket_sum / total * 100, 2)
            # Also store raw counts for tooltip display
            entry[bracket_key.replace("_pct", "_count")] = bracket_sum

        tract_data[full_geoid] = entry

    print(f"  Fetched age data for {len(tract_data)} MA census tracts")
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

    # ── Fetch ACS age data ──
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
    print("\nJoining tracts to ACS age data...")
    import fiona
    from shapely.geometry import shape as shapely_shape

    features_out = []
    matched = 0
    unmatched_data = 0
    unmatched_spatial = 0

    # Only keep relevant pct keys in output
    bracket_keys = [b[0] for b in AGE_BRACKETS]

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
            for key in bracket_keys:
                feat_props[key] = acs.get(key, 0)

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
        for key in bracket_keys:
            label = AGE_SHORT_LABELS.get(key, key)
            print(f"    {label}: {sample[key]}%")

    # ── Cleanup ──
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
