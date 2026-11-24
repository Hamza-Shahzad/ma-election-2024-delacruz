#!/usr/bin/env python3
"""Analyze PSL and comparison votes in Massachusetts, 2024-2026.

Usage: python3 analyze_psl_growth.py
Output: data/psl_analysis.json

The script uses only the Python standard library. It reads the supplied PD43+
and ACS files and downloads Census Reporter income estimates and Boston
Planning Department neighborhood boundaries when run.
"""

import csv
import json
import math
import random
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = DATA / "psl_analysis.json"
INCOME_URL = "https://api.censusreporter.org/1.0/data/show/latest"
NEIGHBORHOOD_URL = (
    "https://gis.bostonplans.org/hosting/rest/services/Hosted/"
    "Boston_Neighborhood_Boundaries/FeatureServer/5/query"
)
CAPE_TOWNS = {
    "Barnstable", "Bourne", "Brewster", "Chatham", "Dennis", "Eastham",
    "Falmouth", "Harwich", "Mashpee", "Orleans", "Provincetown",
    "Sandwich", "Truro", "Wellfleet", "Yarmouth",
}
BRANCH_CITIES = {"Boston", "Cambridge", "Somerville", "Springfield", "Worcester", "Lowell"}
BRANCH_TOWNS = BRANCH_CITIES | CAPE_TOWNS


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_json(url, params):
    request = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Massachusetts-election-analysis/1.0"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def precinct_key(row):
    return row["city_town"], row["ward"], row["pct"]


def share(votes, total):
    return 100 * votes / total if total else None


def pearson(x, y, weights=None):
    if weights is None:
        mx = statistics.fmean(x)
        my = statistics.fmean(y)
        numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
        denominator = (
            sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)
        )
    else:
        total_weight = sum(weights)
        mx = sum(a * w for a, w in zip(x, weights)) / total_weight
        my = sum(b * w for b, w in zip(y, weights)) / total_weight
        numerator = sum(w * (a - mx) * (b - my) for a, b, w in zip(x, y, weights))
        denominator = (
            sum(w * (a - mx) ** 2 for a, w in zip(x, weights))
            * sum(w * (b - my) ** 2 for b, w in zip(y, weights))
        )
    return numerator / math.sqrt(denominator) if denominator else None


