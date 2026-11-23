#!/usr/bin/env python3
"""Process official 2026 Massachusetts general-election results for the map."""

import csv
import json
import os
from collections import defaultdict

OUTPUT_DIR = "data"
VTD_GEOJSON = os.path.join(OUTPUT_DIR, "ma_vtds.geojson")

RACES = [
    {
        "slug": "senate2026",
        "input": "PD43+__2026_U_S_Senate_General_Election_including_precincts.csv",
        "prefix": "s26",
        "featured": "tache",
        "candidates": {
            "markey": 3,
            "deaton": 4,
            "ayyadurai": 5,
            "tache": 6,
        },
        "headers": ["Edward J. Markey", "John Deaton", "Shiva Ayyadurai", "Joe Tache"],
        "election": "2026 U.S. Senate General Election",
        "candidates_label": "Edward J. Markey / John Deaton / Shiva Ayyadurai / Joe Tache",
        "source_url": "https://electionstats.state.ma.us/elections/view/173531/",
    },
    {
        "slug": "governor2026",
        "input": "PD43+__2026_Governor_General_Election_including_precincts.csv",
        "prefix": "g26",
        "featured": "james",
        "candidates": {
            "healey": 3,
            "minogue": 4,
            "james": 5,
        },
        "headers": ["Healey and Driscoll", "Minogue and Oliver", "James and Kittredge"],
        "election": "2026 Governor General Election",
        "candidates_label": "Healey and Driscoll / Minogue and Oliver / James and Kittredge",
        "source_url": "https://electionstats.state.ma.us/elections/view/173526/",
    },
]


def parse_int(value):
    return int(value.replace(",", ""))


def candidate_key(race, candidate):
    return f"{race['prefix']}_{candidate}"


def load_precincts(race):
    with open(race["input"], newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    actual_headers = [rows[0][column] for column in race["candidates"].values()]
    if actual_headers != race["headers"]:
        raise ValueError(f"Unexpected candidate columns in {race['input']}: {actual_headers}")

    precincts = []
    for row in rows[2:]:
        if len(row) < 9 or row[0].strip().upper() == "TOTALS":
            continue
        try:
            precinct = {
                "city_town": row[0].strip(),
                "ward": row[1].strip(),
                "pct": row[2].strip(),
                "all_others": parse_int(row[-3]),
                "blanks": parse_int(row[-2]),
                "total_votes": parse_int(row[-1]),
            }
            for candidate, column in race["candidates"].items():
                precinct[candidate_key(race, candidate)] = parse_int(row[column])
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid result row in {race['input']}: {row}") from exc
        precincts.append(precinct)

    return precincts


def empty_totals(race):
    totals = {"total_votes": 0, "all_others": 0, "blanks": 0, "precincts": 0}
    for candidate in race["candidates"]:
        totals[candidate_key(race, candidate)] = 0
    return totals


def add_result(totals, precinct):
    for key, value in precinct.items():
        if key not in ("city_town", "ward", "pct"):
            totals[key] += value
    totals["precincts"] += 1


def result_record(race, totals):
    record = dict(totals)
    for candidate in race["candidates"]:
        key = candidate_key(race, candidate)
        record[f"{key}_pct"] = round(
            totals[key] / totals["total_votes"] * 100, 3
        ) if totals["total_votes"] else 0
    return record


def process_race(race, precincts):
    town_totals = defaultdict(lambda: empty_totals(race))
    ward_totals = defaultdict(lambda: defaultdict(lambda: empty_totals(race)))
    statewide = empty_totals(race)

    for precinct in precincts:
        town = precinct["city_town"]
        ward = precinct["ward"]
        add_result(town_totals[town], precinct)
        add_result(ward_totals[town][ward], precinct)
        add_result(statewide, precinct)

    towns = []
    for name, totals in town_totals.items():
        record = result_record(race, totals)
        record.update({
            "town": name,
            "has_wards": len(ward_totals[name]) > 1 or "-" not in ward_totals[name],
        })
        towns.append(record)

    featured_key = candidate_key(race, race["featured"])
    featured_pct_key = f"{featured_key}_pct"
    towns.sort(key=lambda town: town[featured_pct_key], reverse=True)
    for rank, town in enumerate(towns, 1):
        town[f"{featured_key}_rank"] = rank

    wards = []
    for town, town_wards in ward_totals.items():
        for ward, totals in town_wards.items():
            record = result_record(race, totals)
            record.update({"town": town, "ward": ward})
            wards.append(record)

    summary = result_record(race, statewide)
    summary.pop("precincts")
    summary.update({
        "total_towns": len(towns),
        "total_precincts": len(precincts),
        "towns_with_wards": sum(1 for town in towns if town["has_wards"]),
        "candidates": race["candidates_label"],
        "election": race["election"],
        "source": "Massachusetts PD43+ Official Results",
        "source_url": race["source_url"],
    })

    files = {
        f"{race['slug']}_towns.json": towns,
        f"{race['slug']}_wards.json": wards,
        f"{race['slug']}_precincts.json": precincts,
        f"{race['slug']}_summary.json": summary,
    }
    for filename, data in files.items():
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Wrote {path} ({len(data)} records)")

    print(
        f"{race['election']}: {statewide[featured_key]:,} {race['featured']} votes "
        f"out of {statewide['total_votes']:,} ({summary[featured_pct_key]}%)"
    )
    print(f"Top towns by {race['featured']} vote share:")
    for town in towns[:10]:
        print(
            f"  {town[featured_key + '_rank']:3}. {town['town']:20s} "
            f"{town[featured_pct_key]:6.2f}% ({town[featured_key]:,} / {town['total_votes']:,})"
        )


def merge_vtds(race, precincts):
    with open(VTD_GEOJSON, encoding="utf-8") as f:
        geojson = json.load(f)

    prefix = race["prefix"]
    precinct_lookup = {
        (p["city_town"], p["ward"], p["pct"]): p for p in precincts
    }
    matched = 0
    for feature in geojson["features"]:
        props = feature["properties"]
        key = (props.get("town", ""), props.get("ward", ""), props.get("pct", ""))
        precinct = precinct_lookup.get(key)
        props[f"{prefix}_matched"] = precinct is not None
        if precinct is None:
            props[f"{prefix}_total_votes"] = 0
            for candidate in race["candidates"]:
                key_name = candidate_key(race, candidate)
                props[key_name] = 0
                props[f"{key_name}_pct"] = 0
        else:
            matched += 1
            props[f"{prefix}_total_votes"] = precinct["total_votes"]
            for candidate in race["candidates"]:
                key_name = candidate_key(race, candidate)
                votes = precinct[key_name]
                props[key_name] = votes
                props[f"{key_name}_pct"] = round(
                    votes / precinct["total_votes"] * 100, 3
                ) if precinct["total_votes"] else 0

    with open(VTD_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"Matched {matched} of {len(geojson['features'])} VTDs for {race['election']}")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for race in RACES:
        precincts = load_precincts(race)
        process_race(race, precincts)
        merge_vtds(race, precincts)


if __name__ == "__main__":
    main()
