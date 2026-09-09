#!/usr/bin/env python3
"""
Merge 2026 U.S. Senate Democratic Primary (Markey vs. Moulton) precinct
results into the existing VTD GeoJSON (data/ma_vtds.geojson).

Each VTD feature already carries the PD43+ precinct key (town, ward, pct)
from the 2024 presidential join, so the primary results are matched on
that same key — no re-download or spatial join needed.

Adds these properties to every feature:
  p26_matched      — whether a 2026 primary precinct row was found
  p26_total_votes  — total votes cast in the primary
  markey, markey_pct
  moulton, moulton_pct

Usage:
  python3 merge_senate_primary_vtd.py
"""

import csv
import json
import os

INPUT_CSV = "PD43+__2026_U_S_Senate_Democratic_Primary_including_precincts.csv"
VTD_GEOJSON = os.path.join("data", "ma_vtds.geojson")

# Column indices (0-based) in the CSV
COL_CITY_TOWN = 0
COL_WARD = 1
COL_PCT = 2
COL_MARKEY = 3
COL_MOULTON = 4
COL_TOTAL = 7


def parse_int(val: str) -> int:
    """Parse a comma-formatted integer string."""
    return int(val.replace(",", ""))


def main():
    # Load primary precinct data
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    precinct_data = {}  # (town, ward, pct) → vote counts
    for row in rows[2:]:  # Skip header + blank sub-header
        if len(row) < 8:
            continue
        town = row[COL_CITY_TOWN].strip()
        if town.upper() == "TOTALS":
            continue
        ward = row[COL_WARD].strip()
        pct = row[COL_PCT].strip()
        try:
            total = parse_int(row[COL_TOTAL])
            markey = parse_int(row[COL_MARKEY])
            moulton = parse_int(row[COL_MOULTON])
        except (ValueError, IndexError):
            continue
        precinct_data[(town, ward, pct)] = {
            "total_votes": total,
            "markey": markey,
            "markey_pct": round(markey / total * 100, 3) if total > 0 else 0,
            "moulton": moulton,
            "moulton_pct": round(moulton / total * 100, 3) if total > 0 else 0,
        }

    print(f"Loaded {len(precinct_data)} primary precincts from CSV")

    # Load VTD GeoJSON and join
    with open(VTD_GEOJSON) as f:
        geojson = json.load(f)

    matched = 0
    for feature in geojson["features"]:
        props = feature["properties"]
        key = (props.get("town", ""), props.get("ward", ""), props.get("pct", ""))
        pd = precinct_data.get(key)
        if pd is None:
            props["p26_matched"] = False
            props["p26_total_votes"] = 0
            props["markey"] = 0
            props["markey_pct"] = 0
            props["moulton"] = 0
            props["moulton_pct"] = 0
        else:
            matched += 1
            props["p26_matched"] = True
            props["p26_total_votes"] = pd["total_votes"]
            props["markey"] = pd["markey"]
            props["markey_pct"] = pd["markey_pct"]
            props["moulton"] = pd["moulton"]
            props["moulton_pct"] = pd["moulton_pct"]

    with open(VTD_GEOJSON, "w") as f:
        json.dump(geojson, f)

    size_mb = os.path.getsize(VTD_GEOJSON) / (1024 * 1024)
    print(f"Matched {matched} of {len(geojson['features'])} VTD features to 2026 primary results")
    print(f"Updated {VTD_GEOJSON} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