def rank_values(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2 + 1
        for position in range(start, end):
            ranks[order[position]] = rank
        start = end
    return ranks


def correlation_set(rows, xkey, ykey, weight_key="total26"):
    rows = [
        r for r in rows
        if r[xkey] is not None and r[ykey] is not None
        and (not weight_key or r[weight_key])
    ]
    x = [r[xkey] for r in rows]
    y = [r[ykey] for r in rows]
    weights = [r[weight_key] for r in rows] if weight_key else None
    return {
        "n": len(rows),
        "pearson": pearson(x, y),
        "spearman": pearson(rank_values(x), rank_values(y)),
        "turnout_weighted_pearson": pearson(x, y, weights) if weights else None,
    }


def solve(matrix, vector):
    n = len(vector)
    augmented = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("Singular regression matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[i][-1] for i in range(n)]


def fit_regression(rows, ykey, xkeys, weight_key=None, standardize=False):
    weights = [r[weight_key] for r in rows] if weight_key else [1.0] * len(rows)
    columns = [[r[key] for r in rows] for key in xkeys]
    y = [r[ykey] for r in rows]
    means = []
    scales = []
    for column in [y] + columns:
        mean = sum(value * weight for value, weight in zip(column, weights)) / sum(weights)
        variance = sum(weight * (value - mean) ** 2 for value, weight in zip(column, weights)) / sum(weights)
        means.append(mean)
        scales.append(math.sqrt(variance) if standardize else 1.0)
    if standardize:
        y = [(value - means[0]) / scales[0] for value in y]
        columns = [
            [(value - means[index + 1]) / scales[index + 1] for value in column]
            for index, column in enumerate(columns)
        ]
    design = [[1.0] + [column[i] for column in columns] for i in range(len(rows))]
    size = len(xkeys) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for values, outcome, weight in zip(design, y, weights):
        for i in range(size):
            xty[i] += weight * values[i] * outcome
            for j in range(size):
                xtx[i][j] += weight * values[i] * values[j]
    coefficients = solve(xtx, xty)
    predictions = [sum(c * value for c, value in zip(coefficients, values)) for values in design]
    ymean = sum(value * weight for value, weight in zip(y, weights)) / sum(weights)
    residual_ss = sum(weight * (value - prediction) ** 2 for value, prediction, weight in zip(y, predictions, weights))
    total_ss = sum(weight * (value - ymean) ** 2 for value, weight in zip(y, weights))
    return {
        "coefficients": coefficients,
        "predictions": predictions,
        "r_squared": 1 - residual_ss / total_ss,
        "means": means,
        "scales": scales,
    }


def partial_correlation(rows, xkey, ykey, controls, weight_key=None):
    required = [xkey, ykey] + controls
    rows = [
        r for r in rows
        if all(r[key] is not None for key in required)
        and (not weight_key or r[weight_key])
    ]
    xmodel = fit_regression(rows, xkey, controls, weight_key)
    ymodel = fit_regression(rows, ykey, controls, weight_key)
    x = [r[xkey] for r in rows]
    y = [r[ykey] for r in rows]
    xresidual = [value - prediction for value, prediction in zip(x, xmodel["predictions"])]
    yresidual = [value - prediction for value, prediction in zip(y, ymodel["predictions"])]
    weights = [r[weight_key] for r in rows] if weight_key else None
    return pearson(xresidual, yresidual, weights)


def cluster_bootstrap_correlation(rows, xkey, ykey, weight_key, cluster_key, repetitions=500):
    rng = random.Random(20261123)
    groups = defaultdict(list)
    for row in rows:
        groups[row[cluster_key]].append(row)
    clusters = list(groups.values())
    estimates = []
    for _ in range(repetitions):
        sampled = [row for cluster in (rng.choice(clusters) for _ in clusters) for row in cluster]
        estimate = pearson(
            [r[xkey] for r in sampled],
            [r[ykey] for r in sampled],
            [r[weight_key] for r in sampled] if weight_key else None,
        )
        if estimate is not None:
            estimates.append(estimate)
    estimates.sort()
    return {
        "cluster_count": len(clusters),
        "repetitions": len(estimates),
        "lower_95": estimates[int(0.025 * (len(estimates) - 1))],
        "upper_95": estimates[int(0.975 * (len(estimates) - 1))],
    }


def cluster_bootstrap_regression(rows, ykey, xkeys, weight_key, cluster_key, repetitions=500):
    rng = random.Random(20261124)
    groups = defaultdict(list)
    for row in rows:
        groups[row[cluster_key]].append(row)
    clusters = list(groups.values())
    estimates = []
    for _ in range(repetitions):
        sampled = [row for cluster in (rng.choice(clusters) for _ in clusters) for row in cluster]
        model = fit_regression(sampled, ykey, xkeys, weight_key, standardize=True)
        estimates.append(model["coefficients"][1:])
    intervals = {}
    for index, key in enumerate(xkeys):
        values = sorted(estimate[index] for estimate in estimates)
        intervals[key] = {
            "lower_95": values[int(0.025 * (len(values) - 1))],
            "upper_95": values[int(0.975 * (len(values) - 1))],
        }
    return {
        "cluster_count": len(clusters),
        "repetitions": repetitions,
        "standardized_coefficient_intervals": intervals,
    }


def aggregate(rows, name, town=None):
    totals = defaultdict(float)
    for row in rows:
        for field in (
            "total24", "psl24", "stein24", "harris24", "trump24",
            "total26", "tache26", "james26", "markey26", "primary_total",
            "primary_markey", "primary_moulton",
        ):
            totals[field] += row.get(field, 0) or 0
    result = dict(totals)
    result.update({
        "name": name,
        "town": town,
        "precincts": len(rows),
        "psl24_pct": share(totals["psl24"], totals["total24"]),
        "stein24_pct": share(totals["stein24"], totals["total24"]),
        "harris24_pct": share(totals["harris24"], totals["total24"]),
        "tache26_pct": share(totals["tache26"], totals["total26"]),
        "james26_pct": share(totals["james26"], totals["total26"]),
        "markey26_pct": share(totals["markey26"], totals["total26"]),
        "markey_primary_two_party_pct": share(
            totals["primary_markey"], totals["primary_markey"] + totals["primary_moulton"]
        ),
        "psl_share_change_pp": share(totals["tache26"], totals["total26"]) - share(totals["psl24"], totals["total24"]),
        "psl_vote_change": totals["tache26"] - totals["psl24"],
        "votes_above_statewide_rate": totals["tache26"] - 59097 / 2749609 * totals["total26"],
    })
    return result


def geometry_polygons(geometry):
    if "rings" in geometry:
        return [geometry["rings"]]
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError(f"Unsupported geometry: {geometry.get('type')}")


def ring_area_centroid(ring):
    twice_area = 0.0
    x_sum = 0.0
    y_sum = 0.0
    for first, second in zip(ring, ring[1:] + ring[:1]):
        cross = first[0] * second[1] - second[0] * first[1]
        twice_area += cross
        x_sum += (first[0] + second[0]) * cross
        y_sum += (first[1] + second[1]) * cross
    if abs(twice_area) < 1e-15:
        return 0.0, ring[0]
    return abs(twice_area) / 2, (
        x_sum / (3 * twice_area),
        y_sum / (3 * twice_area),
    )


def point_in_ring(point, ring):
    x, y = point
    inside = False
    for first, second in zip(ring, ring[1:] + ring[:1]):
        x1, y1 = first[:2]
        x2, y2 = second[:2]
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def point_in_polygons(point, polygons):
    for polygon in polygons:
        if point_in_ring(point, polygon[0]) and not any(
            point_in_ring(point, hole) for hole in polygon[1:]
        ):
            return True
    return False


def feature_point(geometry):
    polygons = geometry_polygons(geometry)
    largest = max(polygons, key=lambda polygon: ring_area_centroid(polygon[0])[0])
    _, centroid = ring_area_centroid(largest[0])
    if point_in_polygons(centroid, polygons):
        return centroid
    xs = [point[0] for point in largest[0]]
    ys = [point[1] for point in largest[0]]
    candidates = []
    for i in range(21):
        for j in range(21):
            point = (min(xs) + (max(xs) - min(xs)) * i / 20, min(ys) + (max(ys) - min(ys)) * j / 20)
            if point_in_polygons(point, polygons):
                candidates.append(point)
    if not candidates:
        raise ValueError("Could not locate an interior polygon point")
    return min(candidates, key=lambda point: (point[0] - centroid[0]) ** 2 + (point[1] - centroid[1]) ** 2)


def bounds(polygons):
    points = [point for polygon in polygons for ring in polygon for point in ring]
    return (
        min(point[0] for point in points), min(point[1] for point in points),
        max(point[0] for point in points), max(point[1] for point in points),
    )


def locate(point, indexed_features):
    for properties, polygons, feature_bounds in indexed_features:
        xmin, ymin, xmax, ymax = feature_bounds
        if xmin <= point[0] <= xmax and ymin <= point[1] <= ymax and point_in_polygons(point, polygons):
            return properties
    return None


def indexed(features, esri=False):
    result = []
    for feature in features:
        geometry = feature["geometry"]
        properties = feature.get("attributes", {}) if esri else feature.get("properties", {})
        polygons = geometry_polygons(geometry)
        result.append((properties, polygons, bounds(polygons)))
    return result


def election_rows():
    presidential = load_json(DATA / "precincts.json")
    senate = load_json(DATA / "senate2026_precincts.json")
    governor = load_json(DATA / "governor2026_precincts.json")
    primary = load_json(DATA / "primary2026_precincts.json")
    lookups = []
    for rows in (presidential, senate, governor, primary):
        lookup = {precinct_key(row): row for row in rows}
        assert len(lookup) == len(rows)
        lookups.append(lookup)
    p, s, g, d = lookups
    common = set(p) & set(s) & set(g) & set(d)
    rows = []
    for key in sorted(common):
        old = p[key]
        new = s[key]
        gov = g[key]
        dem = d[key]
        assert new["total_votes"] == gov["total_votes"]
        rows.append({
            "key": key,
            "cluster": key[0],
            "town": key[0],
            "ward": key[1],
            "pct": key[2],
            "total24": old["total_votes"],
            "psl24": old["de_la_cruz"],
            "stein24": old["stein"],
            "harris24": old["harris"],
            "trump24": old["trump"],
            "total26": new["total_votes"],
            "tache26": new["s26_tache"],
            "james26": gov["g26_james"],
            "markey26": new["s26_markey"],
            "ayyadurai26": new["s26_ayyadurai"],
            "primary_total": dem["total_votes"],
            "primary_markey": dem["markey"],
            "primary_moulton": dem["moulton"],
        })
    for row in rows:
        for candidate, total in (
            ("psl24", "total24"), ("stein24", "total24"),
            ("harris24", "total24"), ("trump24", "total24"),
            ("tache26", "total26"), ("james26", "total26"),
            ("markey26", "total26"), ("ayyadurai26", "total26"),
        ):
            row[candidate + "_pct"] = share(row[candidate], row[total])
        row["markey_primary_two_party_pct"] = share(
            row["primary_markey"], row["primary_markey"] + row["primary_moulton"]
        )
        row["turnout_ratio"] = row["total26"] / row["total24"] if row["total24"] else None
        row["psl_share_change_pp"] = (
            row["tache26_pct"] - row["psl24_pct"]
            if row["total24"] and row["total26"] else None
        )
    return rows, lookups


def validate_csv_totals():
    specifications = [
        ("PD43+__2024_President_General_Election_including_precincts.csv", 14),
        ("PD43+__2026_U_S_Senate_General_Election_including_precincts.csv", 10),
        ("PD43+__2026_Governor_General_Election_including_precincts.csv", 9),
        ("PD43+__2026_U_S_Senate_Democratic_Primary_including_precincts.csv", 8),
    ]
    checked = 0
    for filename, width in specifications:
        with open(ROOT / filename, newline="", encoding="utf-8-sig") as f:
            records = list(csv.reader(f))
        for row in records[2:]:
            if len(row) < width or row[0].strip().upper() == "TOTALS":
                continue
            values = [int(value.replace(",", "")) for value in row[3:width]]
            assert sum(values[:-1]) == values[-1], (filename, row[:3])
            checked += 1
    return checked


def main():
    rows, lookups = election_rows()
    paired = [row for row in rows if row["total24"] and row["total26"]]
    by_town = defaultdict(list)
    for row in rows:
        by_town[row["town"]].append(row)
    towns = [aggregate(group, name, name) for name, group in by_town.items()]

    statewide = {
        "turnout_2024": 3512930,
        "turnout_2026": 2749609,
        "psl_votes_2024": 12889,
        "psl_votes_2026": 59097,
        "psl_pct_2024": 100 * 12889 / 3512930,
        "psl_pct_2026": 100 * 59097 / 2749609,
        "stein_votes_2024": 26545,
        "stein_pct_2024": 100 * 26545 / 3512930,
        "james_votes_2026": 89951,
        "james_pct_2026": 100 * 89951 / 2749609,
    }
    statewide.update({
        "psl_vote_change": 59097 - 12889,
        "psl_vote_growth_pct": 100 * (59097 / 12889 - 1),
        "psl_vote_multiple": 59097 / 12889,
        "psl_share_change_pp": 100 * 59097 / 2749609 - 100 * 12889 / 3512930,
        "psl_share_multiple": (59097 / 2749609) / (12889 / 3512930),
        "turnout_change_pct": 100 * (2749609 / 3512930 - 1),
        "psl_green_votes_2024": 12889 + 26545,
        "psl_green_pct_2024": 100 * (12889 + 26545) / 3512930,
        "psl_green_votes_2026": 59097 + 89951,
        "psl_green_pct_2026": 100 * (59097 + 89951) / 2749609,
        "psl_share_of_two_candidate_totals_2024": 100 * 12889 / (12889 + 26545),
        "psl_share_of_two_candidate_totals_2026": 100 * 59097 / (59097 + 89951),
    })

    pair_names = [
        ("psl24_pct", "tache26_pct"),
        ("stein24_pct", "james26_pct"),
        ("stein24_pct", "tache26_pct"),
        ("psl24_pct", "james26_pct"),
        ("tache26_pct", "james26_pct"),
        ("tache26_pct", "markey_primary_two_party_pct"),
        ("tache26_pct", "markey26_pct"),
        ("tache26_pct", "harris24_pct"),
        ("james26_pct", "harris24_pct"),
        ("psl24_pct", "stein24_pct"),
    ]
    correlations = {
        f"{x}__{y}": correlation_set(paired, x, y)
        for x, y in pair_names
    }
    partials = {}
    for x, y, controls in [
        ("psl24_pct", "tache26_pct", ["harris24_pct"]),
        ("stein24_pct", "tache26_pct", ["psl24_pct", "harris24_pct"]),
        ("tache26_pct", "james26_pct", ["harris24_pct"]),
        ("tache26_pct", "markey_primary_two_party_pct", ["harris24_pct"]),
        ("tache26_pct", "markey_primary_two_party_pct", ["markey26_pct"]),
    ]:
        label = f"{x}__{y}__controlling_{'+'.join(controls)}"
        partials[label] = {
            "unweighted": partial_correlation(paired, x, y, controls),
            "turnout_weighted": partial_correlation(paired, x, y, controls, "total26"),
        }

    turnout_order = sorted(paired, key=lambda row: row["turnout_ratio"])
    turnout_quintiles = []
    for index in range(5):
        group = turnout_order[index * len(turnout_order) // 5:(index + 1) * len(turnout_order) // 5]
        turnout_quintiles.append({
            "quintile": index + 1,
            "mean_turnout_retention_ratio": statistics.fmean(row["turnout_ratio"] for row in group),
            "mean_psl_share_change_pp": statistics.fmean(row["psl_share_change_pp"] for row in group),
        })
    turnout_robustness = {
        "correlation": correlation_set(paired, "turnout_ratio", "psl_share_change_pp", None),
        "turnout_retention_quintiles": turnout_quintiles,
    }

    concentration = {}
    for candidate, year in (("psl24", 2024), ("tache26", 2026)):
        ordered = sorted(towns, key=lambda row: row[candidate], reverse=True)
        total = sum(row[candidate] for row in towns)
        concentration[str(year)] = {
            "top_3_vote_share_pct": 100 * sum(row[candidate] for row in ordered[:3]) / total,
            "top_10_vote_share_pct": 100 * sum(row[candidate] for row in ordered[:10]) / total,
            "municipality_hhi": sum((row[candidate] / total) ** 2 for row in towns),
            "top_10": [{"town": row["town"], "votes": row[candidate]} for row in ordered[:10]],
        }
    branch_rows = [row for row in rows if row["town"] in BRANCH_TOWNS]
    nonbranch_rows = [row for row in rows if row["town"] not in BRANCH_TOWNS]
    branch_comparison = {
        "organized_core": aggregate(branch_rows, "Existing branch core"),
        "elsewhere": aggregate(nonbranch_rows, "Elsewhere"),
    }

    branch_summary = []
    for name in ("Boston", "Cambridge", "Somerville", "Springfield", "Worcester", "Lowell"):
        branch_summary.append(aggregate(by_town[name], name, name))
    cape_rows = [row for row in rows if row["town"] in CAPE_TOWNS]
    branch_summary.append(aggregate(cape_rows, "Cape Cod"))
    branch_summary.sort(key=lambda row: row["tache26_pct"], reverse=True)
    boston_ward_proxies = []
    for name, wards in (("Allston-Brighton (Wards 21-22)", {"21", "22"}), ("East Boston (Ward 1)", {"1"})):
        selected = [row for row in by_town["Boston"] if row["ward"] in wards]
        boston_ward_proxies.append(aggregate(selected, name, "Boston"))

    ward_hotspots = {}
    for town in ("Boston", "Cambridge", "Somerville", "Springfield", "Worcester", "Lowell"):
        by_ward = defaultdict(list)
        for row in by_town[town]:
            by_ward[row["ward"]].append(row)
        ward_hotspots[town] = sorted(
            [aggregate(group, f"Ward {ward}", town) for ward, group in by_ward.items()],
            key=lambda row: row["tache26_pct"], reverse=True,
        )

    cape_towns = sorted(
        [aggregate(by_town[name], name, name) for name in CAPE_TOWNS],
        key=lambda row: row["tache26_pct"], reverse=True,
    )
    expansion = [row for row in towns if row["town"] not in BRANCH_TOWNS and row["total26"] >= 3000]
    opportunity_model = fit_regression(
        towns, "tache26_pct", ["psl24_pct", "stein24_pct", "harris24_pct"], "total26"
    )
    for row, prediction in zip(towns, opportunity_model["predictions"]):
        row["expected_tache26_pct"] = prediction
        row["opportunity_residual_pp"] = row["tache26_pct"] - prediction
    expansion_scale = sorted(
        [
            {key: row[key] for key in (
                "town", "total26", "tache26", "tache26_pct", "psl24_pct",
                "psl_share_change_pp", "votes_above_statewide_rate",
            )}
            for row in expansion
        ],
        key=lambda row: row["votes_above_statewide_rate"], reverse=True,
    )
    emerging_residuals = [
        row for row in expansion
        if row["total26"] >= 5000
        and row["tache26_pct"] >= statewide["psl_pct_2026"]
        and row["votes_above_statewide_rate"] >= 25
        and row["opportunity_residual_pp"] > 0
    ]
    opportunities = {
        "model_r_squared": opportunity_model["r_squared"],
        "expansion_scale": expansion_scale,
        "emerging_residuals": sorted(
            [
                {key: row[key] for key in (
                    "town", "total26", "tache26", "tache26_pct", "psl24_pct",
                    "psl_share_change_pp", "expected_tache26_pct", "opportunity_residual_pp",
                    "votes_above_statewide_rate",
                )}
                for row in emerging_residuals
            ],
            key=lambda row: row["opportunity_residual_pp"], reverse=True,
        ),
        "scale": sorted(
            [
                {key: row[key] for key in (
                    "town", "total26", "tache26", "tache26_pct", "psl24_pct",
                    "psl_share_change_pp", "votes_above_statewide_rate",
                )}
                for row in towns
            ],
            key=lambda row: row["votes_above_statewide_rate"], reverse=True,
        )[:30],
        "expansion_residuals": sorted(
            [
                {key: row[key] for key in (
                    "town", "total26", "tache26", "tache26_pct", "psl24_pct",
                    "psl_share_change_pp", "expected_tache26_pct", "opportunity_residual_pp",
                    "votes_above_statewide_rate",
                )}
                for row in expansion
            ],
            key=lambda row: row["opportunity_residual_pp"], reverse=True,
        )[:30],
    }

    vtds = load_json(DATA / "ma_vtds.geojson")["features"]
    race_features = load_json(DATA / "ma_tracts_race.geojson")["features"]
    age_features = load_json(DATA / "ma_tracts_age.geojson")["features"]
    age_by_geoid = {feature["properties"]["geoid"]: feature["properties"] for feature in age_features}
    tract_index = indexed(race_features)
    p, s, g, d = lookups
    tract_votes = defaultdict(list)
    located_vtds = []
    for feature in vtds:
        props = feature["properties"]
        key = (props.get("town", ""), props.get("ward", ""), props.get("pct", ""))
        if key not in p or key not in s:
            continue
        point = feature_point(feature["geometry"])
        tract = locate(point, tract_index)
        row = next(row for row in rows if row["key"] == key)
        if tract:
            tract_votes[tract["geoid"]].append(row)
        located_vtds.append((feature, row, point))

    income_response = fetch_json(INCOME_URL, {
        "table_ids": "B19013", "geo_ids": "140|04000US25",
    })
    incomes = {}
    for geoid, tables in income_response["data"].items():
        estimate = tables["B19013"]["estimate"].get("B19013001")
        if estimate is not None and estimate > 0:
            incomes[geoid.removeprefix("14000US")] = estimate

    tract_rows = []
    for geoid, votes in tract_votes.items():
        result = aggregate(votes, geoid)
        if result["total26"] < 200 or not result["total24"] or not result["primary_markey"] + result["primary_moulton"]:
            continue
        race = next(feature["properties"] for feature in race_features if feature["properties"]["geoid"] == geoid)
        age = age_by_geoid[geoid]
        result.update({
            "geoid": geoid,
            "cluster": race["town"] or geoid,
            "town": race["town"],
            "population": race["total_pop"],
            "median_income": incomes.get(geoid),
            "log_median_income": math.log(incomes[geoid]) if geoid in incomes else None,
        })
        for key in ("white_pct", "black_pct", "hispanic_pct", "asian_pct", "multi_pct"):
            result[key] = race[key]
        for key in ("under18_pct", "age18_24_pct", "age25_34_pct", "age35_44_pct", "age45_54_pct", "age55_64_pct", "age65plus_pct"):
            result[key] = age[key]
        if result["median_income"]:
            tract_rows.append(result)

    demographic_keys = [
        "log_median_income", "white_pct", "black_pct", "hispanic_pct", "asian_pct",
        "age18_24_pct", "age25_34_pct", "age35_44_pct", "age65plus_pct",
    ]
    demographic_correlations = {}
    for key in demographic_keys:
        demographic_correlations[key] = {
            "tache26": correlation_set(tract_rows, key, "tache26_pct"),
            "psl24": correlation_set(tract_rows, key, "psl24_pct"),
            "james26": correlation_set(tract_rows, key, "james26_pct"),
            "tache26_partial_harris": partial_correlation(
                tract_rows, key, "tache26_pct", ["harris24_pct"], "total26"
            ),
        }

    model_keys = [
        "harris24_pct", "log_median_income", "black_pct", "hispanic_pct", "asian_pct",
        "age18_24_pct", "age25_34_pct", "age65plus_pct",
    ]
    demographic_model = fit_regression(
        tract_rows, "tache26_pct", model_keys, "total26", standardize=True
    )
    vifs = {}
    for index, key in enumerate(model_keys):
        other_keys = [other for other in model_keys if other != key]
        vifs[key] = 1 / (1 - fit_regression(tract_rows, key, other_keys, "total26")["r_squared"])
    persistence_model = fit_regression(
        tract_rows, "tache26_pct", ["psl24_pct", "stein24_pct", "harris24_pct"], "total26", standardize=True
    )

    town_demographics = []
    for town in towns:
        local_tracts = [feature["properties"] for feature in race_features if feature["properties"].get("town") == town["town"]]
        if not local_tracts:
            continue
        population = sum(feature["total_pop"] for feature in local_tracts)
        result = dict(town)
        result["population"] = population
        for key in demographic_keys:
            if key == "log_median_income":
                values = [(math.log(incomes[feature["geoid"]]), feature["total_pop"]) for feature in local_tracts if feature["geoid"] in incomes]
                result[key] = sum(value * weight for value, weight in values) / sum(weight for _, weight in values) if values else None
            else:
                result[key] = sum(
                    (feature[key] if key in feature else age_by_geoid[feature["geoid"]][key])
                    * feature["total_pop"]
                    for feature in local_tracts
                ) / population
        if result["log_median_income"] is not None:
            town_demographics.append(result)
    town_sensitivity = {
        key: pearson(
            [row[key] for row in town_demographics],
            [row["tache26_pct"] for row in town_demographics],
            [row["total26"] for row in town_demographics],
        )
        for key in demographic_keys
    }

    neighborhood_response = fetch_json(NEIGHBORHOOD_URL, {
        "where": "1=1", "outFields": "name,neighborhood_id", "returnGeometry": "true",
        "outSR": "4326", "f": "json",
    })
    neighborhood_index = indexed(neighborhood_response["features"], esri=True)
    neighborhood_votes = defaultdict(list)
    boston_vtds = 0
    for feature, row, point in located_vtds:
        if row["town"] != "Boston":
            continue
        boston_vtds += 1
        neighborhood = locate(point, neighborhood_index)
        if neighborhood:
            neighborhood_votes[neighborhood["name"]].append(row)
    neighborhoods = [aggregate(group, name, "Boston") for name, group in neighborhood_votes.items()]
    allston_brighton = [
        row for name, group in neighborhood_votes.items() if name in {"Allston", "Brighton"} for row in group
    ]
    neighborhoods.append(aggregate(allston_brighton, "Allston-Brighton", "Boston"))
    neighborhoods = sorted(neighborhoods, key=lambda row: row["tache26_pct"], reverse=True)

    demographic_model_bootstrap = cluster_bootstrap_regression(
        tract_rows, "tache26_pct", model_keys, "total26", "cluster"
    )
    persistence_bootstrap = cluster_bootstrap_correlation(
        paired, "psl24_pct", "tache26_pct", "total26", "cluster"
    )
    coverage = {
        "common_precincts": len(rows),
        "paired_nonzero_precincts": len(paired),
        "csv_rows_validated": validate_csv_totals(),
        "vtd_features": len(vtds),
        "paired_vtds": len(located_vtds),
        "paired_vtd_2026_votes": sum(row["total26"] for _, row, _ in located_vtds),
        "paired_vtd_2026_vote_coverage_pct": 100 * sum(row["total26"] for _, row, _ in located_vtds) / 2749609,
        "tract_matched_vtds": sum(len(group) for group in tract_votes.values()),
        "tract_analysis_rows": len(tract_rows),
        "tract_analysis_2026_votes": sum(row["total26"] for row in tract_rows),
        "town_demographic_rows": len(town_demographics),
        "income_release": income_response["release"],
        "boston_vtds": boston_vtds,
        "boston_neighborhood_matched_vtds": sum(len(group) for group in neighborhood_votes.values()),
        "boston_neighborhood_2026_vote_coverage_pct": 100 * sum(row["total26"] for group in neighborhood_votes.values() for row in group) / 201895,
    }

    output = {
        "method": {
            "candidate_denominator": "Total votes cast, including blanks",
            "primary_denominator": "Markey plus Moulton votes for two-party primary share",
            "race_age_source": "Local 2022 ACS five-year tract files",
            "income_source": "Census Reporter B19013, latest release recorded in coverage",
            "tract_allocation": "Each mapped VTD assigned by an interior point to one census tract; tract rows require at least 200 2026 ballots and valid income",
            "boston_allocation": "Mapped VTD interior points joined to Boston Planning Department neighborhood boundaries; these are planning boundaries, not official neighborhood boundaries",
            "opportunity_filter": "Emerging places are outside branch-core municipalities and have at least 5,000 ballots, an above-state PSL share, at least 25 votes above the statewide-rate expectation, and a positive residual from the weighted 2024 PSL/Stein/Harris baseline model",
            "inference": "Full vote counts are descriptive, not samples. Cluster bootstrap resamples municipalities. Demographic results are ecological and do not identify individual voters.",
        },
        "coverage": coverage,
        "statewide": statewide,
        "persistence": {
            "correlation": correlations["psl24_pct__tache26_pct"],
            "municipality_cluster_bootstrap_weighted_pearson": persistence_bootstrap,
            "share_increased": sum(row["tache26_pct"] > row["psl24_pct"] for row in paired),
            "share_same": sum(row["tache26_pct"] == row["psl24_pct"] for row in paired),
            "share_declined": sum(row["tache26_pct"] < row["psl24_pct"] for row in paired),
            "zero_psl_2024": sum(row["psl24"] == 0 for row in paired),
            "zero_psl_2026": sum(row["tache26"] == 0 for row in paired),
            "zero_both": sum(row["psl24"] == 0 and row["tache26"] == 0 for row in paired),
            "zero_2024_nonzero_2026": sum(row["psl24"] == 0 and row["tache26"] > 0 for row in paired),
            "nonzero_2024_zero_2026": sum(row["psl24"] > 0 and row["tache26"] == 0 for row in paired),
            "precincts_at_least_2pct_2024": sum(row["psl24_pct"] >= 2 for row in paired),
            "precincts_at_least_2pct_2026": sum(row["tache26_pct"] >= 2 for row in paired),
            "precincts_at_least_5pct_2024": sum(row["psl24_pct"] >= 5 for row in paired),
            "precincts_at_least_5pct_2026": sum(row["tache26_pct"] >= 5 for row in paired),
            "municipalities_at_least_1pct_2024": sum(row["psl24_pct"] >= 1 for row in towns),
            "municipalities_at_least_1pct_2026": sum(row["tache26_pct"] >= 1 for row in towns),
            "municipalities_at_least_2pct_2024": sum(row["psl24_pct"] >= 2 for row in towns),
            "municipalities_at_least_2pct_2026": sum(row["tache26_pct"] >= 2 for row in towns),
            "municipalities_at_least_3pct_2024": sum(row["psl24_pct"] >= 3 for row in towns),
            "municipalities_at_least_3pct_2026": sum(row["tache26_pct"] >= 3 for row in towns),
        },
        "turnout_robustness": turnout_robustness,
        "concentration": concentration,
        "branch_comparison": branch_comparison,
        "psl_green_municipalities": {
            "psl_outpolled_james": sum(row["tache26"] > row["james26"] for row in towns),
            "tied": sum(row["tache26"] == row["james26"] for row in towns),
            "james_outpolled_psl": sum(row["tache26"] < row["james26"] for row in towns),
            "largest_psl_advantages": [
                {
                    "town": row["town"], "tache26": row["tache26"], "james26": row["james26"],
                    "difference": row["tache26"] - row["james26"],
                }
                for row in sorted(towns, key=lambda row: row["tache26"] - row["james26"], reverse=True)[:15]
            ],
            "largest_james_advantages": [
                {
                    "town": row["town"], "tache26": row["tache26"], "james26": row["james26"],
                    "difference": row["james26"] - row["tache26"],
                }
                for row in sorted(towns, key=lambda row: row["james26"] - row["tache26"], reverse=True)[:15]
            ],
        },
        "correlations": correlations,
        "partial_correlations": partials,
        "branch_summary": branch_summary,
        "boston_ward_proxies": boston_ward_proxies,
        "boston_neighborhoods": neighborhoods,
        "ward_hotspots": ward_hotspots,
        "cape_towns": cape_towns,
        "opportunities": opportunities,
        "census": {
            "tract_correlations": demographic_correlations,
            "town_turnout_weighted_sensitivity": town_sensitivity,
            "demographic_model": {
                "n": len(tract_rows),
                "predictors": model_keys,
                "standardized_coefficients": demographic_model["coefficients"][1:],
                "intercept_standardized": demographic_model["coefficients"][0],
                "r_squared": demographic_model["r_squared"],
                "variance_inflation_factors": vifs,
                "municipality_cluster_bootstrap": demographic_model_bootstrap,
            },
            "persistence_model": {
                "predictors": ["psl24_pct", "stein24_pct", "harris24_pct"],
                "standardized_coefficients": persistence_model["coefficients"][1:],
                "r_squared": persistence_model["r_squared"],
            },
            "demographic_bootstrap": {
                key: cluster_bootstrap_correlation(tract_rows, key, "tache26_pct", "total26", "cluster")
                for key in ("log_median_income", "black_pct", "hispanic_pct", "asian_pct", "age18_24_pct", "age25_34_pct")
            },
        },
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    print(f"Validated {coverage['csv_rows_validated']} precinct result rows")
    print(f"PSL: {statewide['psl_votes_2024']:,} to {statewide['psl_votes_2026']:,} votes; {statewide['psl_pct_2024']:.3f}% to {statewide['psl_pct_2026']:.3f}%")
    print(f"Matched-precinct weighted Pearson r: {correlations['psl24_pct__tache26_pct']['turnout_weighted_pearson']:.3f}")
    print(f"Demographic tract rows: {len(tract_rows):,}; Boston VTD vote coverage: {coverage['boston_neighborhood_2026_vote_coverage_pct']:.1f}%")


if __name__ == "__main__":
    main()
