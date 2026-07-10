#!/usr/bin/env python3
"""
Process PD43+ 2024 Presidential General Election precinct-level CSV
into structured JSON for the interactive Massachusetts map.

Outputs:
  data/towns.json      — town-level rollup with De La Cruz metrics
  data/wards.json      — ward-level breakdown (multi-ward cities only)
  data/precincts.json  — full precinct detail
  data/summary.json    — statewide stats

Usage:
  python3 process_data.py
"""

import csv
import json
import os
from collections import defaultdict

INPUT_CSV = "PD43+__2024_President_General_Election_including_precincts.csv"
OUTPUT_DIR = "data"

# Column indices (0-based) in the CSV
COL_CITY_TOWN = 0
COL_WARD = 1
COL_PCT = 2
COL_HARRIS = 3
COL_TRUMP = 4
COL_STEIN = 5
COL_AYYADURAI = 6
COL_OLIVER = 7
COL_DE_LA_CRUZ = 8
COL_SONSKI = 9
COL_WEST = 10
COL_ALL_OTHERS = 11
COL_BLANKS = 12
COL_TOTAL = 13


def parse_int(val: str) -> int:
    """Parse a comma-formatted integer string."""
    return int(val.replace(",", ""))


def main():
    # Read CSV
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Row 0: candidate names header
    # Row 1: party affiliations (sub-header)
    # Row 2+: data
    data_rows = rows[2:]

    # Aggregations
    town_totals = defaultdict(lambda: {"total_votes": 0, "de_la_cruz": 0, "precincts": 0})
    ward_totals = defaultdict(lambda: defaultdict(lambda: {"total_votes": 0, "de_la_cruz": 0, "precincts": 0}))
    precincts = []

    statewide_total = 0
    statewide_delacruz = 0

    for row in data_rows:
        if len(row) < 14:
            continue

        city_town = row[COL_CITY_TOWN].strip()
        ward = row[COL_WARD].strip()
        pct = row[COL_PCT].strip()

        # Skip TOTALS row
        if city_town.upper() == "TOTALS":
            continue

        try:
            total = parse_int(row[COL_TOTAL])
            delacruz = parse_int(row[COL_DE_LA_CRUZ])
            harris = parse_int(row[COL_HARRIS])
            trump = parse_int(row[COL_TRUMP])
            stein = parse_int(row[COL_STEIN])
            oliver = parse_int(row[COL_OLIVER])
            west = parse_int(row[COL_WEST])
            all_others = parse_int(row[COL_ALL_OTHERS])
            blanks = parse_int(row[COL_BLANKS])
        except (ValueError, IndexError):
            continue

        # Town-level aggregation
        town_totals[city_town]["total_votes"] += total
        town_totals[city_town]["de_la_cruz"] += delacruz
        town_totals[city_town]["precincts"] += 1

        # Ward-level aggregation
        ward_totals[city_town][ward]["total_votes"] += total
        ward_totals[city_town][ward]["de_la_cruz"] += delacruz
        ward_totals[city_town][ward]["precincts"] += 1

        # Precinct-level detail
        precincts.append({
            "city_town": city_town,
            "ward": ward,
            "pct": pct,
            "harris": harris,
            "trump": trump,
            "stein": stein,
            "oliver": oliver,
            "de_la_cruz": delacruz,
            "west": west,
            "all_others": all_others,
            "blanks": blanks,
            "total_votes": total,
        })

        statewide_total += total
        statewide_delacruz += delacruz

    # Build town list with percentages and ranking
    town_list = []
    for name, d in town_totals.items():
        pct = (d["de_la_cruz"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
        town_list.append({
            "town": name,
            "total_votes": d["total_votes"],
            "de_la_cruz": d["de_la_cruz"],
            "de_la_cruz_pct": round(pct, 3),
            "precincts": d["precincts"],
            "has_wards": len(ward_totals[name]) > 1 or "-" not in ward_totals[name],
        })

    # Sort by De La Cruz % descending
    town_list.sort(key=lambda t: t["de_la_cruz_pct"], reverse=True)

    # Assign ranks
    for i, t in enumerate(town_list):
        t["rank"] = i + 1

    # Build ward list
    ward_list = []
    for town, wards in ward_totals.items():
        for ward, d in wards.items():
            pct = (d["de_la_cruz"] / d["total_votes"] * 100) if d["total_votes"] > 0 else 0
            ward_list.append({
                "town": town,
                "ward": ward,
                "total_votes": d["total_votes"],
                "de_la_cruz": d["de_la_cruz"],
                "de_la_cruz_pct": round(pct, 3),
                "precincts": d["precincts"],
            })

    # Build precinct list (already aggregated above)

    # Summary
    summary = {
        "total_votes_cast": statewide_total,
        "total_de_la_cruz": statewide_delacruz,
        "de_la_cruz_pct": round(statewide_delacruz / statewide_total * 100, 3) if statewide_total > 0 else 0,
        "total_towns": len(town_list),
        "total_precincts": len(precincts),
        "towns_with_wards": sum(1 for t in town_list if t["has_wards"]),
        "candidate": "Claudia De La Cruz / Karina Garcia",
        "party": "Peace and Freedom / PSL",
        "election": "2024 Presidential General Election",
        "source": "Massachusetts PD43+ Official Results",
    }

    # Ensure output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Write JSON files
    files = {
        "towns.json": town_list,
        "wards.json": ward_list,
        "precincts.json": precincts,
        "summary.json": summary,
    }

    for filename, data in files.items():
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Wrote {path} ({len(data)} records)")

    # Print summary
    print(f"\nStatewide: {statewide_delacruz:,} De La Cruz votes out of {statewide_total:,} ({summary['de_la_cruz_pct']}%)")
    print(f"Towns: {len(town_list)} | Precincts: {len(precincts)} | Towns with wards: {summary['towns_with_wards']}")

    # Top 10 towns
    print("\nTop 10 towns by De La Cruz %:")
    for t in town_list[:10]:
        print(f"  {t['rank']:3}. {t['town']:20s}  {t['de_la_cruz_pct']:6.2f}%  ({t['de_la_cruz']:,} / {t['total_votes']:,})")


if __name__ == "__main__":
    main()
