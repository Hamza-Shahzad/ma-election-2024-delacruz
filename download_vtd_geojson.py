#!/usr/bin/env python3
"""
Download Census Bureau MA voting district (VTD) shapefile,
match each VTD to PD43+ precinct data, and output a single GeoJSON
with election results joined to every feature.

VTDs are Census approximations of voting precincts — 2,152 for MA.
Naming is consistent: "Springfield City Ward 8 Precinct H",
"Athol Town Precinct 3", etc.
"""

import csv
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
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ma_vtds.geojson")
TOWN_GEOJSON = os.path.join(OUTPUT_DIR, "ma_towns.geojson")
CENSUS_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_25_vtd_500k.zip"
INPUT_CSV = "PD43+__2024_President_General_Election_including_precincts.csv"

# ── Town name normalization ──
DIR_MAP = {
    "N.": "North",
    "S.": "South",
    "E.": "East",
    "W.": "West",
}
# Reverse: full → abbreviated (for matching against PD43+ data)
DIR_REVERSE = {v: k for k, v in DIR_MAP.items()}

# Known town name overrides (VTD name → PD43+ name)
TOWN_OVERRIDES = {
    "Manchester": "Manchester-by-the-Sea",
    "East Hampton": "Easthampton",
    "MT Washington": "Mount Washington",
    "Easthampton City": "Easthampton",
    "WestwoodTown": "Westwood",
}


def normalize_town(name):
    """Normalize town name to match PD43+ convention (abbreviated directions)."""
    name = name.strip()
    # Check overrides first
    for vtd_name, pd43_name in TOWN_OVERRIDES.items():
        if name.lower() == vtd_name.lower():
            return pd43_name
    # Expand any abbreviated direction (N. → North) — VTDs use full names
    for abbr, full in DIR_MAP.items():
        if name.startswith(abbr + " "):
            name = full + " " + name[len(abbr) + 1 :]
    # Now check if any full direction should be abbreviated to match PD43+
    # PD43+ uses "N. Adams", "S. Hadley", etc.
    # VTDs use "North Adams", "South Hadley", etc.
    # We'll match in the lookup function instead
    return name


def parse_vtd_name(name):
    """
    Parse Census VTD NAME20 into (town, ward, precinct).
    Returns (town, ward, precinct) where ward='-' means no ward.

    Patterns:
      "Athol Town Precinct 3"              → Athol, -, 3
      "New Bedford City Ward 3 Precinct F" → New Bedford, 3, F
      "Wilbraham Town Precinct A"          → Wilbraham, -, A
      "Springfield City Ward 8 Precinct H" → Springfield, 8, H
    """
    if not name or not name.strip():
        return None, None, None

    name = name.strip()

    # "City Ward X Precinct Y"
    m = re.match(r"^(.+?)\s+City\s+Ward\s+(\S+)\s+Precinct\s+(\S+)$", name)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()

    # "Town Precinct X"
    m = re.match(r"^(.+?)\s+Town\s+Precinct\s+(\S+)$", name)
    if m:
        return m.group(1).strip(), "-", m.group(2).strip()

    # "City Precinct X" (city without wards, e.g., "Easthampton City Precinct 1")
    m = re.match(r"^(.+?)\s+City\s+Precinct\s+(\S+)$", name)
    if m:
        return m.group(1).strip(), "-", m.group(2).strip()

    # Fallback: "Something Precinct X" (no Town/City marker)
    m = re.match(r"^(.+?)\s+Precinct\s+(\S+)$", name)
    if m:
        return m.group(1).strip(), "-", m.group(2).strip()

    # Fallback: just the name itself — treat as single precinct
    return name, "-", "1"

    return name, None, None


def parse_numeric_vtd_name(name):
    """
    Parse a numeric VTD name like "0101" or "0502A" into (ward, precinct).
    Format is typically WWPP[Suffix] where WW = ward (2 digits), PP = precinct (2 digits).
    Returns (ward_str, precinct_str).
    """
    m = re.match(r"^(\d{2})(\d{2,3})([A-Za-z]?)$", name)
    if m:
        ward = str(int(m.group(1)))  # strip leading zero
        pct = str(int(m.group(2))) + m.group(3)  # strip leading zero, keep suffix
        return ward, pct
    return None, None


