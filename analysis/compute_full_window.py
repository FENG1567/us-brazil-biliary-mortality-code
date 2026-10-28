#!/usr/bin/env python3
"""Compute direct standardized MCOD point estimates and paired Poisson CIs.

The CLI is intentionally separate from preparation. It reads only a validated
local payload produced by prepare_full_window.py; it never reads raw death
files or runs model/time analyses. The formal mode is fixed at 50,000 draws for
seeds 20261025 (reported) and 20261026 (stability check only).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THREAD_BUDGET_ENV = "USBR_NUM_THREADS"
_raw_thread_budget = os.environ.get(THREAD_BUDGET_ENV, "8")
try:
    NATIVE_THREAD_BUDGET = int(_raw_thread_budget)
except (TypeError, ValueError) as error:
    raise SystemExit(f"{THREAD_BUDGET_ENV} must be an integer from 1 to 8; got {_raw_thread_budget!r}") from error
if not 1 <= NATIVE_THREAD_BUDGET <= 8:
    raise SystemExit(f"{THREAD_BUDGET_ENV} must be an integer from 1 to 8; got {NATIVE_THREAD_BUDGET}")

NATIVE_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS",
    "TBB_NUM_THREADS",
)
# Load only stdlib preparation helpers first; that module sets its CLI default
# to one thread. Compute applies its validated budget afterward, before NumPy
# initializes any native pool.
from prepare_full_window import GROUPS, GROUP_BITS, WINDOWS, UNIT_PER_100K, sha256_file, write_csv, write_json

for _thread_env in NATIVE_THREAD_ENV_VARS:
    os.environ[_thread_env] = str(NATIVE_THREAD_BUDGET)

import numpy as np

SEEDS = (20261025, 20261026)
FORMAL_DRAWS = 50_000
DIAGNOSTIC_DRAWS = 2_000
MAX_BLOCK_REPS = 200
DEFAULT_MEMORY_LIMIT_MIB = 256
M2_GROUPS = tuple(GROUPS[1:])
PRIMARY_WINDOW = "2012-2023"
SUPPORT_WINDOW = "2012-2024"
PRIMARY_FAMILY_ID = "primary_overall_RRR_DD"
SECONDARY_FAMILY_ID = "secondary_fixed_code_RRR_H"


def runtime_threadpool_snapshot() -> dict[str, Any]:
    """Report loaded native pools when threadpoolctl is already available."""
    try:
        from threadpoolctl import threadpool_info
    except ImportError:
        return {"status": "unavailable", "reason": "threadpoolctl is not installed"}
    try:
        return {"status": "available", "pools": threadpool_info()}
    except Exception as error:  # diagnostics must not make analysis fail
        return {"status": "error", "reason": f"{type(error).__name__}: {error}"}


class ComputeError(RuntimeError):
    """Raised for invalid payload, non-estimable inference, or output errors."""


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_rows_stream(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def validate_payload(input_dir: Path) -> tuple[list[dict[str, Any]], dict[tuple[str, int, str, str], int], dict[str, float], dict[str, Any]]:
    manifest_path = input_dir / "payload_manifest.json"
    if not manifest_path.is_file():
        raise ComputeError(f"Missing prepared payload manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("schema") != "mcod_direct_payload_manifest_v1" or manifest.get("payload_status") != "READY_FOR_COMPUTE":
        raise ComputeError("Prepared payload schema/status is not accepted")
    qc = read_json(input_dir / "prepare_qc.json")
    if qc.get("status") != "PASS" or qc.get("validation", {}).get("m2_strata_mismatches") != 0:
        raise ComputeError("Prepared payload did not pass source/M2 closure checks")
    input_manifest = read_json(input_dir / "input_manifest.json")
    if len(input_manifest.get("selected_candidate_streams", [])) != 26:
        raise ComputeError("Prepared input manifest does not bind exactly 26 selected streams")
    if not input_manifest.get("contract", {}).get("sha256"):
        raise ComputeError("Prepared input manifest is not bound to the frozen contract")
    for item in manifest.get("files", []):
        path = input_dir / item["basename"]
        if not path.is_file():
            raise ComputeError(f"Prepared payload file is missing: {path}")
        if sha256_file(path) != item["sha256"] or path.stat().st_size != int(item["bytes"]):
            raise ComputeError(f"Prepared payload checksum/size mismatch: {path}")
    if manifest.get("group_order") != list(GROUPS):
        raise ComputeError("Prepared payload group order differs from compute contract")

    patterns: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in csv_rows_stream(input_dir / "joint_patterns.csv.gz"):
        try:
            pattern = {
                "country": row["country"], "year": int(row["year"]),
                "age_group": row["age_group"], "sex": row["sex"],
                "U_mask": int(row["U_mask"]), "A_mask": int(row["A_mask"]),
                "R_mask": None if row.get("R_mask", "") == "" else int(row["R_mask"]),
                "malignant_UCD": int(row["malignant_UCD"]),
                "malignant_A": int(row["malignant_A"]),
                "malignant_R": None if row.get("malignant_R", "") == "" else int(row["malignant_R"]),
                "N_pattern": int(row["N_pattern"]),
            }
        except (KeyError, ValueError) as error:
            raise ComputeError("Invalid joint-pattern row") from error
        key = (
            pattern["country"], pattern["year"], pattern["age_group"], pattern["sex"],
            pattern["U_mask"], pattern["A_mask"], pattern["R_mask"],
            pattern["malignant_UCD"], pattern["malignant_A"], pattern["malignant_R"],
        )
        if key in seen:
            raise ComputeError(f"Duplicate joint-pattern cell in payload: {key}")
        if pattern["N_pattern"] <= 0:
            raise ComputeError("Joint-pattern multiplicity must be positive")
        if pattern["U_mask"] & ~pattern["A_mask"]:
            raise ComputeError("Prepared payload violates U subset A")
        if pattern["country"] == "US":
            if pattern["R_mask"] is None or pattern["malignant_R"] is None or pattern["U_mask"] & ~pattern["R_mask"]:
                raise ComputeError("Prepared US payload violates U subset R")
        elif pattern["country"] == "BR":
            if pattern["R_mask"] is not None or pattern["malignant_R"] is not None:
                raise ComputeError("Brazil R must be absent in this structural-axis design")
        else:
            raise ComputeError(f"Unsupported country in joint-pattern payload: {pattern['country']}")
        max_mask = (1 << len(GROUPS)) - 1
        if any(mask < 0 or mask > max_mask for mask in (pattern["U_mask"], pattern["A_mask"])):
            raise ComputeError("Prepared U/A mask exceeds nine-group payload schema")
        if pattern["R_mask"] is not None and (pattern["R_mask"] < 0 or pattern["R_mask"] > max_mask):
            raise ComputeError("Prepared R mask exceeds nine-group payload schema")
        seen.add(key)
        patterns.append(pattern)
    if not patterns:
        raise ComputeError("Prepared joint-pattern payload is empty")

    population_rows = read_csv_rows(input_dir / "population.csv")
    population: dict[tuple[str, int, str, str], int] = {}
    for row in population_rows:
        key = (row["country"], int(row["year"]), row["age_group"], row["sex"])
        if key in population:
            raise ComputeError(f"Duplicate population cell {key}")
        value = int(row["population"])
        if value <= 0:
            raise ComputeError(f"Nonpositive population denominator at {key}")
        population[key] = value
    if len(population) != 936:
        raise ComputeError(f"Expected 936 population cells, found {len(population)}")
    pattern_total = sum(row["N_pattern"] for row in patterns)
    if pattern_total != 271_647:
        raise ComputeError(f"Prepared pattern total differs from frozen retained total: {pattern_total}")
    for row in patterns:
        cell = (row["country"], row["year"], row["age_group"], row["sex"])
        if cell not in population:
            raise ComputeError(f"Pattern lacks a frozen population denominator: {cell}")

    weights_rows = read_csv_rows(input_dir / "who18_weights.csv")
    weights = {row["age_group"]: float(row["weight_normalized"]) for row in weights_rows}
    if len(weights) != 18 or abs(sum(weights.values()) - 1.0) > 1e-12:
        raise ComputeError("WHO18 normalized weights do not form an 18-age unit vector")
    if any(row["age_group"] not in weights for row in patterns):
        raise ComputeError("Joint-pattern age is absent from WHO18 weights")
    return patterns, population, weights, manifest


def build_rate_design(
    patterns: list[dict[str, Any]],
    population: dict[tuple[str, int, str, str], int],
    weights: dict[str, float],
) -> dict[str, Any]:
    specs: list[tuple[str, str, str, str]] = []
    for window in WINDOWS:
        for country in ("US", "BR"):
            for group in GROUPS:
                for endpoint in ("U", "A"):
                    specs.append((window, country, group, endpoint))
                if country == "US":
                    specs.append((window, country, group, "R"))
    n_patterns, n_specs = len(patterns), len(specs)
    coefficients = np.zeros((n_patterns, n_specs), dtype=np.float64)
    count_indicators = np.zeros((n_patterns, n_specs), dtype=np.uint8)
    spec_index = {spec: index for index, spec in enumerate(specs)}
    years_by_window = {window: set(years) for window, years in WINDOWS.items()}

    countries = np.asarray([row["country"] for row in patterns], dtype=object)
    years = np.asarray([row["year"] for row in patterns], dtype=np.int32)
    age_groups = np.asarray([row["age_group"] for row in patterns], dtype=object)
    sexes = np.asarray([row["sex"] for row in patterns], dtype=object)
    u_masks = np.asarray([row["U_mask"] for row in patterns], dtype=np.int64)
    a_masks = np.asarray([row["A_mask"] for row in patterns], dtype=np.int64)
    r_masks = np.asarray([-1 if row["R_mask"] is None else row["R_mask"] for row in patterns], dtype=np.int64)
    pattern_counts = np.asarray([row["N_pattern"] for row in patterns], dtype=np.int64)

    for spec_index_value, spec in enumerate(specs):
        window, country, group, endpoint = spec
        axis_masks = {"U": u_masks, "A": a_masks, "R": r_masks}[endpoint]
        bit = GROUP_BITS[group]
        eligible = (countries == country) & np.isin(years, tuple(years_by_window[window])) & ((axis_masks & bit) != 0)
        if endpoint == "R" and country != "US":
            raise ComputeError("R endpoint may only be computed for US")
        selected = np.flatnonzero(eligible)
        for pattern_index in selected:
            row = patterns[int(pattern_index)]
            pop = population[(country, row["year"], row["age_group"], row["sex"])]
            if pop <= 0:
                raise ComputeError(f"Nonpositive population denominator at {row}")
            direct_weight = weights[row["age_group"]] * 0.5 / len(years_by_window[window])
            coefficients[pattern_index, spec_index_value] = direct_weight * UNIT_PER_100K / pop
            count_indicators[pattern_index, spec_index_value] = 1

    point_rates = pattern_counts @ coefficients
    point_counts = pattern_counts @ count_indicators
    return {
        "patterns": patterns,
        "pattern_counts": pattern_counts,
        "coefficients": coefficients,
        "count_indicators": count_indicators,
        "specs": specs,
        "spec_index": spec_index,
        "point_rates": point_rates,
        "point_counts": point_counts,
    }


def safe_divide(numerator: np.ndarray | float, denominator: np.ndarray | float) -> np.ndarray | float:
    if isinstance(numerator, np.ndarray) or isinstance(denominator, np.ndarray):
        num = np.asarray(numerator, dtype=np.float64)
        den = np.asarray(denominator, dtype=np.float64)
        result = np.full(np.broadcast_shapes(num.shape, den.shape), np.nan, dtype=np.float64)
        np.divide(num, den, out=result, where=(den != 0))
        return result
    return float(numerator / denominator) if denominator != 0 else float("nan")


def metric_keys() -> list[tuple[str, str, str, str]]:
    keys: list[tuple[str, str, str, str]] = []
    for window in WINDOWS:
        for group in GROUPS:
            for variant in ("entity_any", "record_any"):
                keys.append(("RRR", window, group, variant))
                keys.append(("DD", window, group, variant))
            for metric, variant in (
                ("RR_U", "common_U"), ("RD_U", "common_U"),
                ("E_BR_A", "common_A"),
                ("RR_A", "entity_any"), ("RD_A", "entity_any"), ("E_US_A", "entity_any"),
                ("RR_R", "record_any"), ("RD_R", "record_any"), ("E_US_R", "record_any"),
            ):
                keys.append((metric, window, group, variant))
            for metric in (
                "delta_RRR_record_minus_entity",
                "delta_DD_record_minus_entity",
                "ratio_RRR_record_to_entity",
                "ratio_E_US_record_to_entity",
            ):
                keys.append((metric, window, group, "structural_comparison"))
    for window in WINDOWS:
        for variant in ("entity_any", "record_any"):
            keys.append(("H_logRRR_K83.0_minus_K83.1", window, "K83.0_vs_K83.1", variant))
    return keys


def metric_id(key: tuple[str, str, str, str]) -> str:
    metric, window, group, variant = key
    return "|".join((metric, window, group, variant))


def derive_metrics(rate_matrix: np.ndarray, spec_index: dict[tuple[str, str, str, str], int]) -> dict[tuple[str, str, str, str], np.ndarray]:
    """Derive paired RRR/DD and representation sensitivity from shared rates."""
    n_rows = rate_matrix.shape[0]
    derived: dict[tuple[str, str, str, str], np.ndarray] = {}
    variants = {"entity_any": "A", "record_any": "R"}

    def get(window: str, country: str, group: str, endpoint: str) -> np.ndarray:
        return rate_matrix[:, spec_index[(window, country, group, endpoint)]]

    for window in WINDOWS:
        for group in GROUPS:
            us_u = get(window, "US", group, "U")
            br_u = get(window, "BR", group, "U")
            rr_u = safe_divide(br_u, us_u)
            rd_u = br_u - us_u
            per_variant: dict[str, dict[str, np.ndarray]] = {}
            for variant, us_endpoint in variants.items():
                us_av = get(window, "US", group, us_endpoint)
                br_av = get(window, "BR", group, "A")
                rr_av = safe_divide(br_av, us_av)
                rrr_by_country_representation = safe_divide(
                    safe_divide(br_av, br_u), safe_divide(us_av, us_u)
                )
                rrr = safe_divide(rr_av, rr_u)
                finite = np.isfinite(rrr) & np.isfinite(rrr_by_country_representation)
                if np.any(finite) and not np.allclose(
                    rrr[finite], rrr_by_country_representation[finite], rtol=0, atol=1e-10,
                ):
                    raise ComputeError("RRR != E_BR/E_US under paired draw")
                rd_av = br_av - us_av
                dd = rd_av - rd_u
                per_variant[variant] = {
                    "RRR": rrr,
                    "DD": dd,
                    "RR_available": rr_av,
                    "RD_available": rd_av,
                    "E_US": safe_divide(us_av, us_u),
                    "E_BR": safe_divide(br_av, br_u),
                    "RR_U": rr_u,
                    "RD_U": rd_u,
                }
                derived[("RRR", window, group, variant)] = rrr
                derived[("DD", window, group, variant)] = dd
            entity = per_variant["entity_any"]
            record = per_variant["record_any"]
            derived[("RR_U", window, group, "common_U")] = per_variant["entity_any"]["RR_U"]
            derived[("RD_U", window, group, "common_U")] = per_variant["entity_any"]["RD_U"]
            derived[("E_BR_A", window, group, "common_A")] = entity["E_BR"]
            derived[("RR_A", window, group, "entity_any")] = entity["RR_available"]
            derived[("RD_A", window, group, "entity_any")] = entity["RD_available"]
            derived[("E_US_A", window, group, "entity_any")] = entity["E_US"]
            derived[("RR_R", window, group, "record_any")] = record["RR_available"]
            derived[("RD_R", window, group, "record_any")] = record["RD_available"]
            derived[("E_US_R", window, group, "record_any")] = record["E_US"]
            derived[("delta_RRR_record_minus_entity", window, group, "structural_comparison")] = record["RRR"] - entity["RRR"]
            derived[("delta_DD_record_minus_entity", window, group, "structural_comparison")] = record["DD"] - entity["DD"]
            derived[("ratio_RRR_record_to_entity", window, group, "structural_comparison")] = safe_divide(
                record["RRR"], entity["RRR"]
            )
            derived[("ratio_E_US_record_to_entity", window, group, "structural_comparison")] = safe_divide(
                record["E_US"], entity["E_US"]
            )
        for variant in variants:
            rrr_830 = derived[("RRR", window, "K83.0", variant)]
            rrr_831 = derived[("RRR", window, "K83.1", variant)]
            h = np.full(n_rows, np.nan, dtype=np.float64)
            valid = (rrr_830 > 0) & (rrr_831 > 0) & np.isfinite(rrr_830) & np.isfinite(rrr_831)
            h[valid] = np.log(rrr_830[valid]) - np.log(rrr_831[valid])
            derived[("H_logRRR_K83.0_minus_K83.1", window, "K83.0_vs_K83.1", variant)] = h
    return derived


def point_estimate_rows(design: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str], float]]:
    one_row_rates = design["point_rates"][None, :]
    derived = derive_metrics(one_row_rates, design["spec_index"])
    values = {key: float(arr[0]) for key, arr in derived.items()}
    count_index = design["spec_index"]
    rate_index = design["spec_index"]

    def count(window: str, country: str, group: str, endpoint: str) -> int:
        return int(design["point_counts"][count_index[(window, country, group, endpoint)]])

    def rate(window: str, country: str, group: str, endpoint: str) -> float:
        return float(design["point_rates"][rate_index[(window, country, group, endpoint)]])

    rows: list[dict[str, Any]] = []
    for window in WINDOWS:
        for group in GROUPS:
            for variant, us_endpoint in (("entity_any", "A"), ("record_any", "R")):
                us_u, br_u = rate(window, "US", group, "U"), rate(window, "BR", group, "U")
                us_a, br_a = rate(window, "US", group, "A"), rate(window, "BR", group, "A")
                us_r = rate(window, "US", group, "R")
                us_av = us_a if us_endpoint == "A" else us_r
                rr_u = safe_divide(br_u, us_u)
                rr_a = safe_divide(br_a, us_a)
                rr_r = safe_divide(br_a, us_r)
                e_us = safe_divide(us_av, us_u)
                e_br = safe_divide(br_a, br_u)
                rr_available = safe_divide(br_a, us_av)
                rrr = safe_divide(rr_available, rr_u)
                rd_u, rd_a, rd_r = br_u - us_u, br_a - us_a, br_a - us_r
                dd_a, dd_r = rd_a - rd_u, rd_r - rd_u
                n_us_u, n_br_u = count(window, "US", group, "U"), count(window, "BR", group, "U")
                n_us_a, n_br_a = count(window, "US", group, "A"), count(window, "BR", group, "A")
                n_us_r = count(window, "US", group, "R")
                required_counts = (n_us_u, n_br_u, n_br_a, n_us_a if variant == "entity_any" else n_us_r)
                count_status = "ESTIMABLE_NONZERO_COUNTS" if all(value > 0 for value in required_counts) else "NON_ESTIMABLE_ZERO_COUNT"
                if np.isfinite(rrr) and np.isfinite(e_br) and np.isfinite(e_us) and not np.isclose(
                    rrr, e_br / e_us, rtol=0, atol=1e-10,
                ):
                    raise ComputeError("Point estimate closure failed: RRR != E_BR/E_US")
                rows.append({
                    "estimand_id": f"USBR_MCOD_DIRECT_{window}_{group}_{variant}",
                    "window": window,
                    "window_role": "primary_stable_version" if window == PRIMARY_WINDOW else "version_sensitive_support",
                    "case_definition": "full",
                    "code_group": group,
                    "us_axis": us_endpoint,
                    "br_axis": "A_certificate_line",
                    "axis_variant": variant,
                    "n_US_U": n_us_u,
                    "n_BR_U": n_br_u,
                    "n_US_A": n_us_a,
                    "n_BR_A": n_br_a,
                    "n_US_R": n_us_r,
                    "r_US_U_per_100k": us_u,
                    "r_BR_U_per_100k": br_u,
                    "r_US_A_per_100k": us_a,
                    "r_BR_A_per_100k": br_a,
                    "r_US_R_per_100k": us_r,
                    "RR_U": rr_u,
                    "RR_A": rr_a,
                    "RR_R": rr_r,
                    "E_US": e_us,
                    "E_BR": e_br,
                    "RRR": rrr,
                    "RD_U_per_100k": rd_u,
                    "RD_A_per_100k": rd_a,
                    "RD_R_per_100k": rd_r,
                    "DD_A_per_100k": dd_a,
                    "DD_R_per_100k": dd_r,
                    "count_support_status": count_status,
                    "primary_measure": "RRR and DD_A" if window == PRIMARY_WINDOW and group == "K80-83_total" and variant == "entity_any" else "descriptive_or_sensitivity",
                    "interpretation_note": "RRR<1 is a numerical reduction in the signed country ratio, not universally movement toward equality",
                })
    return rows, values


def choose_block_size(n_patterns: int, n_specs: int, n_metrics: int, reps: int, memory_limit_mib: int) -> tuple[int, int]:
    memory_limit = int(memory_limit_mib) * 1024 * 1024
    persistent = n_patterns * n_specs * 8 + n_patterns * n_specs + reps * n_metrics * 8 + n_patterns * 8
    remaining = memory_limit - persistent
    per_draw = 2 * n_patterns * 8 + n_specs * 8 + n_metrics * 8
    if remaining < per_draw:
        raise ComputeError(
            f"Memory budget cannot fit one draw chunk: estimated persistent={persistent:,} bytes, "
            f"limit={memory_limit:,} bytes"
        )
    block = min(MAX_BLOCK_REPS, max(1, remaining // per_draw))
    return int(block), int(persistent + block * per_draw)


def run_bootstrap(
    design: dict[str, Any],
    *,
    reps: int,
    seed: int,
    memory_limit_mib: int = DEFAULT_MEMORY_LIMIT_MIB,
) -> tuple[list[tuple[str, str, str, str]], np.ndarray, dict[str, Any]]:
    if reps <= 0:
        raise ComputeError("Number of bootstrap repetitions must be positive")
    keys = metric_keys()
    n_patterns = len(design["pattern_counts"])
    n_specs = len(design["specs"])
    block_size, estimated_peak_bytes = choose_block_size(n_patterns, n_specs, len(keys), reps, memory_limit_mib)
    values = np.empty((reps, len(keys)), dtype=np.float64)
    rng = np.random.default_rng(seed)
    coefficients = design["coefficients"]
    lambdas = design["pattern_counts"]
    completed = 0
    while completed < reps:
        n_chunk = min(block_size, reps - completed)
        # Correct aggregation: each death has an independent Pois(1) multiplier,
        # so N deaths in a joint cell sum to Pois(N), not N * Pois(1).
        pattern_draws = rng.poisson(lam=lambdas, size=(n_chunk, n_patterns))
        rate_draws = pattern_draws @ coefficients
        derived = derive_metrics(rate_draws, design["spec_index"])
        for column, key in enumerate(keys):
            values[completed:completed + n_chunk, column] = derived[key]
        completed += n_chunk
    meta = {
        "seed": seed,
        "draws": reps,
        "pattern_cells": n_patterns,
        "rate_specs": n_specs,
        "metric_draw_columns": len(keys),
        "chunk_size": block_size,
        "max_block_repetitions": MAX_BLOCK_REPS,
        "memory_limit_mib": memory_limit_mib,
        "estimated_peak_working_bytes": estimated_peak_bytes,
        "numpy_quantile_method": "linear",
        "resampling": "independent Poisson(N_pattern) by stratum/joint-pattern; shared across all endpoints/groups/axes/windows",
        "country_independence": True,
        "threads": {
            "job_budget": 8,
            "python_workers": 1,
            "requested_native_threads_per_worker": NATIVE_THREAD_BUDGET,
            "nested_oversubscription": False,
        },
    }
    return keys, values, meta


def adjusted_family(key: tuple[str, str, str, str]) -> tuple[str, int, float] | None:
    metric, window, group, variant = key
    if window != PRIMARY_WINDOW or variant != "entity_any":
        return None
    if group == "K80-83_total" and metric in {"RRR", "DD"}:
        return PRIMARY_FAMILY_ID, 2, 0.975
    if metric == "RRR" and group in M2_GROUPS:
        return SECONDARY_FAMILY_ID, 9, 1.0 - 0.05 / 9.0
    if metric == "H_logRRR_K83.0_minus_K83.1" and group == "K83.0_vs_K83.1":
        return SECONDARY_FAMILY_ID, 9, 1.0 - 0.05 / 9.0
    return None


def value_classification(metric: str, low: float, high: float) -> str:
    if not np.isfinite(low) or not np.isfinite(high):
        return "NON_ESTIMABLE"
    if metric == "RRR" or metric == "ratio_RRR_record_to_entity":
        if high < 1:
            return "BELOW_1"
        if low > 1:
            return "ABOVE_1"
        return "INCLUDES_1"
    if high < 0:
        return "BELOW_0"
    if low > 0:
        return "ABOVE_0"
    return "INCLUDES_0"


def interval_rows(
    keys: list[tuple[str, str, str, str]],
    values: np.ndarray,
    point_values: dict[tuple[str, str, str, str], float],
    *,
    seed: int,
    phase: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seed_role = "reported" if seed == SEEDS[0] else "stability_check_only"
    for index, key in enumerate(keys):
        metric, window, group, variant = key
        draw_values = values[:, index]
        finite = draw_values[np.isfinite(draw_values)]
        invalid = int(len(draw_values) - len(finite))
        invalid_fraction = invalid / len(draw_values)
        family = adjusted_family(key)
        adjusted_name, family_n, adjusted_level = family if family else ("", "", "")
        point = point_values.get(key, float("nan"))
        if adjusted_name == PRIMARY_FAMILY_ID:
            evidence_role = "posthoc_primary_adjusted_family"
        elif adjusted_name == SECONDARY_FAMILY_ID:
            evidence_role = "posthoc_secondary_adjusted_family"
        else:
            evidence_role = "descriptive_or_sensitivity"
        base = {
            "estimand_id": f"USBR_MCOD_DIRECT_{window}_{group}_{variant}_{metric}",
            "metric_id": metric_id(key), "metric": metric, "window": window,
            "code_group": group, "axis_variant": variant,
            "seed": seed, "seed_role": seed_role, "phase": phase,
            "draws_requested": len(draw_values), "draws_valid": len(finite),
            "invalid_repetitions": invalid, "invalid_fraction": invalid_fraction,
            "point_estimate": point, "nominal_level": 0.95,
            "nominal_lower": "", "nominal_upper": "",
            "family_id": adjusted_name, "family_size": family_n,
            "adjusted_level": adjusted_level,
            "adjusted_lower": "", "adjusted_upper": "",
            "quantile_method": "numpy.quantile(method='linear')",
            "status": "PASS", "evidence_role": evidence_role,
        }
        if not np.isfinite(point):
            base["status"] = "NON_ESTIMABLE_POINT"
        elif not len(finite):
            base["status"] = "NO_VALID_REPETITIONS"
        elif invalid_fraction > 0.01:
            base["status"] = "INVALID_REPETITIONS_GT_1_PERCENT"
        else:
            lower, upper = np.quantile(finite, [0.025, 0.975], method="linear")
            base["nominal_lower"], base["nominal_upper"] = float(lower), float(upper)
            if invalid:
                base["status"] = "VALID_INTERVAL_WITH_INVALID_DRAWS_REPORTED"
            if family:
                alpha = 1.0 - float(adjusted_level)
                adjusted_lower, adjusted_upper = np.quantile(
                    finite, [alpha / 2.0, 1.0 - alpha / 2.0], method="linear",
                )
                base["adjusted_lower"], base["adjusted_upper"] = float(adjusted_lower), float(adjusted_upper)
        if phase == "diagnostic":
            base["status"] = "DIAGNOSTIC_ONLY" if base["status"] == "PASS" else base["status"]
        rows.append(base)
    return rows


def write_draws(path: Path, keys: list[tuple[str, str, str, str]], values: np.ndarray) -> None:
    fields = ["draw_index"] + [metric_id(key) for key in keys]
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fields)
        for i, row in enumerate(values, start=1):
            writer.writerow([i] + ["" if not np.isfinite(x) else format(float(x), ".17g") for x in row])


def run_seed_outputs(
    design: dict[str, Any],
    point_values: dict[tuple[str, str, str, str], float],
    *,
    seed: int,
    phase: str,
    reps: int,
    memory_limit_mib: int,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run/write one seed and release its large draw matrix before returning."""
    keys, values, meta = run_bootstrap(
        design, reps=reps, seed=seed, memory_limit_mib=memory_limit_mib,
    )
    meta["phase"] = phase
    meta["seed_role"] = "reported" if seed == SEEDS[0] else "stability_check_only"
    meta["invalid_metric_draw_counts"] = {
        metric_id(key): int(np.count_nonzero(~np.isfinite(values[:, index])))
        for index, key in enumerate(keys)
    }
    interval_list = interval_rows(keys, values, point_values, seed=seed, phase=phase)
    write_csv(output_dir / f"intervals_seed{seed}.csv", list(interval_list[0]), interval_list)
    write_draws(output_dir / f"draws_seed{seed}.csv.gz", keys, values)
    del values, keys
    return interval_list, meta


