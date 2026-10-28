#!/usr/bin/env python3
"""Portable, input-pinned replayers for the 2026-10-26 revision analyses.

The original scientific functions are loaded from the read-only source files
named in the input manifest. Only their path globals are overridden before
execution. No network access or raw-record scan is performed here.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import importlib.util
import json
import re
import sys
import types
from pathlib import Path
from typing import Any, Iterable


MANIFEST_NAME = "input_manifest.json"
MANIFEST_SCHEMA = "revision_adapter_inputs_v1"

# Relative to --input-root. The aggregate cache and joint-pattern file are
# intentionally external, not included in this adapter package.
INPUT_PATHS = {
    "residence_audit_source": "revision/code/audit_residence_missing.py",
    "br_parser_source": "helpers/extension/scripts/extract_br.py",
    "us_parser_source": "helpers/extension/scripts/extract_us.py",
    "residence_aggregate_cache": "revision/external/residence_missing_reconstructed_aggregate_cache.json",
    "frozen_population_936": "data/metadata/population.csv",
    "who18_weights": "data/metadata/who_2000_2025_18_group_weights_correct.csv",
    "population_runner_source": "revision/code/run_population_sensitivity.py",
    "population_pattern_file": "revision/external/usbr_joint_patterns.csv",
    "population_bridge_helper": "helpers/bridge/run_usbr_monitoring.py",
    "population_baseline_intervals": "revision/external/usbr_primary_bootstrap_intervals.csv",
    "census_intercensal_2010_2020": "data/population_sources/nc-est2020int-agesex-res.csv",
    "census_postcensal_2020": "data/population_sources/nc-est2020-agesex-res.csv",
    "census_postcensal_2024": "data/population_sources/nc-est2024-agesex-res.csv",
    "census_postcensal_2025": "data/population_sources/nc-est2025-agesex-res.csv",
}

# Published source hashes. The aggregate cache is locally redacted before
# distribution, so its checksum is pinned only by the user-supplied manifest.
KNOWN_SHA256 = {
    "residence_audit_source": "d74e7fdc9e43e0179a6820f3153b2776e511e98652e8a403876c5d8086aecc29",
    "br_parser_source": "b7c575e20929b4bbaf06452bd27db2bf4e4930516b2adf0d6f99c0211d9fa7bb",
    "us_parser_source": "b8ac39e0a07049160c833aa8cccffac9732d7834216c6d2a7632cf60d67f9c97",
    "frozen_population_936": "dab349662bf88652443ee78c1c0fc9de8b384171eb0a0d8564d7900a339d1851",
    "who18_weights": "5922fabf2758762c188eb91dae5e745d9328fee4978debef8e01f4bb9d5f9ba9",
    "population_runner_source": "ed925abb972da20fb4d05df2557abdb4a0d64a5bd1aadff8eec6e585404239ce",
    "population_pattern_file": "b2c7e77d7c51438a6919787087ca146c91f779f6ca3bd2c38c490178d34b14e8",
    "population_bridge_helper": "8240058159d7abc1eb6057350032d5af84ff636e80143de95d30ce8d24e58e6b",
    "population_baseline_intervals": "4b36407eb70d06ce29b6086f399b39635353639e377a1560914376479542752f",
    "census_intercensal_2010_2020": "494672c99edd48ebc3f8052a0fdae54083ef7bc1467d9c5394de47cd8a1525e3",
    "census_postcensal_2020": "03d7376535ae0e15233f40758e84f7692e2669452f7d2da71968eb4a3269e30b",
    "census_postcensal_2024": "fbb151e8ae8554283dcbf192e6b420658ea444ee7d8849988aa1d5615183edf1",
    "census_postcensal_2025": "6436d4f6972d415caf14cb20776807bc031fc9f1bfc9d6ff26055d592a2b0230",
}

RESIDENCE_OUTPUTS = (
    "all_age_and_0_74_bounds.csv",
    "effect_bounds.csv",
    "under75_BR_vs_US_rate_separation_checks.csv",
    "br_residence_scope_sensitivity.csv",
)
POPULATION_POINT_OUTPUTS = (
    "preinference/point_estimates_preinference.csv",
    "preinference/denominator_variants_0_74.csv",
    "preinference/denominator_cell_differences_0_74.csv",
)


class AdapterError(RuntimeError):
    """An input, checksum, or replay contract failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_manifest_path(root: Path, relative: str, role: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AdapterError(f"Input manifest path escapes --input-root for {role}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise AdapterError(f"Input manifest path escapes --input-root for {role}")
    return resolved


def verify_inputs(input_root: Path, roles: Iterable[str], manifest_path: Path | None = None) -> dict[str, dict[str, str]]:
    manifest_path = manifest_path or input_root / MANIFEST_NAME
    if not manifest_path.is_file():
        needed = "\n".join(f"  {role}: {INPUT_PATHS[role]}" for role in roles)
        raise AdapterError(
            f"Missing {MANIFEST_NAME}; create a checksum manifest for the required read-only inputs:\n{needed}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"Cannot read input manifest: {type(exc).__name__}") from exc
    if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(manifest.get("files"), dict):
        raise AdapterError(f"Input manifest must use schema {MANIFEST_SCHEMA!r} and contain a files object")

    verified: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for role in roles:
        expected_path = INPUT_PATHS[role]
        item = manifest["files"].get(role)
        if not isinstance(item, dict) or item.get("path") != expected_path:
            invalid.append(f"{role}: manifest path must be {expected_path}")
            continue
        expected_hash = str(item.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            invalid.append(f"{role}: manifest sha256 must contain 64 hexadecimal characters")
            continue
        path = _safe_manifest_path(input_root, expected_path, role)
        if not path.is_file():
            missing.append(f"{role}: {expected_path}")
            continue
        observed = sha256_file(path)
        if observed != expected_hash:
            invalid.append(f"{role}: sha256 mismatch (manifest {expected_hash}, observed {observed})")
            continue
        known = KNOWN_SHA256.get(role)
        if known and observed != known:
            invalid.append(f"{role}: source checksum differs from the published checksum ({observed})")
            continue
        verified[role] = {"path": expected_path, "sha256": observed}
    if missing or invalid:
        details = [*(f"missing: {item}" for item in missing), *(f"invalid: {item}" for item in invalid)]
        raise AdapterError("Input preflight failed:\n  " + "\n  ".join(details))
    return verified


def _check_output_dir(input_root: Path, output_dir: Path) -> Path:
    root = input_root.resolve()
    output = output_dir.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise AdapterError("--output-dir must be separate from --input-root")
    if output.exists() and any(output.iterdir()):
        raise AdapterError("--output-dir must be empty; existing files will not be overwritten")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _module_from_source(
    source_path: Path,
    module_name: str,
    overrides: dict[str, str],
    namespace: dict[str, Any],
) -> types.ModuleType:
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise AdapterError(f"Cannot parse source module for {module_name}: {type(exc).__name__}") from exc
    values = {key: ast.parse(expression, mode="eval").body for key, expression in overrides.items()}

    class PathOverrides(ast.NodeTransformer):
        def visit_Assign(self, node: ast.Assign):  # noqa: N802 - AST visitor protocol
            node = self.generic_visit(node)
            if any(isinstance(target, ast.Name) and target.id in values for target in node.targets):
                target = next(target.id for target in node.targets if isinstance(target, ast.Name) and target.id in values)
                node.value = values[target]
            return node

        def visit_AnnAssign(self, node: ast.AnnAssign):  # noqa: N802 - AST visitor protocol
            node = self.generic_visit(node)
            if isinstance(node.target, ast.Name) and node.target.id in values and node.value is not None:
                node.value = values[node.target.id]
            return node

    tree = ast.fix_missing_locations(PathOverrides().visit(tree))
    module = types.ModuleType(module_name)
    module.__file__ = str(source_path)
    module.__package__ = ""
    module.__dict__.update(namespace)
    sys.modules[module_name] = module
    try:
        exec(compile(tree, str(source_path), "exec"), module.__dict__)
    except Exception as exc:
        raise AdapterError(f"Loading {module_name} failed: {type(exc).__name__}: {exc}") from exc
    return module


def _read_population(path: Path, module: Any) -> dict[tuple[str, int, str, str], int]:
    result: dict[tuple[str, int, str, str], int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"country", "year", "age_group", "sex", "population"}
        if not required.issubset(reader.fieldnames or ()):
            raise AdapterError(f"Population input lacks fields: {sorted(required - set(reader.fieldnames or ())) }")
        for row in reader:
            key = (row["country"].strip(), int(row["year"]), row["age_group"].strip(), row["sex"].strip())
            if key[0] not in {"BR", "US"} or key[1] not in module.YEARS:
                continue
            value = int(row["population"])
            if key in result or value <= 0:
                raise AdapterError(f"Duplicate or nonpositive population cell: {key}")
            result[key] = value
    expected = {
        (country, year, age, sex)
        for country in ("BR", "US")
        for year in module.YEARS
        for age in module.AGE_GROUPS
        for sex in module.SEXES
    }
    if set(result) != expected:
        raise AdapterError(f"Frozen population grid mismatch: expected {len(expected)} cells, observed {len(result)}")
    return result


def _read_weights(path: Path, module: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        field = "weight" if "weight" in fields else "weight_normalized" if "weight_normalized" in fields else None
        if field is None or "age_group" not in fields:
            raise AdapterError("WHO18 weights require age_group plus weight or weight_normalized columns")
        for row in reader:
            age = row["age_group"].strip()
            if age in result:
                raise AdapterError(f"Duplicate WHO18 weight: {age}")
            result[age] = float(row[field])
    if set(result) != set(module.AGE_GROUPS) or abs(sum(result.values()) - 1.0) > 1e-9:
        raise AdapterError("WHO18 weights are incomplete or do not sum to one")
    return result


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise AdapterError(f"Refusing to write empty result: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _output_hashes(output_dir: Path, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    return {
        path.relative_to(output_dir).as_posix(): sha256_file(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file() and path.relative_to(output_dir).as_posix() not in excluded
    }


def _compare_exact(reference_dir: Path | None, output_dir: Path, names: Iterable[str]) -> dict[str, Any]:
    if reference_dir is None:
        return {"status": "NOT_REQUESTED"}
    differences = []
    for name in names:
        reference = reference_dir / name
        observed = output_dir / name
        if not reference.is_file():
            differences.append(f"missing reference: {name}")
        elif not observed.is_file() or reference.read_bytes() != observed.read_bytes():
            differences.append(f"content differs: {name}")
    return {"status": "PASS" if not differences else "FAIL", "differences": differences}


def run_residence(
    input_root: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
    reference_dir: Path | None = None,
) -> dict[str, Any]:
    roles = (
        "residence_audit_source",
        "br_parser_source",
        "us_parser_source",
        "residence_aggregate_cache",
        "frozen_population_936",
        "who18_weights",
    )
    input_root = input_root.resolve()
    output_dir = _check_output_dir(input_root, output_dir)
    verified = verify_inputs(input_root, roles, manifest_path)
    adapter_names = {
        "_ADAPTER_INPUT_ROOT": input_root,
        "_ADAPTER_OUTPUT_DIR": output_dir,
    }
    module = _module_from_source(
        input_root / INPUT_PATHS["residence_audit_source"],
        "_revision_adapter_residence_audit",
        {
            "WORK_DIR": "_ADAPTER_OUTPUT_DIR",
            "EXT_ROOT": "_ADAPTER_INPUT_ROOT / 'helpers' / 'extension'",
            "FROZEN_ROOT": "_ADAPTER_INPUT_ROOT / 'data'",
            "FROZEN_ANALYSIS": "_ADAPTER_INPUT_ROOT / 'data' / 'metadata' / 'population.csv'",
            "FROZEN_WEIGHTS": "_ADAPTER_INPUT_ROOT / 'data' / 'metadata' / 'who_2000_2025_18_group_weights_correct.csv'",
            "BR_RAW_DIR": "_ADAPTER_INPUT_ROOT / 'unused_raw' / 'br'",
            "US_RAW_DIR": "_ADAPTER_INPUT_ROOT / 'unused_raw' / 'us'",
            "FROZEN_LEDGER": "_ADAPTER_INPUT_ROOT / 'unused_ledger.csv'",
            "FROZEN_SOURCE_IDENTITY": "_ADAPTER_INPUT_ROOT / 'unused_source_identity.csv'",
            "PRIOR_TABNET": "_ADAPTER_INPUT_ROOT / 'unused_tabnet.csv'",
        },
        adapter_names,
    )
    populations = _read_population(input_root / INPUT_PATHS["frozen_population_936"], module)
    weights = _read_weights(input_root / INPUT_PATHS["who18_weights"], module)

    cache_path = input_root / INPUT_PATHS["residence_aggregate_cache"]
    scans, _ibge_metadata, _ibge_payload = module.load_aggregate_scan_cache(cache_path)
    _summary, missing, _codes, residence_strata, valid_strata, _source_metadata = module.flatten_scan_results(scans)
    rate_rows, contrast_rows, bounds = module.rate_bound_rows(
        valid_strata, missing, populations, weights, residence_strata,
    )
    residence_scope_rows = module.calculate_residence_scope_sensitivity(
        valid_strata, residence_strata, populations, weights,
    )
    for filename, rows in (
        ("all_age_and_0_74_bounds.csv", rate_rows),
        ("effect_bounds.csv", contrast_rows),
        ("under75_BR_vs_US_rate_separation_checks.csv", bounds["time_0_74_BR_vs_US_rate_separation_checks"]),
        ("br_residence_scope_sensitivity.csv", residence_scope_rows),
    ):
        _write_rows(output_dir / filename, rows)

    comparison = _compare_exact(reference_dir, output_dir, RESIDENCE_OUTPUTS)
    if comparison["status"] == "FAIL":
        raise AdapterError(f"Residence output comparison failed: {comparison['differences']}")
    summary = {
        "schema": "revision_adapter_replay_v1",
        "analysis": "residence_bounds_from_aggregate_cache",
        "status": "PASS",
        "network_access": "not_used",
        "raw_records_opened": False,
        "aggregate_cache_replayed": True,
        "cache_complete_scans": len(scans),
        "cache_sha256": verified["residence_aggregate_cache"]["sha256"],
        "input_sha256": {role: record["sha256"] for role, record in verified.items()},
        "reference_comparison": comparison,
        "scope_note": "Deterministic allocation bounds replay only; raw scans, TabNet/ledger identity checks, and source-record verification were not rerun.",
        "outputs": _output_hashes(output_dir),
    }
    (output_dir / "replay_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _denominator_contract() -> Any:
    age_groups = ("0-4",) + tuple(f"{lower}-{lower + 4}" for lower in range(5, 85, 5)) + ("85+",)
    return types.SimpleNamespace(
        YEARS=tuple(range(2012, 2025)),
        AGE_GROUPS=age_groups,
        AGE_GROUPS_0_74=age_groups[:15],
        SEXES=("F", "M"),
    )


def _population_source_module(input_root: Path, output_dir: Path) -> Any:
    return _module_from_source(
        input_root / INPUT_PATHS["population_runner_source"],
        "_revision_adapter_population_runner",
        {
            "OUTPUT_DIR": "_ADAPTER_OUTPUT_DIR",
            "SOURCE_DIR": "_ADAPTER_INPUT_ROOT / 'data' / 'population_sources'",
            "HELPER_PATH": "_ADAPTER_INPUT_ROOT / 'helpers' / 'bridge' / 'run_usbr_monitoring.py'",
            "BASELINE_DIR": "_ADAPTER_INPUT_ROOT / 'revision' / 'external'",
            "FROZEN_WEIGHTS_PATH": "_ADAPTER_INPUT_ROOT / 'data' / 'metadata' / 'who_2000_2025_18_group_weights_correct.csv'",
            "PATTERN_PATH": "_ADAPTER_INPUT_ROOT / 'revision' / 'external' / 'usbr_joint_patterns.csv'",
            "BASELINE_POPULATION_PATH": "_ADAPTER_INPUT_ROOT / 'data' / 'metadata' / 'population.csv'",
            "BASELINE_INTERVALS_PATH": "_ADAPTER_INPUT_ROOT / 'revision' / 'external' / 'usbr_primary_bootstrap_intervals.csv'",
            "BASELINE_POINTS_PATH": "_ADAPTER_INPUT_ROOT / 'revision' / 'external' / 'unused_point_reference.csv'",
        },
        {"_ADAPTER_INPUT_ROOT": input_root, "_ADAPTER_OUTPUT_DIR": output_dir},
    )


def _compare_population_points(reference_dir: Path | None, output_dir: Path) -> dict[str, Any]:
    if reference_dir is None:
        return {"status": "NOT_REQUESTED"}
    comparisons = []
    for filename in POPULATION_POINT_OUTPUTS:
        expected = reference_dir / filename
        observed = output_dir / filename
        if not expected.is_file() or not observed.is_file():
            comparisons.append({"file": filename, "status": "MISSING"})
            continue
        with expected.open("r", encoding="utf-8-sig", newline="") as stream:
            expected_rows = list(csv.DictReader(stream))
        with observed.open("r", encoding="utf-8-sig", newline="") as stream:
            observed_rows = list(csv.DictReader(stream))
        key_fields = ("variant", "metric") if "point_estimate" in (observed_rows[0] if observed_rows else {}) else ("variant", "country", "year", "age_group", "sex")
        expected_by_key = {tuple(row[field] for field in key_fields): row for row in expected_rows}
        observed_by_key = {tuple(row[field] for field in key_fields): row for row in observed_rows}
        failures = []
        if set(expected_by_key) != set(observed_by_key):
            failures.append("row keys differ")
        else:
            for key in expected_by_key:
                left, right = expected_by_key[key], observed_by_key[key]
                for field in left.keys() | right.keys():
                    if field == "point_estimate":
                        try:
                            if abs(float(left[field]) - float(right[field])) > 1e-12:
                                failures.append(f"{key}: {field} differs")
                        except (KeyError, ValueError):
                            failures.append(f"{key}: invalid {field}")
                    elif left.get(field) != right.get(field):
                        failures.append(f"{key}: {field} differs")
        comparisons.append({"file": filename, "status": "PASS" if not failures else "FAIL", "differences": failures[:10]})
    status = "PASS" if all(item["status"] == "PASS" for item in comparisons) else "FAIL"
    return {"status": status, "files": comparisons, "tolerance": 1e-12}


def run_population(
    input_root: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
    reference_dir: Path | None = None,
    formal: bool = False,
    denominators_only: bool = False,
) -> dict[str, Any]:
    if formal and denominators_only:
        raise AdapterError("--formal cannot be combined with --denominators-only")
    input_root = input_root.resolve()
    output_dir = _check_output_dir(input_root, output_dir)
    roles = [
        "population_runner_source",
        "frozen_population_936",
        "who18_weights",
        "census_intercensal_2010_2020",
        "census_postcensal_2020",
        "census_postcensal_2024",
        "census_postcensal_2025",
    ]
    if not denominators_only:
        roles.extend(("population_pattern_file", "population_bridge_helper"))
    if formal:
        roles.append("population_baseline_intervals")
    verified = verify_inputs(input_root, roles, manifest_path)
    module = _population_source_module(input_root, output_dir)
    if denominators_only:
        helper = _denominator_contract()
    else:
        helper = module.load_helper()

    variants, denominator_qc = module.make_population_variants(helper)
    baseline_population = _read_population(input_root / INPUT_PATHS["frozen_population_936"], helper)
    if variants["baseline_split_2020_2024"] != baseline_population:
        raise AdapterError("Baseline variant differs from the checksum-pinned frozen 936-cell population grid")
    weights = _read_weights(input_root / INPUT_PATHS["who18_weights"], helper)
    denominator_qc["frozen_936_population_grid"] = {
        "status": "PASS",
        "cells_checked": len(baseline_population),
        "all_baseline_population_values_match": True,
        "source_hashes": {
            "population": verified["frozen_population_936"]["sha256"],
            "who18_weights": verified["who18_weights"]["sha256"],
        },
    }
    if denominators_only:
        point_values = None
        module.write_csv(output_dir / "preinference" / "denominator_variants_0_74.csv", module.denominator_rows(variants, helper))
        module.write_csv(output_dir / "preinference" / "denominator_cell_differences_0_74.csv", module.denominator_difference_rows(variants, helper))
        analysis = "population_denominator_variants_only"
    else:
        pattern_data = module.load_pattern_data(helper)
        point_values = {
            variant: module.compute_point_values(helper, pattern_data, population, weights)
            for variant, population in variants.items()
        }
        module.run_point_stage(helper, variants, pattern_data, weights, denominator_qc,
                               {role: record["sha256"] for role, record in verified.items()})
        if formal:
            module.run_bootstrap_stage(helper, variants, pattern_data, weights, point_values,
                                       {role: record["sha256"] for role, record in verified.items()}, denominator_qc)
        analysis = "population_denominator_sensitivity_points" if not formal else "population_denominator_sensitivity_formal"

    if denominators_only:
        comparison_names = (
            "preinference/denominator_variants_0_74.csv",
            "preinference/denominator_cell_differences_0_74.csv",
        )
        comparison = _compare_exact(reference_dir, output_dir, comparison_names)
    else:
        comparison = _compare_population_points(reference_dir, output_dir)
    if comparison["status"] == "FAIL":
        raise AdapterError(f"Population reference comparison failed: {comparison}")

    summary = {
        "schema": "revision_adapter_replay_v1",
        "analysis": analysis,
        "status": "PASS",
        "network_access": "not_used",
        "bootstrap_run": bool(formal),
        "input_sha256": {role: record["sha256"] for role, record in verified.items()},
        "denominator_qc": denominator_qc,
        "point_estimates": point_values,
        "reference_comparison": comparison,
        "outputs": _output_hashes(output_dir),
    }
    (output_dir / "replay_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, choices=("residence", "population"))
    parser.add_argument("--input-root", required=True, type=Path, help="root of the checksum-manifested external input tree")
    parser.add_argument("--output-dir", required=True, type=Path, help="new or empty output directory, separate from input-root")
    parser.add_argument("--manifest", type=Path, help=f"checksum manifest (default: --input-root/{MANIFEST_NAME})")
    parser.add_argument("--reference-dir", type=Path, help="optional existing output directory for exact/numeric comparison")
    parser.add_argument("--formal", action="store_true", help="explicitly run the original full bootstrap after point estimates")
    parser.add_argument("--denominators-only", action="store_true", help="population-only source check; does not require death-pattern inputs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analysis == "residence" and (args.formal or args.denominators_only):
        parser.error("--formal and --denominators-only apply only to --analysis population")
    try:
        if args.analysis == "residence":
            result = run_residence(args.input_root, args.output_dir,
                                   manifest_path=args.manifest, reference_dir=args.reference_dir)
        else:
            result = run_population(args.input_root, args.output_dir,
                                    manifest_path=args.manifest, reference_dir=args.reference_dir,
                                    formal=args.formal, denominators_only=args.denominators_only)
    except Exception as exc:
        print(f"ADAPTER FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "analysis": result["analysis"],
                      "reference_comparison": result["reference_comparison"],
                      "outputs": result["outputs"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