def build_town_spatial_index(town_geojson_path):
    """
    Load town boundaries and build a list of (town_name, shapely_geometry) for
    point-in-polygon testing.
    """
    from shapely.geometry import shape as shapely_shape

    if not os.path.exists(town_geojson_path):
        print(f"  WARNING: Town GeoJSON not found at {town_geojson_path}")
        print(f"  Run download_geojson.py first to generate it.")
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
                # Try to fix invalid geometries
                town_geoms.append((town_name, geom.buffer(0)))
        except Exception:
            continue

    print(f"  Loaded {len(town_geoms)} town boundaries for spatial join")
    return town_geoms


def find_town_for_point(point, town_geoms):
    """
    Find which town contains the given point.
    Returns town name or None.
    """
    from shapely.geometry import Point as ShapelyPoint

    pt = ShapelyPoint(point[0], point[1])
    for town_name, geom in town_geoms:
        if geom.contains(pt) or geom.touches(pt):
            return town_name
        # Also check distance within a small tolerance (for boundary points)
        if geom.distance(pt) < 0.0001:
            return town_name
    return None


def load_pd43_data(csv_path):
    """Load PD43+ CSV and return (precinct_lookup, precinct_data)."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Build key → data lookup and set of keys
    precinct_data = {}  # (town, ward, pct) → vote counts
    key_set = set()

    for row in rows[2:]:  # Skip header + party sub-header
        if len(row) < 14:
            continue
        town = row[0].strip()
        if town.upper() == "TOTALS":
            continue

        ward = row[1].strip()
        pct = row[2].strip()

        try:
            total = int(row[13].replace(",", ""))
            delacruz = int(row[8].replace(",", ""))
            harris = int(row[3].replace(",", ""))
            trump = int(row[4].replace(",", ""))
        except (ValueError, IndexError):
            continue

        key = (town, ward, pct)
        precinct_data[key] = {
            "town": town,
            "ward": ward,
            "pct": pct,
            "total_votes": total,
            "de_la_cruz": delacruz,
            "de_la_cruz_pct": round(delacruz / total * 100, 3) if total > 0 else 0,
            "harris": harris,
            "trump": trump,
        }
        key_set.add(key)

    return precinct_data, key_set


def match_town_names(vtd_town, pd43_towns_set):
    """
    Try to match a VTD town name to PD43+ town names.
    VTDs use full directional names ("North Adams");
    PD43+ uses abbreviations ("N. Adams").
    """
    # Exact match
    if vtd_town in pd43_towns_set:
        return vtd_town

    # Try abbreviated versions (North → N.)
    for full, abbr in DIR_REVERSE.items():
        if vtd_town.startswith(full + " "):
            candidate = abbr + " " + vtd_town[len(full) + 1 :]
            if candidate in pd43_towns_set:
                return candidate

    # Try full versions (N. → North)
    for abbr, full in DIR_MAP.items():
        if vtd_town.startswith(abbr + " "):
            candidate = full + " " + vtd_town[len(abbr) + 1 :]
            if candidate in pd43_towns_set:
                return candidate

    # Check overrides
    for vtd_override, pd43_name in TOWN_OVERRIDES.items():
        if vtd_town.lower() == vtd_override.lower():
            return pd43_name

    return None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Load PD43+ data ──
    print("Loading PD43+ precinct data...")
    precinct_data, pd43_keys = load_pd43_data(INPUT_CSV)
    pd43_towns = set(k[0] for k in pd43_keys)
    print(f"  {len(precinct_data)} precincts across {len(pd43_towns)} towns")

    # ── Download and extract VTD shapefile ──
    tmpdir = tempfile.mkdtemp(prefix="ma_vtd_")
    zip_path = os.path.join(tmpdir, "vtd.zip")

    print(f"\nDownloading Census VTD shapefile...")
    print(f"  {CENSUS_URL}")
    try:
        urllib.request.urlretrieve(CENSUS_URL, zip_path)
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

    # ── Parse VTDs and match ──
    print("\nParsing VTD features and matching to PD43+...")
    import fiona
    from shapely.geometry import shape as shapely_shape

    features_out = []
    matched = 0
    unmatched = 0
    unmatched_samples = []
    matched_by_spatial = 0

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

            vtd_name = props.get("NAME20", "")
            vtd_town, vtd_ward, vtd_pct = parse_vtd_name(vtd_name)

            # ── Handle numeric VTD names (e.g., Boston: "0101") ──
            is_numeric_vtd = False
            if vtd_town is not None and re.match(r"^\d+[A-Za-z]?$", vtd_name):
                is_numeric_vtd = True
                numeric_ward, numeric_pct = parse_numeric_vtd_name(vtd_name)
                if numeric_ward is None:
                    unmatched += 1
                    continue

                # Use spatial join to find which town this VTD belongs to
                try:
                    vtd_geom = shapely_shape(geom_dict)
                    centroid = vtd_geom.centroid
                    spatial_town = find_town_for_point(
                        (centroid.x, centroid.y), town_geoms
                    )
                except Exception:
                    spatial_town = None

                if spatial_town is None:
                    unmatched += 1
                    if len(unmatched_samples) < 20:
                        unmatched_samples.append(
                            f"Numeric VTD '{vtd_name}' — spatial join failed"
                        )
                    continue

                # Match spatial town name to PD43+ convention
                pd43_town = match_town_names(spatial_town, pd43_towns)
                if pd43_town is None:
                    unmatched += 1
                    if len(unmatched_samples) < 20:
                        unmatched_samples.append(
                            f"Numeric VTD '{vtd_name}' → spatial town '{spatial_town}' not in PD43+"
                        )
                    continue

                vtd_town = pd43_town
                vtd_ward = numeric_ward
                vtd_pct = numeric_pct
                matched_by_spatial += 1
            else:
                if vtd_town is None:
                    unmatched += 1
                    continue

                # Match town name to PD43+ convention
                pd43_town = match_town_names(vtd_town, pd43_towns)

                if pd43_town is None:
                    unmatched += 1
                    if len(unmatched_samples) < 20:
                        unmatched_samples.append(
                            f"VTD town '{vtd_town}' not found in PD43+"
                        )
                    continue

                vtd_town = pd43_town

            # Build lookup key
            key = (vtd_town, vtd_ward, vtd_pct)
            pd = precinct_data.get(key)

            if pd is None:
                unmatched += 1
                if len(unmatched_samples) < 30:
                    unmatched_samples.append(
                        f"Precinct not found: {vtd_town} / {vtd_ward} / {vtd_pct}"
                    )
                # Still include the feature, just without data
                feat_props = {
                    "town": vtd_town,
                    "ward": vtd_ward,
                    "pct": vtd_pct,
                    "vtd_name": vtd_name,
                    "matched": False,
                    "total_votes": 0,
                    "de_la_cruz": 0,
                    "de_la_cruz_pct": 0,
                    "harris": 0,
                    "trump": 0,
                }
            else:
                matched += 1
                feat_props = {
                    "town": pd["town"],
                    "ward": pd["ward"],
                    "pct": pd["pct"],
                    "vtd_name": vtd_name,
                    "matched": True,
                    "total_votes": pd["total_votes"],
                    "de_la_cruz": pd["de_la_cruz"],
                    "de_la_cruz_pct": pd["de_la_cruz_pct"],
                    "harris": pd["harris"],
                    "trump": pd["trump"],
                    # Include full town name for GeoJSON join fallback
                    "vtd_town_full": vtd_town,
                }

            features_out.append(
                {
                    "type": "Feature",
                    "properties": feat_props,
                    "geometry": geom_dict,
                }
            )

    geojson = {
        "type": "FeatureCollection",
        "features": features_out,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(geojson, f)

    size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    print(f"\n── Results ──")
    print(f"  Total VTDs: {len(features_out) + unmatched}")
    print(f"  Matched to PD43+: {matched}")
    print(f"  Matched via spatial join: {matched_by_spatial}")
    print(f"  Unmatched: {unmatched}")
    print(f"  Output features: {len(features_out)}")
    print(f"  File: {OUTPUT_FILE} ({size_mb:.1f} MB)")

    if unmatched_samples:
        print(f"\n  Sample unmatched ({len(unmatched_samples)} shown):")
        for s in unmatched_samples[:15]:
            print(f"    {s}")

    # ── Cleanup ──
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