def point_sensitivity_rows(
    point_values: dict[tuple[str, str, str, str], float],
    point_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    point_index = {
        (row["window"], row["code_group"], row["axis_variant"]): row
        for row in point_rows
    }
    for window in WINDOWS:
        for group in GROUPS:
            entity_rrr = point_values[("RRR", window, group, "entity_any")]
            record_rrr = point_values[("RRR", window, group, "record_any")]
            entity_dd = point_values[("DD", window, group, "entity_any")]
            record_dd = point_values[("DD", window, group, "record_any")]
            e_us_entity = float(point_index[(window, group, "entity_any")]["E_US"])
            e_us_record = float(point_index[(window, group, "record_any")]["E_US"])
            rows.append({
                "estimand_id": f"USBR_MCOD_DIRECT_{window}_{group}_US_axis_sensitivity",
                "window": window, "code_group": group,
                "RRR_entity_axis": entity_rrr, "RRR_record_axis": record_rrr,
                "RRR_record_minus_entity": record_rrr - entity_rrr,
                "E_US_entity_axis": e_us_entity,
                "E_US_record_axis": e_us_record,
                "E_US_record_to_entity_ratio": point_values[("ratio_E_US_record_to_entity", window, group, "structural_comparison")],
                "DD_entity_axis_per_100k": entity_dd, "DD_record_axis_per_100k": record_dd,
                "DD_record_minus_entity_per_100k": record_dd - entity_dd,
                "status": "descriptive_structural_sensitivity",
            })
    return rows


def compare_seed_intervals(rows_by_seed: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not all(seed in rows_by_seed for seed in SEEDS):
        return []
    indexed = {
        seed: {row["metric_id"]: row for row in rows_by_seed[seed]}
        for seed in SEEDS
    }
    output: list[dict[str, Any]] = []
    for metric_key, first in indexed[SEEDS[0]].items():
        if not first.get("family_id"):
            continue
        second = indexed[SEEDS[1]].get(metric_key)
        if second is None:
            raise ComputeError(f"Seed 20261026 is missing family member {metric_key}")
        first_low, first_high = first.get("adjusted_lower"), first.get("adjusted_upper")
        second_low, second_high = second.get("adjusted_lower"), second.get("adjusted_upper")
        if any(value in (None, "") for value in (first_low, first_high, second_low, second_high)):
            delta_low = delta_high = fraction = ""
            endpoint_pass = False
            first_class = second_class = "NON_ESTIMABLE"
        else:
            first_low, first_high = float(first_low), float(first_high)
            second_low, second_high = float(second_low), float(second_high)
            delta_low, delta_high = abs(second_low - first_low), abs(second_high - first_high)
            width = first_high - first_low
            fraction = max(delta_low, delta_high) / width if width > 0 else (0.0 if delta_low == delta_high == 0 else float("inf"))
            endpoint_pass = fraction <= 0.05
            metric = str(first["metric"])
            first_class = value_classification(metric, first_low, first_high)
            second_class = value_classification(metric, second_low, second_high)
        output.append({
            "metric_id": metric_key, "family_id": first.get("family_id"),
            "seed_reported": SEEDS[0], "seed_stability": SEEDS[1],
            "reported_adjusted_lower": first_low if isinstance(first_low, float) else "",
            "reported_adjusted_upper": first_high if isinstance(first_high, float) else "",
            "stability_adjusted_lower": second_low if isinstance(second_low, float) else "",
            "stability_adjusted_upper": second_high if isinstance(second_high, float) else "",
            "absolute_lower_endpoint_difference": delta_low,
            "absolute_upper_endpoint_difference": delta_high,
            "max_endpoint_difference_over_reported_width": fraction,
            "endpoint_tolerance_pass": endpoint_pass,
            "reported_interval_classification": first_class,
            "stability_interval_classification": second_class,
            "interpretation_stable": first_class == second_class,
            "stability_pass": bool(endpoint_pass and first_class == second_class and first.get("status") not in {"INVALID_REPETITIONS_GT_1_PERCENT", "NON_ESTIMABLE_POINT"} and second.get("status") not in {"INVALID_REPETITIONS_GT_1_PERCENT", "NON_ESTIMABLE_POINT"}),
        })
    return output


def output_record(path: Path, role: str) -> dict[str, Any]:
    return {"role": role, "basename": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def assess_inferential_acceptance(
    phase: str,
    interval_rows_all: list[dict[str, Any]],
    stability_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Separate successful execution from contract-level inference acceptance."""
    if phase != "formal":
        return {
            "status": "DIAGNOSTIC_ONLY",
            "accepted": False,
            "reason": "A 2,000-draw diagnostic is not formal inference acceptance.",
            "suppressed_confirmatory_metric_ids": [],
            "failed_seed_stability_metric_ids": [],
        }

    expected_sizes = {PRIMARY_FAMILY_ID: 2, SECONDARY_FAMILY_ID: 9}
    family_rows = [row for row in interval_rows_all if row.get("family_id")]
    suppressed: list[str] = []
    size_checks: dict[str, Any] = {}
    seed_rows_valid = True
    for seed in SEEDS:
        seed_family_rows = [row for row in family_rows if int(row["seed"]) == seed]
        observed_by_family = {
            family_id: sum(row.get("family_id") == family_id for row in seed_family_rows)
            for family_id in expected_sizes
        }
        size_checks[str(seed)] = observed_by_family
        if observed_by_family != expected_sizes:
            seed_rows_valid = False
        for row in seed_family_rows:
            if (
                row.get("adjusted_lower") in (None, "")
                or row.get("adjusted_upper") in (None, "")
                or row.get("status") in {
                    "NON_ESTIMABLE_POINT", "NO_VALID_REPETITIONS",
                    "INVALID_REPETITIONS_GT_1_PERCENT",
                }
            ):
                suppressed.append(str(row["metric_id"]))

    stability_failures = [
        str(row.get("metric_id", ""))
        for row in stability_rows
        if not row.get("stability_pass", False)
    ]
    stability_valid = len(stability_rows) == sum(expected_sizes.values()) and not stability_failures
    accepted = seed_rows_valid and not suppressed and stability_valid
    return {
        "status": "PASS" if accepted else "NOT_ACCEPTED",
        "accepted": accepted,
        "reason": "All adjusted family intervals and two-seed stability checks passed." if accepted else "At least one adjusted interval is suppressed/non-estimable or a seed-stability gate failed.",
        "expected_family_sizes_per_seed": expected_sizes,
        "observed_family_sizes_per_seed": size_checks,
        "confirmatory_interval_rows": len(family_rows),
        "suppressed_confirmatory_metric_ids": sorted(set(suppressed)),
        "seed_stability_rows_expected": sum(expected_sizes.values()),
        "seed_stability_rows_observed": len(stability_rows),
        "failed_seed_stability_metric_ids": stability_failures,
    }


def run_compute(args: argparse.Namespace) -> dict[str, Any]:
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise ComputeError(f"Output directory is nonempty; choose a new directory or pass --overwrite: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    patterns, population, weights, payload_manifest = validate_payload(input_dir)
    design = build_rate_design(patterns, population, weights)
    point_rows, point_values = point_estimate_rows(design)
    write_csv(output_dir / "point_estimates.csv", list(point_rows[0]), point_rows)
    sensitivity_rows = point_sensitivity_rows(point_values, point_rows)
    write_csv(output_dir / "axis_sensitivity.csv", list(sensitivity_rows[0]), sensitivity_rows)

    reps = DIAGNOSTIC_DRAWS if args.phase == "diagnostic" else FORMAL_DRAWS
    seed_interval_rows: dict[int, list[dict[str, Any]]] = {}
    run_metadata: list[dict[str, Any]] = []
    for seed in args.seeds:
        interval_list, meta = run_seed_outputs(
            design, point_values, seed=seed, phase=args.phase, reps=reps,
            memory_limit_mib=args.memory_limit_mib, output_dir=output_dir,
        )
        seed_interval_rows[seed] = interval_list
        run_metadata.append(meta)

    all_interval_rows = [row for seed in args.seeds for row in seed_interval_rows[seed]]
    write_csv(output_dir / "intervals_all_seeds.csv", list(all_interval_rows[0]), all_interval_rows)
    stability_rows = compare_seed_intervals(seed_interval_rows)
    if stability_rows:
        write_csv(output_dir / "seed_stability.csv", list(stability_rows[0]), stability_rows)
    else:
        write_json(output_dir / "seed_stability.json", {"status": "NOT_RUN", "reason": "Both contract seeds are required for comparison"})
    inference_acceptance = assess_inferential_acceptance(args.phase, all_interval_rows, stability_rows)

    output_files = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "compute_manifest.json":
            output_files.append(output_record(path, "compute_output"))
    manifest = {
        "schema": "mcod_direct_compute_manifest_v1",
        "status": "COMPLETED",
        "execution_status": "PASS",
        "inferential_acceptance_status": inference_acceptance["status"],
        "inference_acceptance": inference_acceptance,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase": args.phase,
        "input_payload_dir": str(input_dir),
        "input_payload_manifest_sha256": sha256_file(input_dir / "payload_manifest.json"),
        "compute_script": str(Path(__file__).resolve()),
        "compute_script_sha256": sha256_file(Path(__file__).resolve()),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "numpy_version": np.__version__,
        "resampling": "Nstar_pattern ~ Poisson(N_pattern), independent across disjoint pattern cells; one count draw reused across all endpoints, groups, axes, windows",
        "standardization": {
            "age_weights": "normalized frozen WHO18 weights",
            "sex_weights": {"F": 0.5, "M": 0.5},
            "year_weights": "equal within each window",
            "denominator": "fixed population by country/year/age/sex",
            "rate_unit": "per 100000",
        },
        "seeds": run_metadata,
        "reported_seed": 20261025,
        "stability_only_seed": 20261026,
        "multiplicity_families": [
            {"family_id": PRIMARY_FAMILY_ID, "members": ["RRR", "DD for K80-83_total, 2012-2023, entity axis"], "familywise_level": 0.95, "per_contrast_level": 0.975},
            {"family_id": SECONDARY_FAMILY_ID, "members": ["eight fixed-group RRRs plus H=log(RRR_K83.0)-log(RRR_K83.1), 2012-2023, entity axis"], "familywise_level": 0.95, "per_contrast_level": 1.0 - 0.05 / 9.0},
        ],
        "intervals": "percentile endpoints via numpy.quantile(method='linear'); invalid draws counted, never replaced; intervals suppressed if invalid fraction exceeds 1%",
        "thread_budget": {
            "maximum_total": 8,
            "requested_native_threads_per_worker": NATIVE_THREAD_BUDGET,
            "python_workers": 1,
            "nested_oversubscription": False,
            "environment_variable": THREAD_BUDGET_ENV,
            "native_thread_environment": {name: os.environ.get(name) for name in NATIVE_THREAD_ENV_VARS},
            "runtime_threadpools": runtime_threadpool_snapshot(),
        },
        "draws_saved": True,
        "outputs": output_files,
        "seed_stability": {
            "family_rows_checked": len(stability_rows),
            "all_stability_pass": bool(stability_rows) and all(row["stability_pass"] for row in stability_rows),
            "endpoint_difference_limit": "<=5% of seed 20261025 interval width",
        },
        "limitations": [
            "Poisson-process intervals are conditional on the retained source streams, fixed population, weights, and definitions.",
            "Intervals do not cover certification, coverage, missing-source, or source-version uncertainty.",
            "The 2012-2024 window and US record-axis analyses are exploratory structural/version-sensitive sensitivity results.",
        ],
        "formal_draws_run": args.phase == "formal",
    }
    write_json(output_dir / "compute_manifest.json", manifest)
    return {
        "status": "COMPLETED",
        "execution_status": "PASS",
        "inferential_acceptance_status": inference_acceptance["status"],
        "inference_acceptance": inference_acceptance,
        "phase": args.phase,
        "draws_per_seed": reps,
        "seeds": args.seeds,
        "output_dir": str(output_dir),
        "pattern_cells": len(patterns),
        "point_rows": len(point_rows),
        "interval_rows": len(all_interval_rows),
        "formal_draws_run": args.phase == "formal",
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True, help="prepared local payload directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="new output directory")
    parser.add_argument("--phase", choices=("diagnostic", "formal"), required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS), choices=SEEDS)
    parser.add_argument("--memory-limit-mib", type=int, default=DEFAULT_MEMORY_LIMIT_MIB)
    parser.add_argument("--overwrite", action="store_true", help="allow replacing known files in output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if tuple(args.seeds) != SEEDS:
        print(f"Compute requires the ordered contract seeds {SEEDS}; got {tuple(args.seeds)}", file=sys.stderr)
        return 2
    if args.memory_limit_mib <= 0:
        print("--memory-limit-mib must be positive", file=sys.stderr)
        return 2
    try:
        result = run_compute(args)
    except Exception as error:
        print(f"Compute failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
