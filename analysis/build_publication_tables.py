#!/usr/bin/env python3
"""Assemble manuscript-ready CSVs and a machine-readable result ledger.

This stage consumes only the checksum-bound formal summary tables and named
reused summaries. It does not resample, re-filter source records, or create
figures/documents. All selection keys are explicit below and echoed into the
result ledger.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RUN_ID = "USBR_MCOD_finalization_20261025"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIMARY_WINDOW = "2012-2023"
SUPPORT_WINDOW = "2012-2024"
PRIMARY_GROUP = "K80-83_total"
FIXED_EIGHT_GROUPS = ("K80", "K80-81", "K81", "K82", "K82.9", "K83", "K83.0", "K83.1")
ALL_GROUPS = (PRIMARY_GROUP,) + FIXED_EIGHT_GROUPS
REPORTED_SEED = 20261025
STABILITY_SEED = 20261026
M1_PERIOD = PRIMARY_WINDOW
M1_FOCUS_DEFINITION = "K83_in_family_set__only_PartI"
M1_FOCUS_UCD = "malignant_C00_C97"
M1_TOP_N_PER_SIGN = 10
M1_CLOSURE_TOLERANCE = 1e-8

SOURCE_PATHS = {
    "source_map": "01_inventory/SOURCE_MAP.json",
    "formal_manifest": "02_analysis/formal/compute_manifest.json",
    "formal_points": "02_analysis/formal/point_estimates.csv",
    "formal_intervals_reported": "02_analysis/formal/intervals_seed20261025.csv",
    "formal_intervals_stability": "02_analysis/formal/intervals_seed20261026.csv",
    "formal_axis_sensitivity": "02_analysis/formal/axis_sensitivity.csv",
    "formal_seed_stability": "02_analysis/formal/seed_stability.csv",
    "m1_joint_contributions": "02_analysis/reused/m1_m2_m3/M1_extra_joint_DD_contributions.csv",
    "m1_focus": "02_analysis/reused/m1_m2_m3/M1_focus_K83_PartI_malignant_DD.csv",
    "m3_contrasts": "02_analysis/reused/m1_m2_m3/M3_age_under75_contrasts.csv",
    "m3_rates": "02_analysis/reused/m1_m2_m3/M3_age_under75_standardized_rates.csv",
    "time_annual_rates": "02_analysis/reused/time_0_74/usbr_annual_standardized_rates.csv",
    "time_monitoring_points": "02_analysis/reused/time_0_74/usbr_monitoring_point_estimates.csv",
    "time_primary_intervals": "02_analysis/reused/time_0_74/usbr_primary_bootstrap_intervals.csv",
}

POINT_CONTEXT_FIELDS = (
    "estimand_id", "window_role", "case_definition", "us_axis", "br_axis",
    "n_US_U", "n_BR_U", "n_US_A", "n_BR_A", "n_US_R",
    "r_US_U_per_100k", "r_BR_U_per_100k", "r_US_A_per_100k",
    "r_BR_A_per_100k", "r_US_R_per_100k", "RR_U", "RR_A", "RR_R",
    "E_US", "E_BR", "RRR", "RD_U_per_100k", "RD_A_per_100k",
    "RD_R_per_100k", "DD_A_per_100k", "DD_R_per_100k",
    "count_support_status", "primary_measure", "interpretation_note",
)

TIME_FAMILY_METRICS = (
    "T_UCD_R", "T_any_R", "I_R", "T_UCD_A", "T_any_A", "I_A",
)
TIME_POINT_MAP = {
    "T_UCD_R": ("UCD", "T_relative_gap_change"),
    "T_any_R": ("any_mention", "T_relative_gap_change"),
    "I_R": ("DEFINITION_INTERACTION", "T_relative_gap_change"),
    "T_UCD_A": ("UCD", "T_absolute_gap_change_per_100k"),
    "T_any_A": ("any_mention", "T_absolute_gap_change_per_100k"),
    "I_A": ("DEFINITION_INTERACTION", "T_absolute_gap_change_per_100k"),
}
RECORD_RESTRICTION_CASES = ("full", "exclude_malignant_UCD", "no_malignancy_source_any")
RECORD_RESTRICTION_ENDPOINTS = ("UCD", "any_mention", "DD_any_minus_UCD")
RECORD_RESTRICTION_ENDPOINT_LABELS = {
    "UCD": ("U", "RD_U", "RR_U"),
    "any_mention": ("A", "RD_A", "RR_A"),
    "DD_any_minus_UCD": ("DD", "DD_A", "RRR"),
}
EXPECTED_PRIMARY_RESTRICTION_DD_RRR = {
    "full": (0.8544005936050134, 0.8196817213157902),
    "exclude_malignant_UCD": (0.2569637455380165, 0.8164916380569482),
    "no_malignancy_source_any": (0.26878266687159713, 0.8179765003845283),
}


class TableBuildError(RuntimeError):
    """Raised when a named source or frozen key fails validation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise TableBuildError(f"Cannot read JSON source {path}: {error}") from error
    if not isinstance(value, dict):
        raise TableBuildError(f"Expected a JSON object in {path}")
    return value


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as error:
        raise TableBuildError(f"Cannot read CSV source {path}: {error}") from error


def as_float(value: Any, *, field: str) -> float:
    if value in (None, ""):
        raise TableBuildError(f"Required numeric value is blank: {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise TableBuildError(f"Invalid number for {field}: {value!r}") from error
    if not math.isfinite(parsed):
        raise TableBuildError(f"Non-finite number for {field}: {value!r}")
    return parsed


def nullable_float(value: Any) -> float | str:
    return "" if value in (None, "") else as_float(value, field="source value")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise TableBuildError(f"Refusing to write an empty output table: {path.name}")
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = buffer.getvalue().encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rebuilds leave any byte-identical generated CSV untouched. This keeps
    # already-reviewed source-data artifacts stable when only the ledger or a
    # newly requested table changes.
    if path.is_file() and path.read_bytes() == content:
        return
    path.write_bytes(content)


def exact_filter(rows: Iterable[dict[str, str]], **criteria: str) -> list[dict[str, str]]:
    return [row for row in rows if all(row.get(key) == value for key, value in criteria.items())]


def keyed_index(rows: Iterable[dict[str, str]], keys: tuple[str, ...], *, label: str) -> dict[tuple[str, ...], dict[str, str]]:
    output: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in keys)
        if key in output:
            raise TableBuildError(f"Duplicate {label} source key: {key}")
        output[key] = row
    return output


