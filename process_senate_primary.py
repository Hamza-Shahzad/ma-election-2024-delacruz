#!/usr/bin/env python3
"""
Process PD43+ 2026 U.S. Senate Democratic Primary precinct-level CSV
(Markey vs. Moulton) into structured JSON for the interactive Massachusetts map.

Outputs:
  data/primary2026_towns.json      — town-level rollup with Markey/Moulton metrics
  data/primary2026_wards.json      — ward-level breakdown (multi-ward cities only)
  data/primary2026_precincts.json  — full precinct detail
  data/primary2026_summary.json    — statewide stats

Usage:
  python3 process_senate_primary.py
"""

import csv
import json
import os
from collections import defaultdict

INPUT_CSV = "PD43+__2026_U_S_Senate_Democratic_Primary_including_precincts.csv"
OUTPUT_DIR = "data"

# Column indices (0-based) in the CSV
COL_CITY_TOWN = 0
COL_WARD = 1
COL_PCT = 2
COL_MARKEY = 3
COL_MOULTON = 4
COL_ALL_OTHERS = 5
COL_BLANKS = 6
COL_TOTAL = 7


def parse_int(val: str) -> int:
    """Parse a comma-formatted integer string."""
    return int(val.replace(",", ""))


def main():
    # Read CSV
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Row 0: candidate names header
    # Row 1: blank sub-header
    # Row 2+: data
    data_rows = rows[2:]

    # Aggregations
    town_totals = defaultdict(lambda: {"total_votes": 0, "markey": 0, "moulton": 0, "precincts": 0})
    ward_totals = defaultdict(lambda: defaultdict(lambda: {"total_votes": 0, "markey": 0, "moulton": 0, "precincts": 0}))
    precincts = []

    statewide_total = 0
    statewide_markey = 0
    statewide_moulton = 0
    statewide_all_others = 0
    statewide_blanks = 0

    for row in data_rows:
        if len(row) < 8:
            continue

        city_town = row[COL_CITY_TOWN].strip()
        ward = row[COL_WARD].strip()
        pct = row[COL_PCT].strip()

        # Skip TOTALS row
        if city_town.upper() == "TOTALS":
            continue

        try:
            total = parse_int(row[COL_TOTAL])
            markey = parse_int(row[COL_MARKEY])
            moulton = parse_int(row[COL_MOULTON])
            all_others = parse_int(row[COL_ALL_OTHERS])
            blanks = parse_int(row[COL_BLANKS])
        except (ValueError, IndexError):
            continue

        # Town-level aggregation
        town_totals[city_town]["total_votes"] += total
        town_totals[city_town]["markey"] += markey
        town_totals[city_town]["moulton"] += moulton
        town_totals[city_town]["precincts"] += 1

        # Ward-level aggregation
        ward_totals[city_town][ward]["total_votes"] += total
        ward_totals[city_town][ward]["markey"] += markey
        ward_totals[city_town][ward]["moulton"] += moulton
        ward_totals[city_town][ward]["precincts"] += 1

        # Precinct-level detail
        precincts.append({
            "city_town": city_town,
            "ward": ward,
            "pct": pct,
            "markey": markey,
            "moulton": moulton,
            "all_others": all_others,
            "blanks": blanks,
            "total_votes": total,
        })

        statewide_total += total
        statewide_markey += markey
        statewide_moulton += moulton
        statewide_all_others += all_others
        statewide_blanks += blanks

    # Build town list with percentages and ranking
    town_list = []
    for name, d in town_totals.items():
        markey_pct = (d["markey"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
        moulton_pct = (d["moulton"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
        town_list.append({
            "town": name,
            "total_votes": d["total_votes"],
            "markey": d["markey"],
            "markey_pct": round(markey_pct, 3),
            "moulton": d["moulton"],
            "moulton_pct": round(moulton_pct, 3),
            "precincts": d["precincts"],
            "has_wards": len(ward_totals[name]) > 1 or "-" not in ward_totals[name],
        })

    # Sort by Markey % descending and assign ranks
    town_list.sort(key=lambda t: t["markey_pct"], reverse=True)
    for i, t in enumerate(town_list):
        t["markey_rank"] = i + 1

    # Assign Moulton ranks
    moulton_sorted = sorted(town_list, key=lambda t: t["moulton_pct"], reverse=True)
    for i, t in enumerate(moulton_sorted):
        t["moulton_rank"] = i + 1

    # Build ward list
    ward_list = []
    for town, wards in ward_totals.items():
        for ward, d in wards.items():
            markey_pct = (d["markey"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
            moulton_pct = (d["moulton"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
            ward_list.append({
                "town": town,
                "ward": ward,
                "total_votes": d["total_votes"],
                "markey": d["markey"],
                "markey_pct": round(markey_pct, 3),
                "moulton": d["moulton"],
                "moulton_pct": round(moulton_pct, 3),
                "precincts": d["precincts"],
            })

    # Summary
    summary = {
        "total_votes_cast": statewide_total,
        "total_markey": statewide_markey,
        "markey_pct": round(statewide_markey / statewide_total * 100, 3) if statewide_total > 0 else 0,
        "total_moulton": statewide_moulton,
        "moulton_pct": round(statewide_moulton / statewide_total * 100, 3) if statewide_total > 0 else 0,
        "total_all_others": statewide_all_others,
        "total_blanks": statewide_blanks,
        "total_towns": len(town_list),
        "total_precincts": len(precincts),
        "towns_with_wards": sum(1 for t in town_list if t["has_wards"]),
        "candidates": "Edward J. Markey / Seth W. Moulton",
        "party": "Democratic",
        "election": "2026 U.S. Senate Democratic Primary",
        "source": "Massachusetts PD43+ Official Results",
    }

    # Ensure output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Write JSON files
    files = {
        "primary2026_towns.json": town_list,
        "primary2026_wards.json": ward_list,
        "primary2026_precincts.json": precincts,
        "primary2026_summary.json": summary,
    }

    for filename, data in files.items():
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Wrote {path} ({len(data)} records)")

    # Print summary
    print(f"\nStatewide Markey: {statewide_markey:,} votes out of {statewide_total:,} ({summary['markey_pct']}%)")
    print(f"Statewide Moulton: {statewide_moulton:,} votes out of {statewide_total:,} ({summary['moulton_pct']}%)")
    print(f"Towns: {len(town_list)} | Precincts: {len(precincts)} | Towns with wards: {summary['towns_with_wards']}")

    # Top 10 towns by Markey %
    print("\nTop 10 towns by Markey %:")
    for t in town_list[:10]:
        print(f"  {t['markey_rank']:3}. {t['town']:20s}  {t['markey_pct']:6.2f}%  ({t['markey']:,} / {t['total_votes']:,})")

    # Top 10 towns by Moulton %
    moulton_top = sorted(town_list, key=lambda t: t['moulton_pct'], reverse=True)
    print("\nTop 10 towns by Moulton %:")
    for t in moulton_top[:10]:
        print(f"  {t['moulton_rank']:3}. {t['town']:20s}  {t['moulton_pct']:6.2f}%  ({t['moulton']:,} / {t['total_votes']:,})")


if __name__ == "__main__":
    main()
