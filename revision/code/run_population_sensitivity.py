#!/usr/bin/env python3
"""Recompute the frozen 0–74 US/BR time contrasts under denominator vintages.

All writes are confined to revision_20261026/population/outputs. The validated
joint-pattern source and packaged bridge helper are read-only inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


HERE = Path(__file__).resolve().parents[1]
SOURCE_DIR = HERE / "sources"
OUTPUT_DIR = HERE / "outputs"

HELPER_PATH = Path(
    r"C:\Users\aaa12\Desktop\项目\（在投）美巴多死因架构（已修）"
    r"\Paper_Finalization_20261025\07_reproducibility\package\helpers\bridge\run_usbr_monitoring.py"
)
BASELINE_DIR = Path(r"D:\MCOD_Research\USBR_MCOD_bridge_decision_20261022\outputs\usbr_monitoring")
FROZEN_WEIGHTS_PATH = Path(
    r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build\data\metadata"
    r"\who_2000_2025_18_group_weights_correct.csv"
)

PATTERN_PATH = BASELINE_DIR / "usbr_joint_patterns.csv"
BASELINE_POPULATION_PATH = BASELINE_DIR / "usbr_population_denominators.csv"
BASELINE_INTERVALS_PATH = BASELINE_DIR / "usbr_primary_bootstrap_intervals.csv"
BASELINE_POINTS_PATH = BASELINE_DIR / "usbr_monitoring_point_estimates.csv"

SOURCE_FILES = {
    "intercensal_2010_2020": SOURCE_DIR / "nc-est2020int-agesex-res.csv",
    "postcensal_2020": SOURCE_DIR / "nc-est2020-agesex-res.csv",
    "postcensal_2024": SOURCE_DIR / "nc-est2024-agesex-res.csv",
    "postcensal_2025": SOURCE_DIR / "nc-est2025-agesex-res.csv",
}
SOURCE_URLS = {
    "intercensal_2010_2020": "https://www2.census.gov/programs-surveys/popest/datasets/2010-2020/intercensal/national/asrh/nc-est2020int-agesex-res.csv",
    "postcensal_2020": "https://www2.census.gov/programs-surveys/popest/datasets/2010-2020/national/asrh/nc-est2020-agesex-res.csv",
    "postcensal_2024": "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/national/asrh/nc-est2024-agesex-res.csv",
    "postcensal_2025": "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/national/asrh/nc-est2025-agesex-res.csv",
}

EXPECTED_INPUT_SHA256 = {
    "patterns": "b2c7e77d7c51438a6919787087ca146c91f779f6ca3bd2c38c490178d34b14e8",
    "baseline_population": "dab349662bf88652443ee78c1c0fc9de8b384171eb0a0d8564d7900a339d1851",
    "baseline_intervals": "4b36407eb70d06ce29b6086f399b39635353639e377a1560914376479542752f",
    "baseline_points": "eeb0c9192f8ad40a34ac993b4e679377e4e785c153c5d00d94159f6c7e9ec227",
    "packaged_helper": "8240058159d7abc1eb6057350032d5af84ff636e80143de95d30ce8d24e58e6b",
}

VARIANT_LABELS = {
    "baseline_split_2020_2024": "Frozen baseline: NC-EST2020 through 2020; NC-EST2024 for 2021–2024",
    "intercensal_plus_v2024": "NC-EST2020INT for 2012–2019; NC-EST2024 for 2020–2024",
    "intercensal_plus_v2025": "NC-EST2020INT for 2012–2019; NC-EST2025 for 2020–2024",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"Refusing to write empty CSV: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_helper():
    if not HELPER_PATH.is_file():
        raise FileNotFoundError(f"Packaged helper not found: {HELPER_PATH}")
    observed = sha256_file(HELPER_PATH)
    if observed != EXPECTED_INPUT_SHA256["packaged_helper"]:
        raise ValueError(f"Packaged helper hash mismatch: {observed}")
    spec = importlib.util.spec_from_file_location("packaged_usbr_monitoring", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load packaged monitoring helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_inputs() -> dict[str, str]:
    paths = {
        "patterns": PATTERN_PATH,
        "baseline_population": BASELINE_POPULATION_PATH,
        "baseline_intervals": BASELINE_INTERVALS_PATH,
        "baseline_points": BASELINE_POINTS_PATH,
        "packaged_helper": HELPER_PATH,
    }
    observed: dict[str, str] = {}
    for role, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Required read-only input missing: {path}")
        observed[role] = sha256_file(path)
        if observed[role] != EXPECTED_INPUT_SHA256[role]:
            raise ValueError(f"Input hash mismatch for {role}: {observed[role]}")
    for role, path in SOURCE_FILES.items():
        if not path.is_file():
            raise FileNotFoundError(f"Official Census source missing: {path}")
        observed[role] = sha256_file(path)
    return observed


def load_population(rows: list[dict[str, str]]) -> dict[tuple[str, int, str, str], int]:
    result: dict[tuple[str, int, str, str], int] = {}
    for row in rows:
        key = (row["country"], int(row["year"]), row["age_group"], row["sex"])
        if key in result:
            raise ValueError(f"Duplicate denominator cell: {key}")
        value = int(row["population"])
        if value <= 0:
            raise ValueError(f"Nonpositive denominator at {key}: {value}")
        result[key] = value
    return result


def census_age_group(age: int, age_groups: tuple[str, ...]) -> str:
    if age >= 85:
        return "85+"
    start = (age // 5) * 5
    group = f"{start}-{start + 4}"
    if group not in age_groups:
        raise ValueError(f"Age {age} maps outside frozen age grid")
    return group


def load_census_source(
    path: Path,
    years: tuple[int, ...],
    helper: Any,
) -> tuple[dict[tuple[int, str, str], int], dict[str, Any]]:
    """Aggregate official single-year ages to frozen five-year WHO18 groups."""
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"No rows in Census source {path.name}")
    columns = set(rows[0])
    for year in years:
        column = f"POPESTIMATE{year}"
        if column not in columns:
            raise ValueError(f"{path.name} lacks annual field {column}")

    by_sex_age: dict[tuple[int, int], dict[str, str]] = {}
    for row in rows:
        sex = int(row["SEX"])
        age = int(row["AGE"])
        key = (sex, age)
        if key in by_sex_age:
            raise ValueError(f"Duplicate SEX/AGE row in {path.name}: {key}")
        by_sex_age[key] = row

    expected_ages = set(range(101))
    age_rows = {age for sex, age in by_sex_age if sex in (0, 1, 2) and age != 999}
    if age_rows != expected_ages:
        raise ValueError(f"Unexpected single-year age grid in {path.name}: {sorted(age_rows)}")
    for sex in (0, 1, 2):
        if (sex, 999) not in by_sex_age:
            raise ValueError(f"{path.name} lacks total row for SEX={sex}")

    group_values: dict[tuple[int, str, str], int] = {}
    full_age_closure: list[dict[str, Any]] = []
    for year in years:
        column = f"POPESTIMATE{year}"
        sex_sums: dict[int, int] = {}
        for sex in (1, 2):
            totals = {age: int(by_sex_age[(sex, age)][column]) for age in range(101)}
            if any(value < 0 for value in totals.values()):
                raise ValueError(f"Negative estimate in {path.name}, {column}, SEX={sex}")
            sex_total = sum(totals.values())
            census_total = int(by_sex_age[(sex, 999)][column])
            if sex_total != census_total:
                raise ValueError(
                    f"Full-age closure failed in {path.name}, {column}, SEX={sex}: "
                    f"{sex_total} != {census_total}"
                )
            sex_sums[sex] = sex_total
            bins: dict[str, int] = {age: 0 for age in helper.AGE_GROUPS}
            for age, value in totals.items():
                bins[census_age_group(age, helper.AGE_GROUPS)] += value
            if sum(bins.values()) != sex_total:
                raise AssertionError("Grouped age-sex counts do not close")
            for age_group, value in bins.items():
                group_values[(year, age_group, "M" if sex == 1 else "F")] = value

        both_sex_total = int(by_sex_age[(0, 999)][column])
        if sex_sums[1] + sex_sums[2] != both_sex_total:
            raise ValueError(
                f"Male+female population does not equal SEX=0 total in {path.name}, {column}"
            )
        full_age_closure.append(
            {
                "year": year,
                "male_all_age": sex_sums[1],
                "female_all_age": sex_sums[2],
                "sex0_all_age": both_sex_total,
                "male_plus_female_equals_sex0": True,
            }
        )
    expected_cells = len(years) * len(helper.AGE_GROUPS) * len(helper.SEXES)
    if len(group_values) != expected_cells:
        raise AssertionError(f"Expected {expected_cells} source cells, got {len(group_values)}")
    return group_values, {
        "file": path.name,
        "rows": len(rows),
        "years": list(years),
        "single_year_ages": [0, 100],
        "age_group_cells": len(group_values),
        "full_age_and_sex_total_closure": full_age_closure,
    }


def make_population_variants(helper: Any) -> tuple[dict[str, dict[tuple[str, int, str, str], int]], dict[str, Any]]:
    baseline_rows = read_csv(BASELINE_POPULATION_PATH)
    baseline = load_population(baseline_rows)
    expected_keys = {
        (country, year, age, sex)
        for country in ("US", "BR")
        for year in helper.YEARS
        for age in helper.AGE_GROUPS
        for sex in helper.SEXES
    }
    if len(baseline) != 936 or set(baseline) != expected_keys:
        raise ValueError("Frozen baseline denominator table is not a complete 936-cell grid")

    intercensal, intercensal_qc = load_census_source(
        SOURCE_FILES["intercensal_2010_2020"], tuple(range(2012, 2020)), helper
    )
    old_postcensal, old_postcensal_qc = load_census_source(
        SOURCE_FILES["postcensal_2020"], tuple(range(2012, 2021)), helper
    )
    postcensal_2024, postcensal_2024_qc = load_census_source(
        SOURCE_FILES["postcensal_2024"], tuple(range(2020, 2025)), helper
    )
    postcensal_2025, postcensal_2025_qc = load_census_source(
        SOURCE_FILES["postcensal_2025"], tuple(range(2020, 2025)), helper
    )

    # Confirm that the archived baseline US cells reproduce the two source
    # series reported by m03 before changing any denominators.
    baseline_source_mismatches: list[dict[str, Any]] = []
    for key, value in baseline.items():
        country, year, age, sex = key
        if country != "US":
            continue
        if year <= 2020:
            expected = old_postcensal[(year, age, sex)]
            source_name = "nc-est2020-agesex-res.csv"
        else:
            expected = postcensal_2024[(year, age, sex)]
            source_name = "nc-est2024-agesex-res.csv"
        if value != expected:
            baseline_source_mismatches.append(
                {
                    "year": year,
                    "age_group": age,
                    "sex": sex,
                    "baseline_population": value,
                    "source_population": expected,
                    "source_file": source_name,
                }
            )
    if baseline_source_mismatches:
        raise ValueError(
            "Frozen US baseline does not match the claimed official Census vintages; "
            f"first mismatch={baseline_source_mismatches[0]}"
        )

    variants = {
        "baseline_split_2020_2024": dict(baseline),
        "intercensal_plus_v2024": dict(baseline),
        "intercensal_plus_v2025": dict(baseline),
    }
    for variant, postcensal in (
        ("intercensal_plus_v2024", postcensal_2024),
        ("intercensal_plus_v2025", postcensal_2025),
    ):
        population = variants[variant]
        for key in list(population):
            country, year, age, sex = key
            if country != "US":
                continue
            if year <= 2019:
                population[key] = intercensal[(year, age, sex)]
            else:
                population[key] = postcensal[(year, age, sex)]

    metadata = {
        "baseline_source_vintage_match": {
            "status": "PASS",
            "US_cells_checked": sum(1 for key in baseline if key[0] == "US"),
            "mismatches": 0,
            "NC-EST2020_years": "2012–2020",
            "NC-EST2024_years": "2021–2024",
        },
        "official_source_checks": {
            "intercensal_2010_2020": intercensal_qc,
            "postcensal_2020": old_postcensal_qc,
            "postcensal_2024": postcensal_2024_qc,
            "postcensal_2025": postcensal_2025_qc,
        },
        "variant_source_rules": {
            "baseline_split_2020_2024": "Frozen denominator export; no changes",
            "intercensal_plus_v2024": "US 2012–2019 NC-EST2020INT; US 2020–2024 NC-EST2024; BR frozen",
            "intercensal_plus_v2025": "US 2012–2019 NC-EST2020INT; US 2020–2024 NC-EST2025; BR frozen",
        },
    }
    return variants, metadata


def as_helper_population(values: dict[tuple[str, int, str, str], int]) -> dict[tuple[str, int, str, str], dict[str, int]]:
    return {key: {"population": value} for key, value in values.items()}


def load_pattern_data(helper: Any) -> dict[str, Any]:
    rows = read_csv(PATTERN_PATH)
    if not rows:
        raise ValueError("Joint-pattern CSV is empty")
    expected_fields = {
        "country",
        "year",
        "age_group",
        "sex",
        "ucd_mask_lower",
        "any_mask_lower",
        "malignancy_source_any_lower",
        "n_pattern",
    }
    if not expected_fields.issubset(rows[0]):
        raise ValueError(f"Joint-pattern table lacks required fields: {sorted(expected_fields - set(rows[0]))}")
    return {"rows": rows}


def compute_point_values(helper: Any, pattern_data: dict[str, Any], population: dict[tuple[str, int, str, str], int], weights: dict[str, float]) -> dict[str, float]:
    pop = as_helper_population(population)
    annual: dict[str, np.ndarray] = {}
    for country in ("US", "BR"):
        lam, coef = helper.bootstrap_coefficients(pattern_data, country, pop, weights)
        annual[country] = lam @ coef
    metrics = helper.target_metric_values(annual["US"], annual["BR"])
    primary = metrics[helper.BOOTSTRAP_TARGETS[0]]
    return {name: float(np.asarray(value).reshape(-1)[0]) for name, value in primary.items()}


def denominator_rows(variants: dict[str, dict[tuple[str, int, str, str], int]], helper: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variant, population in variants.items():
        for (country, year, age, sex), value in sorted(population.items()):
            if age not in helper.AGE_GROUPS_0_74:
                continue
            rows.append(
                {
                    "variant": variant,
                    "variant_source_rule": VARIANT_LABELS[variant],
                    "country": country,
                    "year": year,
                    "age_group": age,
                    "sex": sex,
                    "population": value,
                }
            )
    return rows


def denominator_difference_rows(variants: dict[str, dict[tuple[str, int, str, str], int]], helper: Any) -> list[dict[str, Any]]:
    baseline = variants["baseline_split_2020_2024"]
    rows: list[dict[str, Any]] = []
    for variant in ("intercensal_plus_v2024", "intercensal_plus_v2025"):
        population = variants[variant]
        for key in sorted(baseline):
            country, year, age, sex = key
            if age not in helper.AGE_GROUPS_0_74:
                continue
            base = baseline[key]
            alternate = population[key]
            difference = alternate - base
            rows.append(
                {
                    "comparison": f"{variant}_vs_baseline",
                    "variant": variant,
                    "country": country,
                    "year": year,
                    "age_group": age,
                    "sex": sex,
                    "baseline_population": base,
                    "alternate_population": alternate,
                    "difference": difference,
                    "percent_difference": difference / base * 100.0,
                }
            )
    return rows


def point_summary_rows(point_values: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    six = ("T_UCD_R", "T_any_R", "I_R", "T_UCD_A", "T_any_A", "I_A")
    units = {"T_UCD_R": "log-RR gap change", "T_any_R": "log-RR gap change", "I_R": "log-RR gap interaction", "T_UCD_A": "per 100000", "T_any_A": "per 100000", "I_A": "per 100000"}
    return [
        {
            "variant": variant,
            "variant_source_rule": VARIANT_LABELS[variant],
            "metric": metric,
            "point_estimate": point_values[variant][metric],
            "unit": units[metric],
            "estimand": "T=|signed gap late|-|signed gap early|; I=T_any-T_UCD",
        }
        for variant in point_values
        for metric in six
    ]


def run_point_stage(
    helper: Any,
    variants: dict[str, dict[tuple[str, int, str, str], int]],
    pattern_data: dict[str, Any],
    weights: dict[str, float],
    metadata: dict[str, Any],
    input_hashes: dict[str, str],
) -> dict[str, dict[str, float]]:
    point_values = {
        variant: compute_point_values(helper, pattern_data, population, weights)
        for variant, population in variants.items()
    }
    stage_dir = OUTPUT_DIR / "preinference"
    write_csv(stage_dir / "point_estimates_preinference.csv", point_summary_rows(point_values))
    write_csv(stage_dir / "denominator_variants_0_74.csv", denominator_rows(variants, helper))
    write_csv(stage_dir / "denominator_cell_differences_0_74.csv", denominator_difference_rows(variants, helper))
    write_json(
        stage_dir / "preinference_source_qc.json",
        {
            "status": "PASS",
            "input_sha256": input_hashes,
            "denominator_qc": metadata,
            "point_estimate_variants": point_values,
            "bootstrap_not_run": True,
        },
    )
    return point_values


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def primary_metric_map(helper: Any) -> dict[str, tuple[str, ...]]:
    return {
        "T_UCD_R": ("signed_logRR_UCD_early", "signed_logRR_UCD_late"),
        "T_any_R": ("signed_logRR_any_early", "signed_logRR_any_late"),
        "I_R": (
            "signed_logRR_UCD_early",
            "signed_logRR_UCD_late",
            "signed_logRR_any_early",
            "signed_logRR_any_late",
        ),
        "T_UCD_A": ("signed_RD_UCD_early", "signed_RD_UCD_late"),
        "T_any_A": ("signed_RD_any_early", "signed_RD_any_late"),
        "I_A": ("signed_RD_UCD_early", "signed_RD_UCD_late", "signed_RD_any_early", "signed_RD_any_late"),
    }


def signed_absolute_diagnostics(
    variant: str,
    seed: int,
    draw_values: dict[str, np.ndarray],
) -> list[dict[str, Any]]:
    component_for_metric = {
        "T_UCD_R": ("signed_logRR_UCD_early", "signed_logRR_UCD_late"),
        "T_any_R": ("signed_logRR_any_early", "signed_logRR_any_late"),
        "I_R": (
            "signed_logRR_UCD_early",
            "signed_logRR_UCD_late",
            "signed_logRR_any_early",
            "signed_logRR_any_late",
        ),
        "T_UCD_A": ("signed_RD_UCD_early", "signed_RD_UCD_late"),
        "T_any_A": ("signed_RD_any_early", "signed_RD_any_late"),
        "I_A": ("signed_RD_UCD_early", "signed_RD_UCD_late", "signed_RD_any_early", "signed_RD_any_late"),
    }
    rows: list[dict[str, Any]] = []
    for metric, components in component_for_metric.items():
        positive_mask = np.ones_like(draw_values[metric], dtype=bool)
        for component in components:
            positive_mask &= draw_values[component] > 0
        signed_value = draw_values[components[1]] - draw_values[components[0]] if len(components) == 2 else (
            draw_values[components[3]] - draw_values[components[2]]
            - draw_values[components[1]] + draw_values[components[0]]
        )
        difference = draw_values[metric] - signed_value
        positive_subset_max_difference = (
            float(np.max(np.abs(difference[positive_mask]))) if np.any(positive_mask) else math.nan
        )
        rows.append(
            {
                "variant": variant,
                "seed": seed,
                "metric": metric,
                "diagnostic_only": True,
                "signed_formula": "late signed gap - early signed gap; I is any minus UCD",
                "all_required_signed_gaps_positive": bool(np.all(positive_mask)),
                "draws_with_all_required_signed_gaps_positive": int(np.count_nonzero(positive_mask)),
                "bootstrap_repetitions": int(draw_values[metric].size),
                "max_abs_abs_gap_minus_signed_change_all_draws": float(np.max(np.abs(difference))),
                "max_abs_abs_gap_minus_signed_change_positive_subset": (
                    positive_subset_max_difference
                ),
                "positive_subset_agrees_within_1e_12": (
                    bool(np.allclose(difference[positive_mask], 0.0, rtol=0.0, atol=1e-12))
                    if np.any(positive_mask)
                    else False
                ),
                "minimum_component_signed_gap": float(min(np.min(draw_values[name]) for name in components)),
                "maximum_component_signed_gap": float(max(np.max(draw_values[name]) for name in components)),
            }
        )
    return rows


def run_bootstrap_variant(
    helper: Any,
    variant: str,
    population_values: dict[tuple[str, int, str, str], int],
    pattern_data: dict[str, Any],
    weights: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    population = as_helper_population(population_values)
    coefficients = {
        country: helper.bootstrap_coefficients(pattern_data, country, population, weights)
        for country in ("US", "BR")
    }
    for country, (lam, coef) in coefficients.items():
        if lam.size != coef.shape[0] or not lam.size:
            raise ValueError(f"Invalid {country} coefficient matrix for {variant}")
    maximum_block_bytes = max(len(coefficients[country][0]) for country in coefficients) * helper.BOOTSTRAP_BLOCK * np.dtype(np.int64).itemsize
    if maximum_block_bytes > helper.BOOTSTRAP_MEMORY_LIMIT_BYTES:
        raise MemoryError(f"Bootstrap Poisson block estimate exceeds helper's memory contract: {maximum_block_bytes}")

    point_annual = {country: lam @ coef for country, (lam, coef) in coefficients.items()}
    point_metrics = helper.target_metric_values(point_annual["US"], point_annual["BR"])[helper.BOOTSTRAP_TARGETS[0]]
    interval_rows: list[dict[str, Any]] = []
    signed_rows: list[dict[str, Any]] = []
    pair_meta: dict[str, Any] = {"variant": variant, "lambda_sha256": {}, "first_block_poisson_sha256": {}}
    alpha = 0.05 / 6
    primary_metrics = ("T_UCD_R", "T_any_R", "I_R", "T_UCD_A", "T_any_A", "I_A")

    for country, (lam, _coef) in coefficients.items():
        pair_meta["lambda_sha256"][country] = hashlib.sha256(np.asarray(lam, dtype=np.int64).tobytes()).hexdigest()

    for seed in helper.BOOTSTRAP_SEEDS:
        children = np.random.SeedSequence(seed).spawn(2)
        rngs = {country: np.random.default_rng(children[i]) for i, country in enumerate(("US", "BR"))}
        collected: dict[str, list[np.ndarray]] = {}
        first_block_hashes: dict[str, str] = {}
        valid_repetitions = {"US": 0, "BR": 0}

        for start in range(0, helper.BOOTSTRAP_REPS, helper.BOOTSTRAP_BLOCK):
            n = min(helper.BOOTSTRAP_BLOCK, helper.BOOTSTRAP_REPS - start)
            annual_rates: dict[str, np.ndarray] = {}
            for country in ("US", "BR"):
                lam, coef = coefficients[country]
                weights_draw = rngs[country].poisson(lam=lam, size=(n, lam.size))
                if start == 0:
                    first_block_hashes[country] = hashlib.sha256(weights_draw.tobytes()).hexdigest()
                annual_rates[country] = weights_draw @ coef
                valid_repetitions[country] += n
                del weights_draw

            values = helper.target_metric_values(annual_rates["US"], annual_rates["BR"])[helper.BOOTSTRAP_TARGETS[0]]
            for metric, array in values.items():
                collected.setdefault(metric, []).append(np.asarray(array, dtype=np.float64).reshape(-1))

        draws = {metric: np.concatenate(blocks) for metric, blocks in collected.items()}
        pair_meta["first_block_poisson_sha256"][str(seed)] = first_block_hashes
        for metric in primary_metrics:
            values = draws[metric]
            family_low, family_high, valid = helper.percentile_interval(values, alpha)
            ci95_low, ci95_high, valid95 = helper.percentile_interval(values, 0.05)
            estimate = float(np.asarray(point_metrics[metric]).reshape(-1)[0])
            interval_rows.append(
                {
                    "variant": variant,
                    "variant_source_rule": VARIANT_LABELS[variant],
                    "seed": seed,
                    "analysis_role": "0_74_primary_time_family_sensitivity",
                    "age_scope": "0_74",
                    "case_definition": "full",
                    "code_group": "K80-83_total",
                    "metric": metric,
                    "estimate": estimate,
                    "ci95_lower": ci95_low,
                    "ci95_upper": ci95_high,
                    "ci95_valid_repetitions": valid95,
                    "family_ci_lower": family_low,
                    "family_ci_upper": family_high,
                    "family_alpha": alpha,
                    "family_confidence_level": 1 - alpha,
                    "interval_method": "two_sided_percentile_numpy_quantile_linear",
                    "interval_scope": "all_repetitions" if valid == helper.BOOTSTRAP_REPS else "finite_only_descriptive_nonclassifying",
                    "bootstrap_repetitions": helper.BOOTSTRAP_REPS,
                    "valid_repetitions": valid,
                    "lower_tail_theoretical_repetitions": helper.BOOTSTRAP_REPS * alpha / 2,
                    "upper_tail_theoretical_repetitions": helper.BOOTSTRAP_REPS * alpha / 2,
                }
            )
        signed_rows.extend(signed_absolute_diagnostics(variant, seed, draws))
        if valid_repetitions != {"US": helper.BOOTSTRAP_REPS, "BR": helper.BOOTSTRAP_REPS}:
            raise ValueError(f"Unexpected missing draw count for {variant}, seed {seed}: {valid_repetitions}")

    return interval_rows, signed_rows, pair_meta


def run_bootstrap_stage(
    helper: Any,
    variants: dict[str, dict[tuple[str, int, str, str], int]],
    pattern_data: dict[str, Any],
    weights: dict[str, float],
    point_values: dict[str, dict[str, float]],
    input_hashes: dict[str, str],
    denominator_qc: dict[str, Any],
) -> None:
    output_dir = OUTPUT_DIR / "final"
    interval_rows: list[dict[str, Any]] = []
    signed_rows: list[dict[str, Any]] = []
    pair_meta: dict[str, Any] = {}
    lambda_by_country: dict[str, str] = {}
    first_block_by_seed_country: dict[str, str] = {}

    for variant, population in variants.items():
        rows, diagnostics, meta = run_bootstrap_variant(helper, variant, population, pattern_data, weights)
        interval_rows.extend(rows)
        signed_rows.extend(diagnostics)
        pair_meta[variant] = meta
        for country, digest in meta["lambda_sha256"].items():
            if country in lambda_by_country and lambda_by_country[country] != digest:
                raise ValueError(f"Poisson pattern/lambda arrays differ across variants for {country}")
            lambda_by_country[country] = digest
        for seed, country_hashes in meta["first_block_poisson_sha256"].items():
            for country, digest in country_hashes.items():
                key = f"{seed}|{country}"
                if key in first_block_by_seed_country and first_block_by_seed_country[key] != digest:
                    raise ValueError(f"Paired Poisson first block differs across variants at {key}")
                first_block_by_seed_country[key] = digest

    write_csv(output_dir / "temporal_contrast_intervals.csv", interval_rows)
    write_csv(output_dir / "signed_gap_diagnostics.csv", signed_rows)
    write_csv(output_dir / "denominator_variants_0_74.csv", denominator_rows(variants, helper))
    write_csv(output_dir / "denominator_cell_differences_0_74.csv", denominator_difference_rows(variants, helper))

    baseline_expected = read_csv(BASELINE_INTERVALS_PATH)
    expected_by_key = {
        (int(row["seed"]), row["metric"]): row
        for row in baseline_expected
        if row["analysis_role"] == "primary" and row["age_scope"] == "0_74" and row["code_group"] == "K80-83_total"
    }
    replicate_checks: list[dict[str, Any]] = []
    for row in interval_rows:
        if row["variant"] != "baseline_split_2020_2024":
            continue
        expected = expected_by_key[(int(row["seed"]), row["metric"])]
        for field in ("estimate", "ci95_lower", "ci95_upper", "family_ci_lower", "family_ci_upper"):
            observed_value = float(row[field])
            expected_value = float(expected[field])
            difference = abs(observed_value - expected_value)
            replicate_checks.append(
                {
                    "seed": row["seed"],
                    "metric": row["metric"],
                    "quantity": field,
                    "observed": observed_value,
                    "expected_frozen": expected_value,
                    "absolute_difference": difference,
                    "pass_1e_12": difference <= 1e-12,
                }
            )
    if len(replicate_checks) != 60 or not all(row["pass_1e_12"] for row in replicate_checks):
        failures = [row for row in replicate_checks if not row["pass_1e_12"]]
        raise ValueError(f"Baseline reproduction mismatch (n={len(replicate_checks)}); first={failures[:1]}")
    write_csv(output_dir / "baseline_replication_checks.csv", replicate_checks)

    final_manifest = {
        "status": "PASS",
        "run_type": "three-denominator-variant 0-74 temporal sensitivity",
        "python_version": sys.version,
        "numpy_version": np.__version__,
        "input_sha256": input_hashes,
        "denominator_qc": denominator_qc,
        "point_estimates": point_values,
        "bootstrap": {
            "repetitions_per_seed_and_variant": helper.BOOTSTRAP_REPS,
            "seeds": list(helper.BOOTSTRAP_SEEDS),
            "block_size": helper.BOOTSTRAP_BLOCK,
            "family_alpha": 0.05 / 6,
            "family_confidence_level": 1 - 0.05 / 6,
            "family": ["T_UCD_R", "T_any_R", "I_R", "T_UCD_A", "T_any_A", "I_A"],
            "draw_model": "independent Poisson(n_pattern) counts for each disjoint joint-pattern cell, countries independent",
            "paired_across_variants": True,
            "paired_draw_check": {
                "lambda_hashes_match": True,
                "first_block_poisson_hashes_match": True,
                "lambda_sha256_by_country": lambda_by_country,
                "first_block_sha256_by_seed_country": first_block_by_seed_country,
            },
            "interval_method": "NumPy percentile quantile method=linear; family interval alpha=0.05/6; nominal 95% also retained",
        },
        "baseline_replication": {
            "status": "PASS",
            "quantities_checked": len(replicate_checks),
            "maximum_absolute_difference": max(row["absolute_difference"] for row in replicate_checks),
            "tolerance": 1e-12,
        },
        "signed_absolute_gap_check": {
            "status": "reported in signed_gap_diagnostics.csv",
            "canonical_estimand": "T=|signed country gap late|-|signed country gap early|; I=T_any-T_UCD",
            "diagnostic_comparator": "late signed country gap - early signed country gap; I is any minus UCD",
            "positive_subset_identity": "When every required early/late signed gap in a draw is positive, the absolute-gap and signed-change formulas agree up to floating-point tolerance (1e-12).",
        },
    }
    write_json(output_dir / "run_manifest.json", final_manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points-only", action="store_true", help="write validated denominator and point estimates; do not run bootstrap")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_hashes = verify_inputs()
    helper = load_helper()
    source_population, weights, _rows_3744, _rows_936, frozen_metadata = helper.load_frozen_inputs()
    variants, denominator_qc = make_population_variants(helper)
    for key, value in variants["baseline_split_2020_2024"].items():
        if int(source_population[key]["population"]) != value:
            raise ValueError(f"Baseline denominator does not match frozen 936 input at {key}")
    denominator_qc["frozen_936_population_grid"] = {
        "status": "PASS",
        "cells_checked": len(source_population),
        "all_baseline_population_values_match": True,
        "source_hashes": frozen_metadata["sha256"],
    }
    pattern_data = load_pattern_data(helper)
    point_values = {
        variant: compute_point_values(helper, pattern_data, population, weights)
        for variant, population in variants.items()
    }
    run_point_stage(helper, variants, pattern_data, weights, denominator_qc, input_hashes)
    if args.points_only:
        return 0
    run_bootstrap_stage(helper, variants, pattern_data, weights, point_values, input_hashes, denominator_qc)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Population-vintage sensitivity failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