def norm_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def validate_formal_manifest(project_root: Path, loaded: dict[str, Path], data: dict[str, list[dict[str, str]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = read_json(loaded["formal_manifest"])
    if manifest.get("schema") != "mcod_direct_compute_manifest_v1":
        raise TableBuildError("Formal compute manifest schema mismatch")
    if manifest.get("phase") != "formal" or manifest.get("execution_status") != "PASS":
        raise TableBuildError("Formal compute execution did not pass")
    acceptance = manifest.get("inference_acceptance", {})
    if manifest.get("inferential_acceptance_status") != "PASS" or acceptance.get("accepted") is not True:
        raise TableBuildError("Formal inferential-acceptance gate did not pass")
    if acceptance.get("observed_family_sizes_per_seed", {}).get(str(REPORTED_SEED)) != {
        "primary_overall_RRR_DD": 2, "secondary_fixed_code_RRR_H": 9,
    }:
        raise TableBuildError("Reported-seed adjusted-family size does not match the frozen contract")
    if acceptance.get("observed_family_sizes_per_seed", {}).get(str(STABILITY_SEED)) != {
        "primary_overall_RRR_DD": 2, "secondary_fixed_code_RRR_H": 9,
    }:
        raise TableBuildError("Stability-seed adjusted-family size does not match the frozen contract")
    seeds = {int(item.get("seed", -1)): item for item in manifest.get("seeds", [])}
    for seed in (REPORTED_SEED, STABILITY_SEED):
        if seed not in seeds or int(seeds[seed].get("draws", 0)) != 50_000:
            raise TableBuildError(f"Formal seed {seed} does not report 50,000 draws")
        if seeds[seed].get("phase") != "formal":
            raise TableBuildError(f"Formal seed {seed} has a non-formal phase")

    output_records = {str(item.get("basename")): item for item in manifest.get("outputs", [])}
    for name in ("formal_points", "formal_intervals_reported", "formal_intervals_stability", "formal_axis_sensitivity", "formal_seed_stability"):
        path = loaded[name]
        record = output_records.get(path.name)
        if not record:
            raise TableBuildError(f"Formal manifest does not bind output {path.name}")
        if int(record.get("bytes", -1)) != path.stat().st_size or record.get("sha256") != sha256_file(path):
            raise TableBuildError(f"Fetched formal output hash/size mismatch: {path.name}")

    points = data["formal_points"]
    intervals_by_seed = {
        REPORTED_SEED: data["formal_intervals_reported"],
        STABILITY_SEED: data["formal_intervals_stability"],
    }
    for seed, rows in intervals_by_seed.items():
        if not rows or {int(row["seed"]) for row in rows} != {seed}:
            raise TableBuildError(f"Formal interval file does not contain exactly seed {seed}")
        if any(row.get("phase") != "formal" for row in rows):
            raise TableBuildError(f"Formal interval file for seed {seed} has a non-formal row")
    if not points or any(row.get("window") not in {PRIMARY_WINDOW, SUPPORT_WINDOW} for row in points):
        raise TableBuildError("Formal point table has no rows or an unexpected window")

    expected_point_keys = {
        (window, group, axis)
        for window in (PRIMARY_WINDOW, SUPPORT_WINDOW)
        for group in ALL_GROUPS
        for axis in ("entity_any", "record_any")
    }
    point_index = keyed_index(points, ("window", "code_group", "axis_variant"), label="formal point")
    if set(point_index) != expected_point_keys:
        missing = sorted(expected_point_keys - set(point_index))
        extra = sorted(set(point_index) - expected_point_keys)
        raise TableBuildError(f"Formal point-table key set mismatch; missing={missing}, extra={extra}")
    return manifest, acceptance


def validate_source_map(project_root: Path, loaded: dict[str, Path]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    source_map = read_json(loaded["source_map"])
    if source_map.get("run_id") != RUN_ID:
        raise TableBuildError("Reused-result source map run_id mismatch")
    by_snapshot: dict[str, dict[str, Any]] = {}
    for item in source_map.get("items", []):
        snapshot = item.get("snapshot")
        if not snapshot:
            continue
        snapshot_path = Path(snapshot)
        if not snapshot_path.is_absolute():
            snapshot_path = project_root / snapshot_path
        by_snapshot[norm_path(snapshot_path)] = item
    reused_names = {
        "m1_joint_contributions", "m1_focus", "m3_contrasts", "m3_rates",
        "time_annual_rates", "time_monitoring_points", "time_primary_intervals",
    }
    identities: dict[str, dict[str, Any]] = {}
    for name in reused_names:
        path = loaded[name]
        item = by_snapshot.get(norm_path(path))
        if not item:
            raise TableBuildError(f"Source map does not bind reused input {path.name}")
        actual_sha = sha256_file(path)
        if item.get("sha256") != actual_sha or int(item.get("bytes", -1)) != path.stat().st_size:
            raise TableBuildError(f"Reused-source manifest hash/size mismatch: {path.name}")
        identities[name] = {
            "source_category": item.get("category", ""),
            "original_source": item.get("source", ""),
            "snapshot": str(path.relative_to(project_root)),
            "bytes": path.stat().st_size,
            "sha256": actual_sha,
            "source_map_sha256": item.get("sha256", ""),
            "reuse_status": item.get("status", ""),
        }
    return source_map, identities


def resolve_payload_identity(
    payload_manifest_path: Path,
    excluded_identity_path: Path,
    expected_payload_hash: str,
) -> dict[str, Any]:
    if not expected_payload_hash:
        raise TableBuildError("Formal compute manifest does not record the prepared payload-manifest hash")
    if payload_manifest_path.is_file():
        actual_payload_hash = sha256_file(payload_manifest_path)
        if actual_payload_hash != expected_payload_hash:
            raise TableBuildError("Local prepared payload manifest hash differs from formal compute input identity")
        return {
            "path": "analysis/prepared_payload/payload_manifest.json",
            "bytes": payload_manifest_path.stat().st_size,
            "sha256": actual_payload_hash,
            "status": "MATCHES_FORMAL_INPUT_MANIFEST",
        }
    if not excluded_identity_path.is_file():
        raise TableBuildError(
            "Prepared payload is absent and no explicit payload-exclusion identity is present"
        )
    identity = read_json(excluded_identity_path)
    if (
        identity.get("schema") != "excluded_prepared_payload_identity_v1"
        or identity.get("payload_included") is not False
        or identity.get("payload_manifest_sha256") != expected_payload_hash
    ):
        raise TableBuildError("Payload-exclusion identity does not match the formal input manifest")
    try:
        manifest_bytes = int(identity.get("payload_manifest_bytes", -1))
    except (TypeError, ValueError) as error:
        raise TableBuildError("Payload-exclusion identity has an invalid manifest size") from error
    if manifest_bytes <= 0:
        raise TableBuildError("Payload-exclusion identity has an invalid manifest size")
    return {
        "path": "EXCLUDED_FROM_PORTABLE_PACKAGE",
        "bytes": manifest_bytes,
        "sha256": expected_payload_hash,
        "status": "EXPECTED_HASH_RECORDED; PREPARED_PAYLOAD_AND_MANIFEST_EXCLUDED",
    }


def interval_unit(metric: str) -> str:
    if metric.startswith("H_"):
        return "log ratio"
    if metric.startswith("DD") or metric.startswith("RD") or metric.startswith("delta_DD"):
        return "deaths per 100000 population"
    if metric.startswith("delta_RRR"):
        return "ratio difference"
    if metric.startswith("RRR") or metric.startswith("RR_") or metric.startswith("E_") or metric.startswith("ratio_"):
        return "ratio"
    if metric.startswith("signed_RD"):
        return "deaths per 100000 population"
    if metric.startswith("signed_logRR"):
        return "log rate ratio"
    return "source metric unit; see metric label"


def interval_view(row: dict[str, str], manifest: dict[str, Any]) -> dict[str, Any]:
    families = {item.get("family_id"): item for item in manifest.get("multiplicity_families", [])}
    family_id = row.get("family_id", "")
    family = families.get(family_id, {})
    return {
        "estimand_id": row["estimand_id"],
        "metric_id": row["metric_id"],
        "metric": row["metric"],
        "estimate": nullable_float(row.get("point_estimate")),
        "estimate_unit": interval_unit(row["metric"]),
        "nominal_ci_level": nullable_float(row.get("nominal_level")),
        "nominal_ci_lower": nullable_float(row.get("nominal_lower")),
        "nominal_ci_upper": nullable_float(row.get("nominal_upper")),
        "nominal_ci_type": "paired Poisson(N_pattern) percentile interval; reported seed",
        "quantile_method": row.get("quantile_method", ""),
        "adjusted_ci_lower": nullable_float(row.get("adjusted_lower")),
        "adjusted_ci_upper": nullable_float(row.get("adjusted_upper")),
        "adjusted_per_contrast_level": nullable_float(row.get("adjusted_level")),
        "adjusted_ci_type": "post hoc multiplicity-adjusted percentile interval" if family_id else "",
        "family_id": family_id,
        "family_size": nullable_float(row.get("family_size")),
        "familywise_level": nullable_float(family.get("familywise_level")),
        "evidence_role": row.get("evidence_role", ""),
        "seed": int(row["seed"]),
        "seed_role": row.get("seed_role", ""),
        "draws_requested": int(row["draws_requested"]),
        "draws_valid": int(row["draws_valid"]),
        "invalid_repetitions": int(row["invalid_repetitions"]),
        "interval_status": row.get("status", ""),
    }


def attach_point_context(output: dict[str, Any], interval: dict[str, str], point_index: dict[tuple[str, str, str], dict[str, str]]) -> None:
    for field in POINT_CONTEXT_FIELDS:
        output[field] = ""
    variant = interval.get("axis_variant", "")
    point_variant = variant if variant in {"entity_any", "record_any"} else "entity_any" if variant in {"common_U", "common_A"} else ""
    if not point_variant:
        return
    point = point_index.get((interval["window"], interval["code_group"], point_variant))
    if point:
        for field in POINT_CONTEXT_FIELDS:
            output[field] = point.get(field, "")
        output["point_context_axis_variant"] = point_variant
    else:
        output["point_context_axis_variant"] = ""


def build_full_axis_table(
    interval_rows: list[dict[str, str]],
    point_index: dict[tuple[str, str, str], dict[str, str]],
    axis_rows: list[dict[str, str]],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    sensitivity_index = keyed_index(axis_rows, ("window", "code_group"), label="axis sensitivity")
    output: list[dict[str, Any]] = []
    for row in interval_rows:
        view = interval_view(row, manifest)
        view.update({
            "run_id": RUN_ID,
            "window": row["window"],
            "code_group": row["code_group"],
            "axis_variant": row["axis_variant"],
            "count_support_status": "",
            "point_context_axis_variant": "",
            "rate_interval_status": "NOT_COMPUTED_FOR_RATE_FIELDS",
        })
        attach_point_context(view, row, point_index)
        if row.get("axis_variant") == "structural_comparison":
            sensitivity = sensitivity_index.get((row["window"], row["code_group"]), {})
            for field in (
                "RRR_entity_axis", "RRR_record_axis", "RRR_record_minus_entity",
                "E_US_entity_axis", "E_US_record_axis", "E_US_record_to_entity_ratio",
                "DD_entity_axis_per_100k", "DD_record_axis_per_100k",
                "DD_record_minus_entity_per_100k",
            ):
                view[field] = sensitivity.get(field, "")
            view["structural_sensitivity_status"] = sensitivity.get("status", "")
        else:
            for field in (
                "RRR_entity_axis", "RRR_record_axis", "RRR_record_minus_entity",
                "E_US_entity_axis", "E_US_record_axis", "E_US_record_to_entity_ratio",
                "DD_entity_axis_per_100k", "DD_record_axis_per_100k",
                "DD_record_minus_entity_per_100k", "structural_sensitivity_status",
            ):
                view.setdefault(field, "")
        if view.get("count_support_status") == "" and view.get("point_context_axis_variant"):
            # Count support is a point-estimate property and rate/count CIs are
            # deliberately not imputed into the interval table.
            point = point_index[(row["window"], row["code_group"], view["point_context_axis_variant"])]
            view["count_support_status"] = point.get("count_support_status", "")
        output.append(view)
    return output


def table_row(point: dict[str, str], interval: dict[str, str], manifest: dict[str, Any], *, table_role: str) -> dict[str, Any]:
    result = {
        "run_id": RUN_ID,
        "table_role": table_role,
        "estimand_id": interval["estimand_id"],
        "window": point["window"],
        "window_role": point["window_role"],
        "case_definition": point["case_definition"],
        "code_group": point["code_group"],
        "axis_variant": point["axis_variant"],
        "us_axis": point["us_axis"],
        "br_axis": point["br_axis"],
        "metric": interval["metric"],
    }
    result.update(interval_view(interval, manifest))
    for field in POINT_CONTEXT_FIELDS:
        if field not in {"estimand_id"}:
            result[field] = point.get(field, "")
    result["rate_interval_status"] = "NOT_COMPUTED_FOR_RATE_FIELDS"
    return result


def build_main_tables(
    points: list[dict[str, str]],
    reported_intervals: list[dict[str, str]],
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str, str, str], dict[str, str]]]:
    point_index = keyed_index(points, ("window", "code_group", "axis_variant"), label="formal point")
    interval_index = keyed_index(reported_intervals, ("window", "code_group", "axis_variant", "metric"), label="reported interval")
    table1: list[dict[str, Any]] = []
    primary_point = point_index[(PRIMARY_WINDOW, PRIMARY_GROUP, "entity_any")]
    for metric in ("RRR", "DD"):
        interval = interval_index.get((PRIMARY_WINDOW, PRIMARY_GROUP, "entity_any", metric))
        if not interval or interval.get("nominal_lower", "") == "" or interval.get("nominal_upper", "") == "":
            raise TableBuildError(f"Primary overall {metric} nominal CI is missing")
        row = table_row(primary_point, interval, manifest, table_role="Table 1: primary overall, two scales")
        row["estimand_id"] = interval["estimand_id"]
        table1.append(row)

    table2: list[dict[str, Any]] = []
    for group in FIXED_EIGHT_GROUPS:
        point = point_index[(PRIMARY_WINDOW, group, "entity_any")]
        for metric in ("RRR", "DD"):
            interval = interval_index.get((PRIMARY_WINDOW, group, "entity_any", metric))
            if not interval:
                raise TableBuildError(f"Missing primary reported interval row for {group}/{metric}")
            if interval.get("nominal_lower", "") == "" or interval.get("nominal_upper", "") == "":
                raise TableBuildError(f"Missing nominal CI for primary {group}/{metric}")
            table2.append(table_row(point, interval, manifest, table_role="Table 2: eight fixed code groups, two scales"))
    return table1, table2, point_index


def build_support_tables(full_axis: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics = {"RRR", "DD"}
    support = [
        row for row in full_axis
        if row.get("window") == SUPPORT_WINDOW
        and row.get("code_group") in ALL_GROUPS
        and row.get("axis_variant") in {"entity_any", "record_any"}
        and row.get("metric") in metrics
    ]
    expected = len(ALL_GROUPS) * 2 * 2
    if len(support) != expected:
        raise TableBuildError(f"Expected {expected} 2012-2024 support rows, found {len(support)}")
    record_axis = [
        row for row in full_axis
        if row.get("window") == PRIMARY_WINDOW
        and row.get("code_group") in ALL_GROUPS
        and row.get("axis_variant") == "record_any"
        and row.get("metric") in metrics
    ]
    if len(record_axis) != len(ALL_GROUPS) * 2:
        raise TableBuildError("Primary record-axis RRR/DD table is incomplete")
    return support, record_axis


def build_h_heterogeneity(reported_intervals: list[dict[str, str]], points: list[dict[str, str]], manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [
        row for row in reported_intervals
        if row.get("window") == PRIMARY_WINDOW
        and row.get("code_group") == "K83.0_vs_K83.1"
        and row.get("axis_variant") == "entity_any"
        and row.get("metric") == "H_logRRR_K83.0_minus_K83.1"
    ]
    if len(candidates) != 1:
        raise TableBuildError(f"Expected exactly one direct K83 H interval; found {len(candidates)}")
    h = candidates[0]
    estimate = as_float(h["point_estimate"], field="H point estimate")
    h_nominal_lower = as_float(h["nominal_lower"], field="H nominal lower")
    h_nominal_upper = as_float(h["nominal_upper"], field="H nominal upper")
    h_adjusted_lower = as_float(h["adjusted_lower"], field="H adjusted lower")
    h_adjusted_upper = as_float(h["adjusted_upper"], field="H adjusted upper")
    exp_point = math.exp(estimate)
    exp_nominal = (math.exp(h_nominal_lower), math.exp(h_nominal_upper))
    exp_adjusted = (math.exp(h_adjusted_lower), math.exp(h_adjusted_upper))
    point_index = keyed_index(points, ("window", "code_group", "axis_variant"), label="formal point")
    k830 = as_float(point_index[(PRIMARY_WINDOW, "K83.0", "entity_any")]["RRR"], field="K83.0 RRR")
    k831 = as_float(point_index[(PRIMARY_WINDOW, "K83.1", "entity_any")]["RRR"], field="K83.1 RRR")
    expected_ratio = k830 / k831
    if not math.isclose(exp_point, expected_ratio, rel_tol=0, abs_tol=1e-10):
        raise TableBuildError("exp(H) does not equal the point-estimate RRR_K83.0/RRR_K83.1")
    row = {
        "run_id": RUN_ID,
        "estimand_id": h["estimand_id"],
        "metric_id": h["metric_id"],
        "window": PRIMARY_WINDOW,
        "code_group": "K83.0_vs_K83.1",
        "axis_variant": "entity_any",
        "metric": h["metric"],
        "H_log_RRR_difference": estimate,
        "H_log_RRR_nominal_ci95_lower": h_nominal_lower,
        "H_log_RRR_nominal_ci95_upper": h_nominal_upper,
        "H_log_RRR_adjusted_lower": h_adjusted_lower,
        "H_log_RRR_adjusted_upper": h_adjusted_upper,
        "expH_RRR_ratio": exp_point,
        "expH_nominal_ci95_lower": exp_nominal[0],
        "expH_nominal_ci95_upper": exp_nominal[1],
        "expH_adjusted_ci_lower": exp_adjusted[0],
        "expH_adjusted_ci_upper": exp_adjusted[1],
        "adjusted_per_contrast_level": as_float(h["adjusted_level"], field="H adjusted level"),
        "family_id": h["family_id"],
        "family_size": int(h["family_size"]),
        "familywise_level": 0.95,
        "evidence_role": h["evidence_role"],
        "expH_unit": "ratio of RRR_K83.0 to RRR_K83.1",
        "ci_method": h["quantile_method"],
        "status": h["status"],
        "interpretation_guardrail": "Direct relative-effect heterogeneity; not a causal contrast.",
    }
    json_row = {
        "estimand_id": h["estimand_id"],
        "metric_id": h["metric_id"],
        "H_log_RRR_difference": estimate,
        "H_nominal_ci95": [h_nominal_lower, h_nominal_upper],
        "H_adjusted_ci": [h_adjusted_lower, h_adjusted_upper],
        "expH_RRR_ratio": exp_point,
        "expH_nominal_ci95": list(exp_nominal),
        "expH_adjusted_ci": list(exp_adjusted),
        "adjusted_per_contrast_level": as_float(h["adjusted_level"], field="H adjusted level"),
        "family_id": h["family_id"],
        "family_size": int(h["family_size"]),
        "familywise_level": 0.95,
        "evidence_role": h["evidence_role"],
        "RRR_K83.0": k830,
        "RRR_K83.1": k831,
        "status": h["status"],
    }
    return row, json_row


def build_m1_tables(joint_rows: list[dict[str, str]], focus_rows: list[dict[str, str]], primary_point: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    selected = exact_filter(joint_rows, period=M1_PERIOD)
    if len(selected) != 240:
        raise TableBuildError(f"Expected exact 240-row M1 partition for {M1_PERIOD}; found {len(selected)}")
    family_sets = {row["family_set"] for row in selected}
    positions = {row["position_group"] for row in selected}
    ucd_groups = {row["ucd_group"] for row in selected}
    if (len(family_sets), len(positions), len(ucd_groups)) != (15, 4, 4):
        raise TableBuildError("M1 joint table no longer has the frozen 15×4×4 category structure")
    dd_key = "DD_contribution_BR_minus_US_per_100k"
    full_rows: list[dict[str, Any]] = []
    for row in selected:
        full_rows.append({
            "run_id": RUN_ID,
            "estimand_id": f"USBR_M1_joint_DD_{M1_PERIOD}_{row['family_set']}_{row['position_group']}_{row['ucd_group']}",
            **row,
            "contribution_unit": "deaths per 100000 population",
            "interval_status": "DESCRIPTIVE_POINT_ONLY_NO_CI",
            "interpretation_guardrail": "Signed compositional contribution; not a causal attribution.",
        })
    full_by_key = {
        (row["family_set"], row["position_group"], row["ucd_group"]): row
        for row in full_rows
    }
    total = math.fsum(as_float(row[dd_key], field=dd_key) for row in selected)
    formal_primary_dd = as_float(primary_point["DD_A_per_100k"], field="primary DD_A")
    closure_error = total - formal_primary_dd
    if abs(closure_error) > M1_CLOSURE_TOLERANCE:
        raise TableBuildError(f"M1 240-category closure fails: error={closure_error:.17g}")

    positive = sorted((row for row in selected if as_float(row[dd_key], field=dd_key) > 0), key=lambda row: (-float(row[dd_key]), row["family_set"], row["position_group"], row["ucd_group"]))
    negative = sorted((row for row in selected if as_float(row[dd_key], field=dd_key) < 0), key=lambda row: (float(row[dd_key]), row["family_set"], row["position_group"], row["ucd_group"]))
    top_positive = positive[:M1_TOP_N_PER_SIGN]
    top_negative = negative[:M1_TOP_N_PER_SIGN]
    chosen_keys = {(row["family_set"], row["position_group"], row["ucd_group"]) for row in top_positive + top_negative}
    omitted = [row for row in selected if (row["family_set"], row["position_group"], row["ucd_group"]) not in chosen_keys]
    remainder = {
        "run_id": RUN_ID,
        "estimand_id": f"USBR_M1_joint_DD_{M1_PERIOD}_remainder_after_top_{M1_TOP_N_PER_SIGN}_per_sign",
        "period": M1_PERIOD,
        "family_set": "REMAINDER",
        "position_group": "all_nonselected",
        "ucd_group": "all_nonselected",
        "BR_deaths": str(sum(int(row["BR_deaths"]) for row in omitted)),
        "US_deaths": str(sum(int(row["US_deaths"]) for row in omitted)),
        "BR_std_rate_per_100k": math.fsum(as_float(row["BR_std_rate_per_100k"], field="BR standardized rate") for row in omitted),
        "US_std_rate_per_100k": math.fsum(as_float(row["US_std_rate_per_100k"], field="US standardized rate") for row in omitted),
        dd_key: math.fsum(as_float(row[dd_key], field=dd_key) for row in omitted),
        "contribution_unit": "deaths per 100000 population",
        "interval_status": "DESCRIPTIVE_EXACT_SIGNED_SUM_NO_CI",
        "category_count": len(omitted),
        "interpretation_guardrail": "Exact algebraic sum of all nonselected categories; not a causal attribution.",
    }
    compact: list[dict[str, Any]] = []
    for rank, row in enumerate(top_positive, start=1):
        full_row = full_by_key[(row["family_set"], row["position_group"], row["ucd_group"])]
        compact.append({
            "selection": "top_positive",
            "selection_rank": rank,
            **full_row,
            "category_count": 1,
        })
    for rank, row in enumerate(top_negative, start=1):
        full_row = full_by_key[(row["family_set"], row["position_group"], row["ucd_group"])]
        compact.append({
            "selection": "top_negative",
            "selection_rank": rank,
            **full_row,
            "category_count": 1,
        })
    compact.append({
        "selection": "remainder",
        "selection_rank": "",
        **remainder,
    })
    compact_total = math.fsum(as_float(row[dd_key], field=dd_key) for row in compact)
    if not math.isclose(compact_total, total, rel_tol=0, abs_tol=1e-12):
        raise TableBuildError(f"M1 top-plus-remainder signed sum does not close: {compact_total - total:.17g}")

    focus_scope = exact_filter(focus_rows, period=PRIMARY_WINDOW, ucd_group=M1_FOCUS_UCD)
    if len(focus_scope) != 3:
        raise TableBuildError(f"Expected three M1 focus definitions for {PRIMARY_WINDOW}; found {len(focus_scope)}")
    matched_focus = exact_filter(focus_scope, focus_definition=M1_FOCUS_DEFINITION)
    if len(matched_focus) != 1:
        raise TableBuildError("Frozen M1 malignant-UCD/K83-in-family/only-Part-I focus key is not unique")
    primary_rd_u = as_float(primary_point["RD_U_per_100k"], field="primary RD_U")
    focus_output: list[dict[str, Any]] = []
    for row in focus_scope:
        focus_value = as_float(row["DD_contribution_BR_minus_US_per_100k"], field="M1 focus DD contribution")
        focus_output.append({
            "run_id": RUN_ID,
            "estimand_id": f"USBR_M1_focus_{row['period']}_{row['focus_definition']}_{row['ucd_group']}",
            **row,
            "focus_share_of_primary_DD_pct": 100.0 * focus_value / formal_primary_dd,
            "focus_share_of_primary_RD_U_pct": 100.0 * focus_value / primary_rd_u,
            "interval_status": "DESCRIPTIVE_POINT_ONLY_NO_CI",
            "contribution_unit": "deaths per 100000 population",
            "interpretation_guardrail": "Composition share only; denominator is the specified primary contrast, not a causal fraction.",
        })
    focus = matched_focus[0]
    focus_value = as_float(focus["DD_contribution_BR_minus_US_per_100k"], field="primary M1 focus")
    dd_as_pct_rd_u = 100.0 * formal_primary_dd / primary_rd_u
    focus_share_dd = 100.0 * focus_value / formal_primary_dd
    if not (52.0 <= dd_as_pct_rd_u <= 53.5):
        raise TableBuildError(f"Primary DD/RD_U check outside the expected verification band: {dd_as_pct_rd_u:.6g}%")
    summary = {
        "period": M1_PERIOD,
        "partition_expected_categories": 240,
        "partition_observed_categories": len(selected),
        "family_sets": len(family_sets),
        "position_groups": len(positions),
        "ucd_groups": len(ucd_groups),
        "signed_contribution_sum_per_100k": total,
        "formal_primary_DD_A_per_100k": formal_primary_dd,
        "closure_error_per_100k": closure_error,
        "closure_tolerance_per_100k": M1_CLOSURE_TOLERANCE,
        "closure_status": "PASS",
        "negative_category_count": len(negative),
        "top_n_per_sign": M1_TOP_N_PER_SIGN,
        "top_plus_remainder_sum_per_100k": compact_total,
        "top_plus_remainder_closure_error_per_100k": compact_total - total,
        "focus_estimand_id": f"USBR_M1_focus_{PRIMARY_WINDOW}_{M1_FOCUS_DEFINITION}_{M1_FOCUS_UCD}",
        "focus_contribution_per_100k": focus_value,
        "focus_share_of_primary_DD_pct": focus_share_dd,
        "primary_RD_U_per_100k": primary_rd_u,
        "primary_DD_as_pct_of_primary_RD_U": dd_as_pct_rd_u,
        "focus_share_of_primary_RD_U_pct": 100.0 * focus_value / primary_rd_u,
        "focus_ci_status": "NOT_COMPUTED",
        "focus_interpretation": "The 2012–2023 focus share uses the current primary DD denominator; this is descriptive composition, not causal attribution.",
    }
    return full_rows, compact, focus_output, summary


def build_m3_under75(contrast_rows: list[dict[str, str]], rate_rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contrasts = exact_filter(contrast_rows, period=PRIMARY_WINDOW, age_scope="0_74", case_definition="full")
    rates = exact_filter(rate_rows, period=PRIMARY_WINDOW, age_scope="0_74", case_definition="full")
    if len(contrasts) != 9 or len(rates) != 12:
        raise TableBuildError(f"M3 under-75 primary scope row counts changed: contrasts={len(contrasts)}, rates={len(rates)}")
    contrast_output = []
    for row in contrasts:
        contrast_output.append({
            "run_id": RUN_ID,
            "estimand_id": f"USBR_M3_{row['period']}_{row['age_scope']}_{row['case_definition']}_{row['code_group']}_{row['endpoint']}",
            **row,
            "interval_status": "REUSED_DESCRIPTIVE_POINT_ONLY_NO_CI",
        })
    rate_output = []
    for row in rates:
        rate_output.append({
            "run_id": RUN_ID,
            "estimand_id": f"USBR_M3_RATE_{row['period']}_{row['age_scope']}_{row['case_definition']}_{row['country']}_{row['code_group']}_{row['endpoint']}",
            **row,
            "rate_unit": "deaths per 100000 population",
            "rate_interval_status": "NOT_COMPUTED_IN_REUSED_M3_TABLE",
        })
    return contrast_output, rate_output


def build_primary_record_restrictions(contrast_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Select the exact all-age primary-window record-restriction contrasts."""
    selected = exact_filter(
        contrast_rows,
        period=PRIMARY_WINDOW,
        age_scope="all_age",
        code_group=PRIMARY_GROUP,
    )
    selected = [row for row in selected if row.get("case_definition") in RECORD_RESTRICTION_CASES]
    expected_keys = {
        (case_definition, endpoint)
        for case_definition in RECORD_RESTRICTION_CASES
        for endpoint in RECORD_RESTRICTION_ENDPOINTS
    }
    index = keyed_index(selected, ("case_definition", "endpoint"), label="primary record restriction")
    if set(index) != expected_keys:
        missing = sorted(expected_keys - set(index))
        extra = sorted(set(index) - expected_keys)
        raise TableBuildError(f"All-age primary record-restriction key set mismatch; missing={missing}, extra={extra}")
    output: list[dict[str, Any]] = []
    for case_definition in RECORD_RESTRICTION_CASES:
        for endpoint in RECORD_RESTRICTION_ENDPOINTS:
            row = index[(case_definition, endpoint)]
            endpoint_label, difference_metric, ratio_metric = RECORD_RESTRICTION_ENDPOINT_LABELS[endpoint]
            output.append({
                "run_id": RUN_ID,
                "estimand_id": f"USBR_M3_record_restriction_{row['period']}_{row['age_scope']}_{row['case_definition']}_{row['code_group']}_{row['endpoint']}",
                "difference_estimand_id": f"USBR_M3_{row['period']}_{row['age_scope']}_{row['case_definition']}_{row['code_group']}_{difference_metric}",
                "ratio_estimand_id": f"USBR_M3_{row['period']}_{row['age_scope']}_{row['case_definition']}_{row['code_group']}_{ratio_metric}",
                "endpoint_scale_label": endpoint_label,
                "absolute_metric": difference_metric,
                "absolute_estimate": nullable_float(row.get("BR_minus_US_rate_difference_per_100k")),
                "absolute_estimate_unit": "deaths per 100000 population",
                "ratio_metric": ratio_metric,
                "ratio_estimate": nullable_float(row.get("BR_over_US_rate_ratio")),
                "ratio_estimate_unit": "ratio",
                **row,
                "ci_status": "NOT_COMPUTED_IN_REUSED_M3_TABLE",
                "result_status": "REUSED_POINT_ONLY",
            })
    return output


def validate_record_restriction_checks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dd_rows = {row["case_definition"]: row for row in rows if row["endpoint"] == "DD_any_minus_UCD"}
    if set(dd_rows) != set(RECORD_RESTRICTION_CASES):
        raise TableBuildError("Primary record-restriction DD rows are incomplete")
    results: dict[str, Any] = {}
    for case_definition, (expected_dd, expected_rrr) in EXPECTED_PRIMARY_RESTRICTION_DD_RRR.items():
        row = dd_rows[case_definition]
        dd = as_float(row["absolute_estimate"], field=f"{case_definition} DD")
        rrr = as_float(row["ratio_estimate"], field=f"{case_definition} RRR")
        if not math.isclose(dd, expected_dd, rel_tol=0, abs_tol=1e-10):
            raise TableBuildError(f"Unexpected primary DD for record restriction {case_definition}: {dd:.17g}")
        if not math.isclose(rrr, expected_rrr, rel_tol=0, abs_tol=1e-10):
            raise TableBuildError(f"Unexpected primary RRR for record restriction {case_definition}: {rrr:.17g}")
        results[case_definition] = {
            "DD_A_per_100k": dd,
            "RRR": rrr,
            "ci_status": "NOT_COMPUTED",
            "dd_estimand_id": row["difference_estimand_id"],
            "rrr_estimand_id": row["ratio_estimand_id"],
        }
    return results


def build_annual_overall(annual_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    selected = exact_filter(annual_rows, age_scope="all_age", case_definition="full", code_group=PRIMARY_GROUP)
    selected.sort(key=lambda row: (int(row["year"]), row["country"], row["endpoint"]))
    if len(selected) != 52:
        raise TableBuildError(f"Expected 52 all-age annual overall source rows; found {len(selected)}")
    return [
        {
            "run_id": RUN_ID,
            "estimand_id": f"USBR_TIME_ANNUAL_{row['age_scope']}_{row['year']}_{row['case_definition']}_{row['country']}_{row['code_group']}_{row['endpoint']}",
            **row,
            "interval_status": "ANNUAL_RATE_CI_NOT_COMPUTED",
        }
        for row in selected
    ]


def build_time_six_family(interval_rows: list[dict[str, str]], monitoring_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    scope = exact_filter(interval_rows, analysis_role="primary", age_scope="0_74", case_definition="full", code_group=PRIMARY_GROUP)
    selected = [row for row in scope if row.get("metric") in TIME_FAMILY_METRICS]
    if len(selected) != len(TIME_FAMILY_METRICS) * 2:
        raise TableBuildError(f"Expected six 0–74 primary family metrics across two seeds; found {len(selected)}")
    if {row["seed"] for row in selected} != {"20261023", "20261024"}:
        raise TableBuildError("The reused 0–74 six-family set must contain exactly seeds 20261023 and 20261024")
    point_rows = exact_filter(monitoring_rows, age_scope="0_74", case_definition="full", code_group=PRIMARY_GROUP)
    monitor_index = keyed_index(point_rows, ("endpoint",), label="0-74 time point")
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in selected:
        metric = row["metric"]
        endpoint, point_field = TIME_POINT_MAP[metric]
        monitor = monitor_index.get((endpoint,))
        if not monitor:
            raise TableBuildError(f"Missing 0–74 monitoring point row for {metric}/{endpoint}")
        key = (row["seed"], metric)
        if key in seen:
            raise TableBuildError(f"Duplicate 0–74 six-family interval {key}")
        seen.add(key)
        point_value = as_float(row["estimate"], field=f"{metric} point estimate")
        if not math.isclose(point_value, as_float(monitor[point_field], field=point_field), rel_tol=0, abs_tol=1e-10):
            raise TableBuildError(f"Time interval point estimate does not match reused point table for {metric}")
        output.append({
            "run_id": RUN_ID,
            "estimand_id": f"USBR_TIME_0_74_{row['case_definition']}_{row['code_group']}_{metric}_seed{row['seed']}",
            "age_scope": row["age_scope"],
            "case_definition": row["case_definition"],
            "code_group": row["code_group"],
            "metric": metric,
            "seed": int(row["seed"]),
            "seed_role": "reported" if row["seed"] == "20261023" else "stability_check_only",
            "estimate": point_value,
            "estimate_unit": "log relative-gap change" if metric.endswith("_R") or metric == "I_R" else "deaths per 100000 population absolute-gap change",
            "nominal_ci95_lower": nullable_float(row.get("ci95_lower")),
            "nominal_ci95_upper": nullable_float(row.get("ci95_upper")),
            "family_ci_lower": nullable_float(row.get("family_ci_lower")),
            "family_ci_upper": nullable_float(row.get("family_ci_upper")),
            "family_confidence_level": nullable_float(row.get("family_confidence_level")),
            "family_alpha": nullable_float(row.get("family_alpha")),
            "family_members": len(TIME_FAMILY_METRICS),
            "familywise_level": 0.95,
            "interval_method": row.get("interval_method", ""),
            "interval_scope": row.get("interval_scope", ""),
            "valid_repetitions": int(row["valid_repetitions"]),
            "bootstrap_repetitions": int(row["bootstrap_repetitions"]),
            "endpoint": endpoint,
            "interaction_definition": monitor.get("interaction_definition", ""),
            "US_early_rate_per_100k": monitor.get("US_early_rate_per_100k", ""),
            "BR_early_rate_per_100k": monitor.get("BR_early_rate_per_100k", ""),
            "US_late_rate_per_100k": monitor.get("US_late_rate_per_100k", ""),
            "BR_late_rate_per_100k": monitor.get("BR_late_rate_per_100k", ""),
            "RR_early_BR_over_US": monitor.get("RR_early_BR_over_US", ""),
            "RR_late_BR_over_US": monitor.get("RR_late_BR_over_US", ""),
            "RD_early_BR_minus_US_per_100k": monitor.get("RD_early_BR_minus_US_per_100k", ""),
            "RD_late_BR_minus_US_per_100k": monitor.get("RD_late_BR_minus_US_per_100k", ""),
            "window_rule": monitor.get("window_rule", ""),
            "status": "REUSED_PRIMARY_0_74_TIME_RESULT",
        })
    output.sort(key=lambda row: (row["metric"], row["seed"]))
    if any(row["family_confidence_level"] in ("", None) for row in output):
        raise TableBuildError("A member of the six-family 0–74 set is missing its family-adjusted CI")
    return output


def source_identity(path: Path, project_root: Path, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": str(path.relative_to(project_root)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_all(project_root: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    loaded = {key: project_root / relative for key, relative in SOURCE_PATHS.items()}
    missing = [str(path) for path in loaded.values() if not path.is_file()]
    if missing:
        raise TableBuildError("Missing required exact-scope source(s): " + "; ".join(missing))
    data = {key: read_csv(path) for key, path in loaded.items() if key != "formal_manifest" and key != "source_map"}
    source_map, reused_identities = validate_source_map(project_root, loaded)
    formal_manifest, acceptance = validate_formal_manifest(project_root, loaded, data)

    points = data["formal_points"]
    reported_intervals = data["formal_intervals_reported"]
    stability_intervals = data["formal_intervals_stability"]
    point_index = keyed_index(points, ("window", "code_group", "axis_variant"), label="formal point")
    interval_index = keyed_index(reported_intervals, ("window", "code_group", "axis_variant", "metric"), label="reported interval")
    primary_point = point_index[(PRIMARY_WINDOW, PRIMARY_GROUP, "entity_any")]
    primary_dd = as_float(primary_point["DD_A_per_100k"], field="primary DD_A")
    primary_rd_u = as_float(primary_point["RD_U_per_100k"], field="primary RD_U")

    table1, table2, point_index = build_main_tables(points, reported_intervals, formal_manifest)
    full_axis = build_full_axis_table(reported_intervals, point_index, data["formal_axis_sensitivity"], formal_manifest)
    support_2024, primary_record_axis = build_support_tables(full_axis)
    h_table, h_json = build_h_heterogeneity(reported_intervals, points, formal_manifest)
    m1_full, m1_top, m1_focus, m1_summary = build_m1_tables(
        data["m1_joint_contributions"], data["m1_focus"], primary_point,
    )
    m3_contrasts, m3_rates = build_m3_under75(data["m3_contrasts"], data["m3_rates"])
    annual_overall = build_annual_overall(data["time_annual_rates"])
    time_six_family = build_time_six_family(data["time_primary_intervals"], data["time_monitoring_points"])
    primary_record_restrictions = build_primary_record_restrictions(data["m3_contrasts"])
    primary_record_restriction_checks = validate_record_restriction_checks(primary_record_restrictions)

    primary_metric_rows = {
        metric: table1_row
        for metric, table1_row in ((row["metric"], row) for row in table1)
    }
    primary_overall_json = {
        "window": PRIMARY_WINDOW,
        "window_role": primary_point["window_role"],
        "case_definition": primary_point["case_definition"],
        "axis_variant": primary_point["axis_variant"],
        "estimand_id": primary_point["estimand_id"],
        "RRR": {
            "estimand_id": primary_metric_rows["RRR"]["estimand_id"],
            "estimate": as_float(primary_metric_rows["RRR"]["estimate"], field="primary RRR"),
            "nominal_ci95": [as_float(primary_metric_rows["RRR"]["nominal_ci_lower"], field="RRR CI lower"), as_float(primary_metric_rows["RRR"]["nominal_ci_upper"], field="RRR CI upper")],
            "adjusted_ci": [as_float(primary_metric_rows["RRR"]["adjusted_ci_lower"], field="RRR adjusted lower"), as_float(primary_metric_rows["RRR"]["adjusted_ci_upper"], field="RRR adjusted upper")],
            "family_id": primary_metric_rows["RRR"]["family_id"],
            "familywise_level": primary_metric_rows["RRR"]["familywise_level"],
            "evidence_role": primary_metric_rows["RRR"]["evidence_role"],
        },
        "DD_A_per_100k": {
            "estimand_id": primary_metric_rows["DD"]["estimand_id"],
            "estimate": primary_dd,
            "nominal_ci95": [as_float(primary_metric_rows["DD"]["nominal_ci_lower"], field="DD CI lower"), as_float(primary_metric_rows["DD"]["nominal_ci_upper"], field="DD CI upper")],
            "adjusted_ci": [as_float(primary_metric_rows["DD"]["adjusted_ci_lower"], field="DD adjusted lower"), as_float(primary_metric_rows["DD"]["adjusted_ci_upper"], field="DD adjusted upper")],
            "family_id": primary_metric_rows["DD"]["family_id"],
            "familywise_level": primary_metric_rows["DD"]["familywise_level"],
            "unit": "deaths per 100000 population",
            "evidence_role": primary_metric_rows["DD"]["evidence_role"],
        },
        "RD_U_per_100k": primary_rd_u,
        "rates_per_100k": {field: as_float(primary_point[field], field=field) for field in ("r_US_U_per_100k", "r_BR_U_per_100k", "r_US_A_per_100k", "r_BR_A_per_100k")},
        "counts": {field: int(primary_point[field]) for field in ("n_US_U", "n_BR_U", "n_US_A", "n_BR_A")},
        "rate_confidence_intervals": "NOT_COMPUTED",
        "DD_as_pct_of_RD_U": 100.0 * primary_dd / primary_rd_u,
        "DD_as_pct_of_RD_U_formula": "100 * DD_A_per_100k / RD_U_per_100k",
    }

    fixed_eight_json = []
    for group in FIXED_EIGHT_GROUPS:
        group_rows = [row for row in table2 if row["code_group"] == group]
        fixed_eight_json.append({
            "code_group": group,
            "axis_variant": "entity_any",
            "RRR": {
                "estimand_id": next(row["estimand_id"] for row in group_rows if row["metric"] == "RRR"),
                "estimate": as_float(next(row["estimate"] for row in group_rows if row["metric"] == "RRR"), field=f"{group} RRR"),
                "nominal_ci95": [as_float(next(row["nominal_ci_lower"] for row in group_rows if row["metric"] == "RRR"), field="RRR lower"), as_float(next(row["nominal_ci_upper"] for row in group_rows if row["metric"] == "RRR"), field="RRR upper")],
                "adjusted_ci": [as_float(next(row["adjusted_ci_lower"] for row in group_rows if row["metric"] == "RRR"), field="RRR adjusted lower"), as_float(next(row["adjusted_ci_upper"] for row in group_rows if row["metric"] == "RRR"), field="RRR adjusted upper")],
                "family_id": next(row["family_id"] for row in group_rows if row["metric"] == "RRR"),
                "evidence_role": next(row["evidence_role"] for row in group_rows if row["metric"] == "RRR"),
            },
            "DD_A_per_100k": {
                "estimand_id": next(row["estimand_id"] for row in group_rows if row["metric"] == "DD"),
                "estimate": as_float(next(row["estimate"] for row in group_rows if row["metric"] == "DD"), field=f"{group} DD"),
                "nominal_ci95": [as_float(next(row["nominal_ci_lower"] for row in group_rows if row["metric"] == "DD"), field="DD lower"), as_float(next(row["nominal_ci_upper"] for row in group_rows if row["metric"] == "DD"), field="DD upper")],
                "adjusted_ci": None,
                "evidence_role": next(row["evidence_role"] for row in group_rows if row["metric"] == "DD"),
                "unit": "deaths per 100000 population",
            },
            "counts": {field: int(group_rows[0][field]) for field in ("n_US_U", "n_BR_U", "n_US_A", "n_BR_A")},
            "rates_per_100k": {field: as_float(group_rows[0][field], field=field) for field in ("r_US_U_per_100k", "r_BR_U_per_100k", "r_US_A_per_100k", "r_BR_A_per_100k")},
            "rate_confidence_intervals": "NOT_COMPUTED",
        })

    stability_index = keyed_index(stability_intervals, ("metric_id",), label="stability interval")
    family_stability_rows = [row for row in data["formal_seed_stability"] if row.get("stability_pass", "").lower() == "true"]
    if len(family_stability_rows) != 11:
        raise TableBuildError(f"Expected 11 passing formal seed-stability rows; found {len(family_stability_rows)}")
    if len(stability_index) < 11:
        raise TableBuildError("Stability interval output is missing the 11 adjusted-family members")

    expected_payload_hash = formal_manifest.get("input_payload_manifest_sha256", "")
    analysis_dir = Path(__file__).resolve().parent
    payload_identity = resolve_payload_identity(
        analysis_dir / "prepared_payload" / "payload_manifest.json",
        analysis_dir / "payload_identity.json",
        expected_payload_hash,
    )

    outputs: dict[str, list[dict[str, Any]]] = {
        "Table1_overall_two_scales.csv": table1,
        "Table2_fixed_eight_codes_two_scales.csv": table2,
        "Source_full_axis_effects_and_CIs.csv": full_axis,
        "Supplement_2012_2024_support.csv": support_2024,
        "Supplement_primary_record_axis.csv": primary_record_axis,
        "Supplement_K83_H_and_expH.csv": [h_table],
        "Supplement_M1_joint_all_240.csv": m1_full,
        "Supplement_M1_top_positive_negative_plus_remainder.csv": m1_top,
        "Supplement_M1_focus.csv": m1_focus,
        "Supplement_M3_under75_contrasts.csv": m3_contrasts,
        "Supplement_M3_under75_standardized_rates.csv": m3_rates,
        "Supplement_annual_overall_all_age.csv": annual_overall,
        "Supplement_time_0_74_six_family.csv": time_six_family,
        "Supplement_primary_record_restrictions.csv": primary_record_restrictions,
    }
    time_primary_reported = [row for row in time_six_family if row["seed_role"] == "reported"]
    result_json: dict[str, Any] = {
        "schema": "usbr_mcod_results_for_writing_v1",
        "run_id": RUN_ID,
        "status": "READY_FOR_WRITING",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "formal_analysis": {
            "phase": formal_manifest.get("phase"),
            "execution_status": formal_manifest.get("execution_status"),
            "inferential_acceptance_status": formal_manifest.get("inferential_acceptance_status"),
            "inferential_acceptance": acceptance,
            "draws_per_seed": 50_000,
            "reported_seed": REPORTED_SEED,
            "stability_only_seed": STABILITY_SEED,
            "requested_native_threads": formal_manifest.get("thread_budget", {}).get("requested_native_threads_per_worker"),
            "runtime_threadpools": formal_manifest.get("thread_budget", {}).get("runtime_threadpools"),
            "all_11_adjusted_family_stability_rows_pass": len(family_stability_rows) == 11,
            "formal_compute_manifest_sha256": sha256_file(loaded["formal_manifest"]),
            "formal_compute_script_sha256": formal_manifest.get("compute_script_sha256"),
            "prepared_payload_manifest": payload_identity,
        },
        "primary_overall": primary_overall_json,
        "fixed_eight_groups": fixed_eight_json,
        "record_axis_primary_support": [
            {field: row.get(field) for field in ("estimand_id", "metric_id", "window", "code_group", "axis_variant", "metric", "estimate", "estimate_unit", "nominal_ci_level", "nominal_ci_lower", "nominal_ci_upper", "family_id", "evidence_role", "interval_status")}
            for row in primary_record_axis
        ],
        "support_window_2012_2024": [
            {field: row.get(field) for field in ("estimand_id", "metric_id", "window", "code_group", "axis_variant", "metric", "estimate", "estimate_unit", "nominal_ci_level", "nominal_ci_lower", "nominal_ci_upper", "family_id", "evidence_role", "interval_status")}
            for row in support_2024
        ],
        "K83_direct_heterogeneity": h_json,
        "M1_composition": m1_summary,
        "M3_under75_primary_scope": {
            "period": PRIMARY_WINDOW,
            "age_scope": "0_74",
            "case_definition": "full",
            "contrast_rows": len(m3_contrasts),
            "standardized_rate_rows": len(m3_rates),
            "source_tables": ["M3_age_under75_contrasts.csv", "M3_age_under75_standardized_rates.csv"],
            "confidence_intervals": "No intervals are added; this is the exact reused M3 point-table scope.",
        },
        "primary_record_restrictions": {
            "period": PRIMARY_WINDOW,
            "age_scope": "all_age",
            "code_group": PRIMARY_GROUP,
            "case_definitions": list(RECORD_RESTRICTION_CASES),
            "endpoints": list(RECORD_RESTRICTION_ENDPOINTS),
            "endpoint_scale_labels": {endpoint: RECORD_RESTRICTION_ENDPOINT_LABELS[endpoint][0] for endpoint in RECORD_RESTRICTION_ENDPOINTS},
            "row_count": len(primary_record_restrictions),
            "results_by_case_definition": primary_record_restriction_checks,
            "results": [
                {
                    "case_definition": row["case_definition"],
                    "endpoint": row["endpoint"],
                    "endpoint_scale_label": row["endpoint_scale_label"],
                    "absolute_metric": row["absolute_metric"],
                    "absolute_estimate": row["absolute_estimate"],
                    "absolute_estimate_unit": row["absolute_estimate_unit"],
                    "ratio_metric": row["ratio_metric"],
                    "ratio_estimate": row["ratio_estimate"],
                    "ratio_estimate_unit": row["ratio_estimate_unit"],
                    "estimand_id": row["estimand_id"],
                    "ci_status": row["ci_status"],
                }
                for row in primary_record_restrictions
            ],
            "ci_status": "NOT_COMPUTED; reused point table only",
            "source_table": "M3_age_under75_contrasts.csv",
        },
        "annual_overall_all_age": {
            "source_table": "usbr_annual_standardized_rates.csv",
            "rows": len(annual_overall),
            "age_scope": "all_age",
            "case_definition": "full",
            "code_group": PRIMARY_GROUP,
            "periods": sorted({int(row["year"]) for row in annual_overall}),
            "rate_confidence_intervals": "NOT_COMPUTED",
        },
        "time_0_74_primary_six_family_reported_seed": time_primary_reported,
        "scope_keys": {
            "primary_window": PRIMARY_WINDOW,
            "support_window": SUPPORT_WINDOW,
            "primary_code_group": PRIMARY_GROUP,
            "fixed_eight_code_groups": list(FIXED_EIGHT_GROUPS),
            "primary_us_axis": "entity_any",
            "structural_sensitivity_us_axis": "record_any",
            "brazil_axis": "A_certificate_line",
            "reported_seed": REPORTED_SEED,
            "stability_seed_only": STABILITY_SEED,
            "m1_window": M1_PERIOD,
            "m1_focus_definition": M1_FOCUS_DEFINITION,
            "m1_focus_ucd_group": M1_FOCUS_UCD,
            "m1_top_categories_per_sign": M1_TOP_N_PER_SIGN,
            "m3_restriction": {"period": PRIMARY_WINDOW, "age_scope": "0_74", "case_definition": "full"},
            "time_six_family": {"age_scope": "0_74", "case_definition": "full", "code_group": PRIMARY_GROUP, "metrics": list(TIME_FAMILY_METRICS)},
        },
        "guardrails": [
            "Adjusted families are post hoc multiplicity-adjusted families; they are not described as pre-specified confirmatory families.",
            "All rate and count confidence intervals are omitted unless directly present in a source interval table.",
            "2012–2024 is a version-sensitive support window; the 2012–2023 window remains primary.",
            "US record-any is a structural sensitivity with Brazil remaining on the certificate-line A axis.",
            "M1 joint contributions and focus shares are descriptive composition, not causal attribution.",
            "The existing 0–74 time-family results are reused; no new resampling was performed.",
        ],
        "source_manifest": {
            "source_map_sha256": sha256_file(loaded["source_map"]),
            "formal_compute_manifest_sha256": sha256_file(loaded["formal_manifest"]),
            "formal_output_hashes_verified_against_compute_manifest": True,
            "reused_source_hashes_verified_against_source_map": True,
            "source_files": [
                source_identity(path, project_root, role)
                for role, path in loaded.items()
            ],
            "prepared_payload_manifest": payload_identity,
            "reused_sources": reused_identities,
        },
        "table_outputs": [],
    }
    return outputs, result_json


def output_directory(project_root: Path) -> tuple[Path, Path]:
    return project_root / "03_figures_tables" / "source_data", project_root / "02_analysis" / "RESULTS_FOR_WRITING.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT, help="Paper_Finalization_20261025 root with formal and reused summaries")
    parser.add_argument("--overwrite", action="store_true", help="replace only the exact generated files in their target locations")
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    table_dir, result_path = output_directory(project_root)
    try:
        outputs, results = build_all(project_root)
        intended_paths = [table_dir / name for name in outputs] + [result_path]
        existing = [path for path in intended_paths if path.exists()]
        if existing and not args.overwrite:
            raise TableBuildError("Generated target(s) already exist; pass --overwrite to replace only these named outputs: " + "; ".join(str(path) for path in existing))
        for name, rows in outputs.items():
            write_csv(table_dir / name, rows)
        script_path = Path(__file__).resolve()
        results["builder"] = {
            "script_path": str(script_path.relative_to(project_root)),
            "script_sha256": sha256_file(script_path),
            "no_new_resampling": True,
            "dependencies": "Python standard library only",
        }
        results["table_outputs"] = [
            {
                "path": str((table_dir / name).relative_to(project_root)),
                "rows": len(rows),
                "bytes": (table_dir / name).stat().st_size,
                "sha256": sha256_file(table_dir / name),
            }
            for name, rows in outputs.items()
        ]
        results["result_json_path"] = str(result_path.relative_to(project_root))
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({
            "status": "COMPLETED",
            "table_count": len(outputs),
            "table_rows": {name: len(rows) for name, rows in outputs.items()},
            "results_json": str(result_path.relative_to(project_root)),
            "primary_RRR": results["primary_overall"]["RRR"]["estimate"],
            "primary_DD_A_per_100k": results["primary_overall"]["DD_A_per_100k"]["estimate"],
            "M1_closure_error_per_100k": results["M1_composition"]["closure_error_per_100k"],
            "M1_focus_share_of_primary_DD_pct": results["M1_composition"]["focus_share_of_primary_DD_pct"],
            "primary_DD_as_pct_of_RD_U": results["M1_composition"]["primary_DD_as_pct_of_primary_RD_U"],
        }, ensure_ascii=False))
        return 0
    except (TableBuildError, OSError, ValueError, KeyError) as error:
        print(f"Table build failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
