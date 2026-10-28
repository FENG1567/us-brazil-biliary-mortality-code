#!/usr/bin/env python3
"""Prepare checksum-bound joint-pattern inputs for direct US/BR MCOD inference.

This command only reads the release-selected retained gzip streams, frozen
population/weight inputs, and the corrected M2 reference. It does not perform
bootstrap inference and does not rescan raw death files. The bridge selector is
loaded as a module (its main entry point is not called) to preserve its exact
current-release input selection and identity checks.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

for _thread_env in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
):
    os.environ[_thread_env] = "1"


DEFAULT_BRIDGE_SCRIPT = Path(
    r"D:\MCOD_Research\USBR_MCOD_bridge_decision_20261022\scripts\usbr_monitoring\run_usbr_monitoring.py"
)
DEFAULT_EXTENSION_ROOT = Path(r"D:\MCOD_Research\USBR_MCOD_extension_20261021")
DEFAULT_PILOT_ROOT = Path(r"D:\MCOD_Research\USBR_MCOD_upgrade_pilot_20261021")
DEFAULT_FROZEN_ROOT = Path(r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build")
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.md"
GROUPS = (
    "K80-83_total", "K80", "K81", "K82", "K83", "K80-81",
    "K83.0", "K83.1", "K82.9",
)
GROUP_BITS = {name: 1 << i for i, name in enumerate(GROUPS)}
DEFINITIONS = ("full", "exclude_malignant_UCD", "no_malignancy_source_any")
ENDPOINTS = ("UCD", "any_mention")
WINDOWS = {
    "2012-2023": tuple(range(2012, 2024)),
    "2012-2024": tuple(range(2012, 2025)),
}
UNIT_PER_100K = 100_000.0
RATE_CLOSURE_TOLERANCE = 1e-8


class PreparationError(RuntimeError):
    """Raised when selected inputs, source coverage, or closure checks fail."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_bridge_helpers(
    script_path: Path,
    extension_root: Path,
    pilot_root: Path,
    frozen_root: Path,
) -> Any:
    """Import pure selector/validation helpers without running bridge main()."""
    if not script_path.is_file():
        raise PreparationError(f"Bridge selector script not found: {script_path}")
    spec = importlib.util.spec_from_file_location("mcod_bridge_readonly_helpers", script_path)
    if spec is None or spec.loader is None:
        raise PreparationError(f"Cannot load bridge selector module: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ROOT = script_path.parents[2]
    module.EXT = extension_root
    module.PILOT = pilot_root
    module.FROZEN = frozen_root
    module.CANONICAL_RETAINED_SCRIPT = pilot_root / "scripts" / "retained" / "run_retained_m1_m3.py"
    module.M2_REFERENCE = pilot_root / "outputs" / "retained_m2_filter_fix_20261021" / "M2_code_group_counts_by_stratum.csv"
    return module


def mask_for_codes(codes: Iterable[str], bridge: Any) -> int:
    mask = 0
    normalized = {bridge.norm_code(code) for code in codes if bridge.norm_code(code)}
    for code in normalized:
        for group, bit in GROUP_BITS.items():
            if bridge.matches_group(code, group):
                mask |= bit
    return mask


def parse_record_codes(row: dict[str, str], bridge: Any) -> set[str]:
    raw = bridge.parse_json_list(row, "record_codes")
    if any(not isinstance(value, str) for value in raw):
        raise PreparationError("US record_codes must contain strings")
    return {bridge.norm_code(value) for value in raw if bridge.norm_code(value)}


def parse_parser_flag(value: Any, field: str) -> bool:
    token = str(value).strip()
    if token in {"1", "true", "True"}:
        return True
    if token in {"0", "false", "False"}:
        return False
    raise PreparationError(f"Invalid retained parser flag {field}={value!r}")


def read_selected_patterns(
    candidates: dict[tuple[str, int], Path],
    identity: dict[str, Any],
    bridge: Any,
    population: dict[tuple[str, int, str, str], dict[str, int]],
) -> tuple[Counter, Counter, dict[str, Any]]:
    patterns: Counter[tuple[Any, ...]] = Counter()
    group_counts: Counter[tuple[Any, ...]] = Counter()
    coverage: Counter[tuple[Any, ...]] = Counter()
    expected_rows = bridge.identity_expected_rows(identity)
    file_checks: list[dict[str, Any]] = []
    read_total = 0
    expected_fields = {
        "US": {"country", "year", "source_row", "age_group", "sex", "ucd_code", "entity_codes", "record_codes", "any_common", "ucd_biliary", "alternative"},
        "BR": {"country", "year", "source_row", "age_group", "sex", "ucd_code", "line_codes", "any_common", "ucd_biliary"},
    }

    for country, year in sorted(candidates):
        path = candidates[(country, year)]
        row_count = 0
        previous_source_row = 0
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = expected_fields[country] - set(reader.fieldnames or [])
            if missing:
                raise PreparationError(f"Missing retained-stream columns in {path.name}: {sorted(missing)}")
            for row in reader:
                try:
                    observed_year = int(row["year"])
                    source_row = int(row["source_row"])
                except (KeyError, ValueError) as error:
                    raise PreparationError(f"Invalid row identity in {path.name}") from error
                if row.get("country") != country or observed_year != year:
                    raise PreparationError(f"Country/year differs from selected stream {path.name}")
                if source_row <= previous_source_row:
                    raise PreparationError(f"source_row is not strictly increasing in {path.name}")
                previous_source_row = source_row
                age, sex = row.get("age_group", ""), row.get("sex", "")
                cell = (country, year, age, sex)
                if cell not in population:
                    raise PreparationError(f"Retained record is outside frozen population grid: {cell}")

                ucd = bridge.norm_code(row.get("ucd_code", ""))
                source_codes, extra_codes = bridge.decode_source_codes(row, country)
                ucd_codes = {ucd} if ucd else set()
                u_mask = mask_for_codes(ucd_codes, bridge)
                a_mask = mask_for_codes(source_codes, bridge)
                if country == "US":
                    r_codes = ucd_codes | parse_record_codes(row, bridge)
                    r_mask: int | None = mask_for_codes(r_codes, bridge)
                    malignant_r: int | None = int(any(bridge.is_malignant(code) for code in r_codes))
                else:
                    r_mask = None
                    malignant_r = None

                if u_mask & ~a_mask:
                    raise PreparationError(f"U is not nested in A at {country}/{year}/{source_row}")
                if country == "US" and u_mask & ~(r_mask or 0):
                    raise PreparationError(f"U is not nested in record-axis R at {country}/{year}/{source_row}")
                total_bit = GROUP_BITS["K80-83_total"]
                if not (u_mask | a_mask | (r_mask or 0)) & total_bit:
                    raise PreparationError(f"Retained record has no K80-K83 hit on any available axis at {country}/{year}/{source_row}")

                parser_ucd_flag = parse_parser_flag(row.get("ucd_biliary"), "ucd_biliary")
                parser_any_flag = parse_parser_flag(row.get("any_common"), "any_common")
                if parser_ucd_flag != bool(u_mask & total_bit):
                    raise PreparationError(f"Parser UCD flag disagrees with normalized U mask at {country}/{year}/{source_row}")
                if parser_any_flag != bool(a_mask & total_bit):
                    raise PreparationError(f"Parser any_common flag disagrees with normalized A mask at {country}/{year}/{source_row}")
                if country == "US":
                    parser_record_flag = parse_parser_flag(row.get("alternative"), "alternative")
                    if parser_record_flag != bool((r_mask or 0) & total_bit):
                        raise PreparationError(f"Parser alternative flag disagrees with normalized R mask at US/{year}/{source_row}")

                malignant_ucd = int(bridge.is_malignant(ucd))
                malignant_a = int(any(bridge.is_malignant(code) for code in source_codes))
                pattern_key = (
                    country, year, age, sex, u_mask, a_mask, r_mask,
                    malignant_ucd, malignant_a, malignant_r,
                )
                patterns[pattern_key] += 1
                row_count += 1
                read_total += 1

                if u_mask & total_bit:
                    coverage[(country, year, "UCD_K80-83")] += 1
                if a_mask & total_bit:
                    coverage[(country, year, "A_K80-83")] += 1
                if r_mask is not None and r_mask & total_bit:
                    coverage[(country, year, "R_K80-83")] += 1
                if country == "US" and (r_mask or 0) & total_bit and not a_mask & total_bit:
                    coverage[(country, year, "R_only_K80-83")] += 1
                if country == "US" and a_mask & total_bit and not (r_mask or 0) & total_bit:
                    coverage[(country, year, "A_only_K80-83")] += 1

                for definition in DEFINITIONS:
                    include = (
                        definition == "full"
                        or (definition == "exclude_malignant_UCD" and not malignant_ucd)
                        or (definition == "no_malignancy_source_any" and not malignant_a)
                    )
                    if not include:
                        continue
                    for group, bit in GROUP_BITS.items():
                        if u_mask & bit:
                            group_counts[(country, year, age, sex, definition, group, "UCD")] += 1
                        if a_mask & bit:
                            group_counts[(country, year, age, sex, definition, group, "any_mention")] += 1

        if row_count != expected_rows[(country, year)]:
            raise PreparationError(
                f"Selected stream row count differs from release summary for {country}/{year}: "
                f"{row_count} != {expected_rows[(country, year)]}"
            )
        file_checks.append({
            "country": country,
            "year": year,
            "basename": path.name,
            "candidate_rows": row_count,
            "source_row_order": "strictly_increasing",
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })

    expected_total = sum(int(value) for value in identity["candidate_rows_by_country"].values())
    if read_total != expected_total:
        raise PreparationError(f"Selected stream total differs from identity audit: {read_total} != {expected_total}")
    if len(candidates) != 26:
        raise PreparationError(f"Expected 26 release-selected streams, found {len(candidates)}")
    return patterns, group_counts, {
        "candidate_rows_total": read_total,
        "candidate_rows_by_country": identity["candidate_rows_by_country"],
        "selected_stream_count": len(file_checks),
        "unique_joint_pattern_count": len(patterns),
        "file_checks": file_checks,
        "coverage_counts": coverage,
    }


def validate_m2_closure(
    group_counts: Counter,
    bridge: Any,
    rows_936: list[dict[str, str]],
    rows_3744: list[dict[str, str]],
    population: dict[tuple[str, int, str, str], dict[str, int]],
    weights: dict[str, float],
    m2_path: Path,
) -> dict[str, list[dict[str, Any]]]:
    if sha256_file(m2_path) != bridge.M2_REFERENCE_SHA256:
        raise PreparationError(f"Corrected M2 reference SHA mismatch: {m2_path}")
    reference_rows = read_csv(m2_path)
    m2_groups = tuple(bridge.M2_GROUPS)
    years = tuple(bridge.YEARS)
    ages = tuple(bridge.AGE_GROUPS)
    sexes = tuple(bridge.SEXES)
    expected_keys = {
        (country, year, age, sex, definition, group, endpoint)
        for country in ("US", "BR")
        for year in years
        for age in ages
        for sex in sexes
        for definition in DEFINITIONS
        for group in m2_groups
        for endpoint in ENDPOINTS
    }
    reference: dict[tuple[Any, ...], int] = {}
    for row in reference_rows:
        key = (
            row["country"], int(row["year"]), row["age_group"], row["sex"],
            row["case_definition"], row["code_group"], row["endpoint"],
        )
        if key in reference:
            raise PreparationError(f"Duplicate corrected M2 key: {key}")
        reference[key] = int(row["deaths"])
    if set(reference) != expected_keys:
        raise PreparationError(f"Corrected M2 reference keys differ from expected grid: {len(reference)} != {len(expected_keys)}")

    closure_m2: list[dict[str, Any]] = []
    for key in sorted(expected_keys):
        observed = int(group_counts.get(key, 0))
        expected = reference[key]
        match = observed == expected
        closure_m2.append({
            "country": key[0], "year": key[1], "age_group": key[2], "sex": key[3],
            "case_definition": key[4], "code_group": key[5], "endpoint": key[6],
            "observed_deaths": observed, "reference_deaths": expected, "match": match,
        })
        if not match:
            raise PreparationError(f"Corrected M2 count mismatch at {key}: {observed} != {expected}")

    closure_936: list[dict[str, Any]] = []
    for row in rows_936:
        country, year, age, sex = row["country"], int(row["year"]), row["age_group"], row["sex"]
        for endpoint, source_field in (("UCD", "ucd"), ("any_mention", "any_common")):
            key = (country, year, age, sex, "full", "K80-83_total", endpoint)
            observed = int(group_counts.get(key, 0))
            expected = int(row[source_field])
            if observed != expected:
                raise PreparationError(f"Frozen 936 closure mismatch at {key}: {observed} != {expected}")
            closure_936.append({
                "country": country, "year": year, "age_group": age, "sex": sex,
                "endpoint": endpoint, "observed_deaths": observed,
                "reference_936_deaths": expected, "match": True,
            })
        frozen_pop = int(row["population"])
        if population[(country, year, age, sex)]["population"] != frozen_pop:
            raise PreparationError(f"Frozen population table mismatch at {(country, year, age, sex)}")

    closure_3744: list[dict[str, Any]] = []
    for row in rows_3744:
        country, year, age, sex, group = row["country"], int(row["year"]), row["age_group"], row["sex"], row["subcode"]
        for endpoint, source_field in (("UCD", "ucd"), ("any_mention", "any_common")):
            key = (country, year, age, sex, "full", group, endpoint)
            observed = int(group_counts.get(key, 0))
            expected = int(row[source_field])
            if observed != expected:
                raise PreparationError(f"Frozen 3744 closure mismatch at {key}: {observed} != {expected}")
            closure_3744.append({
                "country": country, "year": year, "age_group": age, "sex": sex,
                "code_group": group, "endpoint": endpoint, "observed_deaths": observed,
                "reference_3744_deaths": expected, "match": True,
            })

    window_closure: list[dict[str, Any]] = []
    for window, window_years in WINDOWS.items():
        for country in ("US", "BR"):
            groups = tuple(m2_groups) + ("K80-83_total",)
            definitions = DEFINITIONS if country in {"US", "BR"} else ()
            for definition in definitions:
                for group in groups:
                    if group == "K80-83_total" and definition != "full":
                        continue
                    for endpoint in ENDPOINTS:
                        observed_deaths = sum(
                            int(group_counts.get((country, year, age, sex, definition, group, endpoint), 0))
                            for year in window_years for age in ages for sex in sexes
                        )
                        if group == "K80-83_total":
                            ref_by_cell = {
                                (row["country"], int(row["year"]), row["age_group"], row["sex"], row["endpoint"]): int(row["reference_936_deaths"])
                                for row in closure_936
                            }
                            expected_deaths = sum(
                                ref_by_cell[(country, year, age, sex, endpoint)]
                                for year in window_years for age in ages for sex in sexes
                            )
                        else:
                            expected_deaths = sum(
                                reference[(country, year, age, sex, definition, group, endpoint)]
                                for year in window_years for age in ages for sex in sexes
                            )
                        if observed_deaths != expected_deaths:
                            raise PreparationError(f"Window death-total closure failed: {(window, country, definition, group, endpoint)}")

                        observed_rate = 0.0
                        reference_rate = 0.0
                        for year in window_years:
                            for age in ages:
                                age_weight = weights[age]
                                for sex in sexes:
                                    pop = population[(country, year, age, sex)]["population"]
                                    if pop <= 0:
                                        raise PreparationError(f"Nonpositive population at {(country, year, age, sex)}")
                                    cell_key = (country, year, age, sex, definition, group, endpoint)
                                    cell_count = int(group_counts.get(cell_key, 0))
                                    observed_rate += age_weight * 0.5 / len(window_years) * cell_count / pop * UNIT_PER_100K
                                    if group == "K80-83_total":
                                        ref_n = ref_by_cell[(country, year, age, sex, endpoint)]
                                    else:
                                        ref_n = reference[(country, year, age, sex, definition, group, endpoint)]
                                    reference_rate += age_weight * 0.5 / len(window_years) * ref_n / pop * UNIT_PER_100K
                        difference = observed_rate - reference_rate
                        rate_match = abs(difference) <= RATE_CLOSURE_TOLERANCE
                        if not rate_match:
                            raise PreparationError(f"Window direct-rate closure failed: {(window, country, definition, group, endpoint)} ({difference})")
                        window_closure.append({
                            "window": window, "country": country, "case_definition": definition,
                            "code_group": group, "endpoint": endpoint,
                            "observed_deaths": observed_deaths, "reference_deaths": expected_deaths,
                            "deaths_match": True, "observed_rate_per_100k": observed_rate,
                            "reference_rate_per_100k": reference_rate,
                            "rate_difference_per_100k": difference,
                            "rate_tolerance_per_100k": RATE_CLOSURE_TOLERANCE,
                            "rate_match": rate_match,
                        })

    if len(closure_m2) != len(expected_keys) or len(closure_936) != 1872 or len(closure_3744) != 7488:
        raise PreparationError("Closure table dimensions do not match the frozen input grids")
    if sum(weights.values()) <= 0 or abs(sum(weights.values()) - 1.0) > 1e-12:
        raise PreparationError("WHO18 weights do not sum to one")
    return {
        "m2_strata": closure_m2,
        "frozen_936": closure_936,
        "frozen_3744": closure_3744,
        "windows": window_closure,
    }


def file_record(path: Path, role: str) -> dict[str, Any]:
    return {"role": role, "path": str(path), "basename": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise PreparationError(f"Output directory is nonempty; pass --overwrite to replace its payload: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_path = args.contract_path.resolve()
    if not contract_path.is_file():
        raise PreparationError(f"Frozen contract not found: {contract_path}")
    bridge = load_bridge_helpers(args.bridge_script.resolve(), args.extension_root.resolve(), args.pilot_root.resolve(), args.frozen_root.resolve())

    candidates, summaries, manifest_info = bridge.expected_candidate_inputs()
    identity = bridge.check_input_identity(candidates, summaries, manifest_info)
    population, weights_raw, rows_3744, rows_936, frozen_metadata = bridge.load_frozen_inputs()
    if len(population) != 936 or any(int(row["population"]) <= 0 for row in population.values()):
        raise PreparationError("Frozen population must contain 936 positive unique cells")
    if len(weights_raw) != 18 or abs(sum(weights_raw.values()) - 1.0) > 1e-12:
        raise PreparationError("Frozen WHO18 weights must contain 18 entries and sum to one")
    weight_sum = sum(weights_raw.values())
    weights = {age: float(weights_raw[age]) / weight_sum for age in bridge.AGE_GROUPS}

    patterns, group_counts, read_checks = read_selected_patterns(candidates, identity, bridge, population)
    closure = validate_m2_closure(
        group_counts, bridge, rows_936, rows_3744, population, weights,
        bridge.M2_REFERENCE,
    )

    pattern_fields = [
        "country", "year", "age_group", "sex", "U_mask", "A_mask", "R_mask",
        "malignant_UCD", "malignant_A", "malignant_R", "N_pattern",
    ]
    pattern_rows = []
    for key, count in sorted(patterns.items()):
        country, year, age, sex, u_mask, a_mask, r_mask, malignant_ucd, malignant_a, malignant_r = key
        pattern_rows.append({
            "country": country, "year": year, "age_group": age, "sex": sex,
            "U_mask": u_mask, "A_mask": a_mask, "R_mask": "" if r_mask is None else r_mask,
            "malignant_UCD": malignant_ucd, "malignant_A": malignant_a,
            "malignant_R": "" if malignant_r is None else malignant_r,
            "N_pattern": count,
        })
    pattern_path = output_dir / "joint_patterns.csv.gz"
    with gzip.open(pattern_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=pattern_fields, extrasaction="raise", lineterminator="\n")
        writer.writeheader()
        writer.writerows(pattern_rows)

    population_rows = [
        {"country": c, "year": y, "age_group": a, "sex": s, "population": v["population"]}
        for (c, y, a, s), v in sorted(population.items())
    ]
    write_csv(output_dir / "population.csv", list(population_rows[0]), population_rows)
    weight_rows = [
        {"age_group": age, "weight_raw": weights_raw[age], "weight_normalized": weights[age]}
        for age in bridge.AGE_GROUPS
    ]
    write_csv(output_dir / "who18_weights.csv", list(weight_rows[0]), weight_rows)
    write_csv(output_dir / "closure_m2_strata.csv", list(closure["m2_strata"][0]), closure["m2_strata"])
    write_csv(output_dir / "closure_frozen_936.csv", list(closure["frozen_936"][0]), closure["frozen_936"])
    write_csv(output_dir / "closure_frozen_3744.csv", list(closure["frozen_3744"][0]), closure["frozen_3744"])
    write_csv(output_dir / "closure_windows.csv", list(closure["windows"][0]), closure["windows"])

    source_records: list[dict[str, Any]] = []
    source_records.append(file_record(manifest_info["manifest_path"], "release_manifest"))
    source_records.extend(identity["input_hashes"])
    for path in (args.extension_root / "scripts" / "extract_us.py", args.extension_root / "scripts" / "extract_br.py", bridge.CANONICAL_RETAINED_SCRIPT):
        source_records.append(file_record(path, "parser_or_canonical_retained_script"))
    for name, path in frozen_metadata["paths"].items():
        source_records.append({"role": "frozen_population_or_weight_input", "path": path, "basename": Path(path).name, "bytes": Path(path).stat().st_size, "sha256": frozen_metadata["sha256"][name]})
    source_records.append(file_record(bridge.M2_REFERENCE, "corrected_m2_reference"))
    source_records.append(file_record(contract_path, "frozen_analysis_contract"))
    source_records.append(file_record(Path(__file__).resolve(), "preparation_script"))
    selected_streams = []
    for key in sorted(candidates):
        c, y = key
        candidate = candidates[key]
        summary = summaries[key]
        selected_streams.append({
            "country": c, "year": y,
            "candidate_basename": candidate.name,
            "candidate_path": str(candidate),
            "candidate_bytes": candidate.stat().st_size,
            "candidate_sha256": sha256_file(candidate),
            "summary_basename": summary.name,
            "summary_sha256": sha256_file(summary),
            "expected_rows": identity["expected_rows_by_country_year"][f"{c}_{y}"],
        })

    payload_files = []
    for name in (
        "joint_patterns.csv.gz", "population.csv", "who18_weights.csv",
        "closure_m2_strata.csv", "closure_frozen_936.csv",
        "closure_frozen_3744.csv", "closure_windows.csv",
    ):
        payload_files.append(file_record(output_dir / name, "prepared_payload"))
    coverage_rows = []
    for (country, year, label), count in sorted(read_checks["coverage_counts"].items()):
        coverage_rows.append({"country": country, "year": year, "axis_count": label, "deaths": count})
    write_csv(output_dir / "source_coverage.csv", list(coverage_rows[0]) if coverage_rows else ["country", "year", "axis_count", "deaths"], coverage_rows)
    payload_files.append(file_record(output_dir / "source_coverage.csv", "prepared_payload"))

    qc = {
        "schema": "mcod_direct_joint_patterns_v1",
        "status": "PASS",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_scope": "26 release-selected retained US/BR candidate gzip streams; no raw-death rescan",
        "selected_stream_count": read_checks["selected_stream_count"],
        "candidate_rows_total": read_checks["candidate_rows_total"],
        "candidate_rows_by_country": read_checks["candidate_rows_by_country"],
        "joint_pattern_rows": len(pattern_rows),
        "groups_and_mask_bits": {group: GROUP_BITS[group] for group in GROUPS},
        "definition": {
            "U": "group-specific UCD mask",
            "A_US": "UCD union released entity-axis codes",
            "A_BR": "UCD union released certificate-line codes",
            "R_US": "UCD union released US record-axis codes",
            "R_BR": "not defined; blank in payload",
        },
        "validation": {
            "m2_strata_rows": len(closure["m2_strata"]),
            "m2_strata_mismatches": 0,
            "frozen_936_rows": len(closure["frozen_936"]),
            "frozen_3744_rows": len(closure["frozen_3744"]),
            "window_closure_rows": len(closure["windows"]),
            "window_rate_tolerance_per_100k": RATE_CLOSURE_TOLERANCE,
            "who18_raw_sum": sum(weights_raw.values()),
            "who18_normalized_sum": sum(weights.values()),
            "population_cells": len(population),
            "all_population_cells_positive": True,
            "U_subset_A_and_US_R": True,
            "parser_axis_flag_agreement": True,
        },
        "environment": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "numpy_version": __import__("numpy").__version__,
            "native_thread_limits": 1,
            "inference_run": False,
        },
        "read_checks": {k: v for k, v in read_checks.items() if k != "coverage_counts"},
    }
    write_json(output_dir / "prepare_qc.json", qc)
    payload_files.append(file_record(output_dir / "prepare_qc.json", "prepared_payload_qc"))
    input_manifest = {
        "schema": "mcod_direct_input_manifest_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "release_manifest_path": str(manifest_info["manifest_path"]),
        "release_manifest_sha256": identity["release_manifest_sha256"],
        "source_inputs": source_records,
        "selected_candidate_streams": selected_streams,
        "helper_script": file_record(args.bridge_script.resolve(), "bridge_selector_helpers"),
        "current_m2_reference": file_record(bridge.M2_REFERENCE, "corrected_m2_reference"),
        "contract": file_record(contract_path, "frozen_analysis_contract"),
        "preparation_script": file_record(Path(__file__).resolve(), "preparation_script"),
    }
    write_json(output_dir / "input_manifest.json", input_manifest)
    payload_files.append(file_record(output_dir / "input_manifest.json", "prepared_payload_manifest"))
    payload_manifest = {
        "schema": "mcod_direct_payload_manifest_v1",
        "payload_status": "READY_FOR_COMPUTE",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "contract_path": str(contract_path),
        "contract_sha256": sha256_file(contract_path),
        "preparation_script_sha256": sha256_file(Path(__file__).resolve()),
        "files": payload_files,
        "group_order": list(GROUPS),
        "windows": {name: list(years) for name, years in WINDOWS.items()},
        "standardization": {
            "age_weights": "normalized frozen WHO18 weights",
            "sex_weights": {"F": 0.5, "M": 0.5},
            "year_weights": "equal within each window",
            "population_denominators": "fixed 936-cell frozen population table",
            "rate_unit": "per 100000",
        },
        "resampling_contract": {
            "pattern_draw": "independent Nstar ~ Poisson(N_pattern) within country/year/age/sex/joint-pattern cells",
            "shared_membership": "one pattern count reused across U/A/R, groups, windows, and metrics",
            "formal_bootstrap_run": False,
        },
        "closure_summary": qc["validation"],
    }
    write_json(output_dir / "payload_manifest.json", payload_manifest)
    return {
        "status": "PASS",
        "output_dir": str(output_dir),
        "candidate_rows": read_checks["candidate_rows_total"],
        "joint_patterns": len(pattern_rows),
        "m2_closure_rows": len(closure["m2_strata"]),
        "window_closure_rows": len(closure["windows"]),
        "inference_run": False,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-script", type=Path, default=DEFAULT_BRIDGE_SCRIPT)
    parser.add_argument("--extension-root", type=Path, default=DEFAULT_EXTENSION_ROOT)
    parser.add_argument("--pilot-root", type=Path, default=DEFAULT_PILOT_ROOT)
    parser.add_argument("--frozen-root", type=Path, default=DEFAULT_FROZEN_ROOT)
    parser.add_argument("--contract-path", type=Path, default=DEFAULT_CONTRACT_PATH)
    parser.add_argument("--output-dir", type=Path, required=True, help="new local payload directory")
    parser.add_argument("--overwrite", action="store_true", help="replace files in a nonempty output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = prepare(args)
    except Exception as error:
        print(f"Preparation failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
