#!/usr/bin/env python3
"""
Download Census Bureau MA county subdivisions shapefile and convert to simplified GeoJSON.
Uses urllib (stdlib) for download, zipfile (stdlib) for extraction, and fiona for conversion.
"""

import os
import sys
import json
import zipfile
import urllib.request
import tempfile
import shutil

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "ma_towns.geojson")
CENSUS_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_25_cousub_500k.zip"

# Town name mappings: Census subdiv names -> election data names
# Census uses "town" suffix, our data doesn't
# Census uses full names like "North Adams", our data has "N. Adams"

def download(url, dest):
    """Download a file with progress reporting."""
    print(f"Downloading {url}...")
    try:
        urllib.request.urlretrieve(url, dest)
        print(f"  Saved to {dest}")
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        return False

def extract_shapefile(zip_path, tmpdir):
    """Extract the .shp, .dbf, .shx files from the zip."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(tmpdir)
    # Find the .shp file
    for f in os.listdir(tmpdir):
        if f.endswith('.shp'):
            base = f[:-4]
            return os.path.join(tmpdir, f), base
    raise FileNotFoundError("No .shp file found in zip")

def convert_to_geojson(shp_path, output_path):
    """Convert shapefile to GeoJSON using fiona."""
    import fiona

    features = []
    with fiona.open(shp_path, 'r') as src:
        # Track CRS
        crs = src.crs

        for feature in src:
            props = dict(feature['properties'])
            geom = feature['geometry']

            # Convert geometry to plain dict for JSON serialization
            if hasattr(geom, '__geo_interface__'):
                geom_dict = geom.__geo_interface__
            elif isinstance(geom, dict):
                geom_dict = geom
            else:
                geom_dict = dict(geom)

            # Census cousub data uses NAME field for town name
            name = props.get('NAME', '')

            # Clean up: remove " town" suffix, handle special cases
            name = name.replace(' town', '').replace(' Town', '').replace(' city', '').replace(' City', '')

            features.append({
                'type': 'Feature',
                'properties': {
                    'town': name,
                    'geoid': props.get('GEOID', ''),
                    'county': props.get('NAMELSAD', ''),
                },
                'geometry': geom_dict,
            })

    geojson = {
        'type': 'FeatureCollection',
        'features': features,
    }

    # CRS: skip for simplicity (Leaflet assumes WGS84/4326)

    with open(output_path, 'w') as f:
        json.dump(geojson, f)

    print(f"Wrote {len(features)} features to {output_path}")
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"File size: {size_mb:.1f} MB")
    return features

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    tmpdir = tempfile.mkdtemp(prefix="ma_towns_")
    zip_path = os.path.join(tmpdir, "ma_towns.zip")

    try:
        # Download
        if not download(CENSUS_URL, zip_path):
            print("ERROR: Download failed. Trying alternative approach.")
            return 1

        # Extract
        try:
            shp_path, base = extract_shapefile(zip_path, tmpdir)
        except FileNotFoundError as e:
            print(f"ERROR: {e}")
            return 1

        # Convert
        features = convert_to_geojson(shp_path, OUTPUT_FILE)

        print(f"\nDone! GeoJSON saved to {OUTPUT_FILE}")

        # Print sample town names
        print("\nSample town names from GeoJSON:")
        for f in features[:10]:
            print(f"  {f['properties']['town']}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
