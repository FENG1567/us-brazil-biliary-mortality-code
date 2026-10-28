#!/usr/bin/env python3
"""Aggregate current-parser retained biliary records for exploratory M1-M3."""

from __future__ import annotations

import csv
import gzip
import hashlib
import itertools
import json
import math
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(r"D:\MCOD_Research\USBR_MCOD_upgrade_pilot_20261021")
EXT = Path(r"D:\MCOD_Research\USBR_MCOD_extension_20261021")
FROZEN = Path(r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build")
PYTHON_RUNTIME = Path(
    r"C:\Users\aaa12\.cache\python-runtimes\python-primary-runtime\dependencies\python\python.exe"
)
EXPECTED_PARSER = {
    "US": "b8ac39e0a07049160c833aa8cccffac9732d7834216c6d2a7632cf60d67f9c97",
    "BR": "b7c575e20929b4bbaf06452bd27db2bf4e4930516b2adf0d6f99c0211d9fa7bb",
}
EXPECTED_RETAINED_ROWS = {"US": 141449, "BR": 130198}
YEARS = tuple(range(2012, 2025))
SEXES = ("F", "M")
AGES = tuple(["0-4"] + [f"{x}-{x + 4}" for x in range(5, 85, 5)] + ["85+"])
AGES_0_74 = AGES[:15]
PREFIXES = ("K80", "K81", "K82", "K83")
FAMILY_SETS = tuple(
    "|".join(prefix for i, prefix in enumerate(PREFIXES) if mask & (1 << i))
    for mask in range(1, 1 << len(PREFIXES))
)
POSITIONS = ("only_part_i", "only_part_ii", "both_part_i_ii", "neither_captured")
UCD_GROUPS = ("malignant_C00_C97", "other_neoplasm_D00_D48", "nonneoplasm", "unknown_invalid")
UCD_DETAILS = (
    "C22_1", "C23", "C24", "C25", "other_C22", "other_malignant",
    "other_neoplasm_D00_D48", "nonneoplasm", "unknown_invalid",
)
ANY_MALIGNANT = ("no", "yes")
CODE_GROUPS = ("K80", "K81", "K82", "K83", "K80-81", "K83.0", "K83.1", "K82.9")
M2_COUNT_GROUPS = CODE_GROUPS + ("K82-83_context",)
M2_DEFINITIONS = ("full", "exclude_malignant_UCD", "no_malignancy_source_any")
ENDPOINTS = ("UCD", "any_mention")
M3_GROUPS = ("K80-83_total", "K80-81", "K82-83_context")
PERIODS = (("2012-2024", YEARS), ("2012-2023", tuple(range(2012, 2024))))


class PilotError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def parse_json_list(row: dict[str, str], field: str) -> list[Any]:
    value = row.get(field, "")
    if not value:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise PilotError(f"Expected JSON list in {field}")
    return decoded


def as_flag(value: Any, field: str) -> bool:
    if str(value).strip() in {"1", "true", "True"}:
        return True
    if str(value).strip() in {"0", "false", "False"}:
        return False
    raise PilotError(f"Invalid flag {field}={value!r}")


def ucd_group(raw_code: Any) -> str:
    code = norm_code(raw_code)
    match = re.match(r"^([A-Z])(\d{2})", code)
    if not match:
        return "unknown_invalid"
    letter, number_text = match.groups()
    number = int(number_text)
    if letter == "C" and number <= 97:
        return "malignant_C00_C97"
    if letter == "D" and number <= 48:
        return "other_neoplasm_D00_D48"
    valid_ranges = {
        "A": (0, 99), "B": (0, 99), "D": (50, 89), "E": (0, 90),
        "F": (0, 99), "G": (0, 99), "H": (0, 95), "I": (0, 99),
        "J": (0, 99), "K": (0, 93), "L": (0, 99), "M": (0, 99),
        "N": (0, 99), "O": (0, 99), "P": (0, 96), "Q": (0, 99),
        "R": (0, 99), "S": (0, 99), "T": (0, 98), "U": (0, 99),
        "V": (0, 98), "W": (0, 99), "X": (0, 99), "Y": (0, 98),
        "Z": (0, 99),
    }
    limits = valid_ranges.get(letter)
    if limits and limits[0] <= number <= limits[1] and len(code) >= 3:
        return "nonneoplasm"
    return "unknown_invalid"


def ucd_detail(raw_code: Any) -> str:
    code = norm_code(raw_code)
    if ucd_group(code) == "other_neoplasm_D00_D48":
        return "other_neoplasm_D00_D48"
    if ucd_group(code) == "nonneoplasm":
        return "nonneoplasm"
    if ucd_group(code) == "unknown_invalid":
        return "unknown_invalid"
    if code.startswith("C221"):
        return "C22_1"
    if code.startswith("C23"):
        return "C23"
    if code.startswith("C24"):
        return "C24"
    if code.startswith("C25"):
        return "C25"
    if code.startswith("C22"):
        return "other_C22"
    return "other_malignant"


def is_malignant(raw_code: Any) -> bool:
    return ucd_group(raw_code) == "malignant_C00_C97"


def include_m2_case_definition(definition: str, ucd_code: Any, source_any: Iterable[str]) -> bool:
    """Apply one canonical record-level rule for each pre-fixed M2 definition."""
    malignant_ucd = is_malignant(ucd_code)
    malignant_source_any = any(is_malignant(code) for code in source_any)
    if definition == "full":
        return True
    if definition == "exclude_malignant_UCD":
        return not malignant_ucd
    if definition == "no_malignancy_source_any":
        return not malignant_source_any
    raise PilotError(f"Unknown M2 case definition: {definition}")


def source_any_codes(row: dict[str, str], country: str) -> set[str]:
    codes = {norm_code(row.get("ucd_code", ""))}
    if country == "US":
        codes.update(norm_code(x) for x in parse_json_list(row, "entity_codes"))
    else:
        for item in parse_json_list(row, "line_codes"):
            if not isinstance(item, dict) or "code" not in item:
                raise PilotError("BR line_codes must contain {field,code} pairs")
            codes.add(norm_code(item["code"]))
    return {code for code in codes if code}


def us_record_union_codes(row: dict[str, str]) -> set[str]:
    codes = {norm_code(row.get("ucd_code", ""))}
    codes.update(norm_code(x) for x in parse_json_list(row, "entity_codes"))
    codes.update(norm_code(x) for x in parse_json_list(row, "record_codes"))
    return {code for code in codes if code}


def primary_biliary_codes(row: dict[str, str], country: str) -> set[str]:
    if country == "US":
        values = parse_json_list(row, "entity_biliary_codes")
        codes = {norm_code(value) for value in values}
    else:
        values = parse_json_list(row, "line_biliary_codes")
        if any(not isinstance(item, dict) or "code" not in item for item in values):
            raise PilotError("BR line_biliary_codes must contain {field,code} pairs")
        codes = {norm_code(item["code"]) for item in values}
    return {code for code in codes if code[:3] in PREFIXES}


def source_position(row: dict[str, str], country: str) -> str:
    if country == "US":
        part_i = bool(parse_json_list(row, "part_i_biliary"))
        part_ii = bool(parse_json_list(row, "part_ii_biliary"))
    else:
        part_i = as_flag(row.get("part_i_biliary"), "part_i_biliary")
        part_ii = as_flag(row.get("part_ii_biliary"), "part_ii_biliary")
    if part_i and part_ii:
        return "both_part_i_ii"
    if part_i:
        return "only_part_i"
    if part_ii:
        return "only_part_ii"
    return "neither_captured"


def family_set(codes: set[str]) -> str:
    present = {code[:3] for code in codes if code[:3] in PREFIXES}
    if not present:
        raise PilotError("EXTRA record has no primary biliary code family")
    return "|".join(prefix for prefix in PREFIXES if prefix in present)


def matches_group(code: str, group: str) -> bool:
    code = norm_code(code)
    if group in PREFIXES:
        return code.startswith(group)
    if group == "K80-81":
        return code.startswith("K80") or code.startswith("K81")
    if group == "K83.0":
        return code.startswith("K830")
    if group == "K83.1":
        return code.startswith("K831")
    if group == "K82.9":
        return code.startswith("K829")
    if group == "K82-83_context":
        return code.startswith("K82") or code.startswith("K83")
    if group == "K80-83_total":
        return code[:3] in PREFIXES
    raise PilotError(f"Unknown code group {group}")


def get_input_paths() -> tuple[dict[tuple[str, int], Path], dict[tuple[str, int], Path], dict[str, Any]]:
    manifest = read_json(EXT / "release_manifest.json")
    listed = {item["path"].replace("\\", "/"): item for item in manifest["files"]}
    candidates: dict[tuple[str, int], Path] = {}
    summaries: dict[tuple[str, int], Path] = {}
    for country in ("BR", "US"):
        for year in YEARS:
            if country == "BR":
                candidate_rel = f"outputs/br/candidate_records_BR_{year}.csv.gz"
                summary_rel = f"outputs/br/summary_BR_{year}.json"
            else:
                candidate_rel = (
                    f"outputs/us/us_{year}_*_{EXPECTED_PARSER['US'][:12]}_retained.csv.gz"
                )
                matches = list((EXT / "outputs" / "us").glob(Path(candidate_rel).name))
                if len(matches) != 1:
                    raise PilotError(f"Expected exactly one current-parser US file for {year}; found {len(matches)}")
                candidate_path = matches[0]
                candidate_rel = candidate_path.relative_to(EXT).as_posix()
                summary_rel = candidate_rel.removesuffix("_retained.csv.gz") + "_summary.json"
            if country == "BR":
                candidate_path = EXT / Path(candidate_rel)
            summary_path = EXT / Path(summary_rel)
            if candidate_rel not in listed or summary_rel not in listed:
                raise PilotError(f"Input absent from release manifest: {candidate_rel} or {summary_rel}")
            candidates[(country, year)] = candidate_path
            summaries[(country, year)] = summary_path
    return candidates, summaries, {"release_manifest": manifest, "manifest_files": listed}


def check_input_identity(
    candidates: dict[tuple[str, int], Path],
    summaries: dict[tuple[str, int], Path],
    manifest_info: dict[str, Any],
) -> tuple[dict[tuple[str, int], int], dict[str, Any]]:
    manifest_files = manifest_info["manifest_files"]
    row_expectations: dict[tuple[str, int], int] = {}
    hash_entries: list[dict[str, Any]] = []
    parser_hashes = {}
    for country in ("US", "BR"):
        parser_path = EXT / "scripts" / f"extract_{country.lower()}.py"
        observed = sha256_file(parser_path)
        if observed != EXPECTED_PARSER[country]:
            raise PilotError(f"Current parser hash mismatch for {country}: {observed}")
        parser_hashes[country] = observed
        hash_entries.append({"role": f"parser_{country}", "path": str(parser_path), "sha256": observed})

    for key, candidate_path in sorted(candidates.items()):
        country, year = key
        summary_path = summaries[key]
        candidate_rel = candidate_path.relative_to(EXT).as_posix()
        summary_rel = summary_path.relative_to(EXT).as_posix()
        for role, path, rel in (("retained_candidate", candidate_path, candidate_rel), ("retained_summary", summary_path, summary_rel)):
            observed = sha256_file(path)
            expected = manifest_files[rel]["sha256"]
            if observed != expected:
                raise PilotError(f"Release-manifest SHA mismatch for {rel}")
            hash_entries.append({"role": role, "path": str(path), "sha256": observed, "bytes": path.stat().st_size})
        summary = read_json(summary_path)
        if summary.get("parser_sha256") != EXPECTED_PARSER[country]:
            raise PilotError(f"Summary is not bound to required current parser: {summary_path}")
        if country == "US":
            row_expectations[key] = int(summary["retained_biliary_records"])
        else:
            row_expectations[key] = int(summary["source_audit_by_year"][str(year)]["candidate_records"])

    identity_rows = csv_rows(EXT / "outputs" / "identity" / "source_identity.csv")
    if len(identity_rows) != 26 or any(row.get("status") != "MATCH" for row in identity_rows):
        raise PilotError("Source identity ledger must have 26 MATCH rows")
    bindings = read_json(EXT / "outputs" / "review" / "full_window_artifact_bindings.json")
    if bindings.get("status") != "PASS" or bindings.get("current_parser_summaries") != 26:
        raise PilotError("Current-parser full-window artifact binding audit did not PASS")
    if bindings.get("source_identity_MATCH_bindings") != 26 or bindings.get("raw_rescan") is not False:
        raise PilotError("Expected 26 source-identity bindings and no raw rescan")
    audit = read_json(EXT / "outputs" / "review" / "final_independent_review.json")
    if audit.get("status") != "PASS" or audit.get("retained_candidates_independently_recounted") != 271647:
        raise PilotError("Independent retained-record audit did not PASS")
    if audit.get("duplicate_keys") != 0 or audit.get("flag_evidence_discrepancies") != 0:
        raise PilotError("Independent retained-record duplicate/flag audit failed")
    for rel in (
        "outputs/identity/source_identity.csv",
        "outputs/review/full_window_artifact_bindings.json",
        "outputs/review/final_independent_review.json",
        "outputs/audit/full_window_audit.json",
        "outputs/statistics/input_sha256.csv",
    ):
        path = EXT / Path(rel)
        hash_entries.append({"role": "source_audit", "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return row_expectations, {"hashes": hash_entries, "parser_sha256": parser_hashes, "bindings": bindings, "independent_audit": audit}


def load_frozen_tables() -> tuple[dict[tuple[str, int, str, str], dict[str, int]], dict[str, float], dict[str, Any]]:
    population_path = FROZEN / "data" / "derived" / "analysis_input_936.csv"
    weights_path = FROZEN / "data" / "metadata" / "who_2000_2025_18_group_weights_correct.csv"
    population_rows = csv_rows(population_path)
    weight_rows = csv_rows(weights_path)
    if len(population_rows) != 936:
        raise PilotError(f"Frozen analysis input should be 936 cells, got {len(population_rows)}")
    population: dict[tuple[str, int, str, str], dict[str, int]] = {}
    for row in population_rows:
        key = (row["country"], int(row["year"]), row["age_group"], row["sex"])
        if key in population:
            raise PilotError(f"Duplicate frozen population cell {key}")
        population[key] = {
            "population": int(row["population"]),
            "ucd": int(row["ucd"]),
            "any_common": int(row["any_common"]),
        }
    expected_keys = {
        (country, year, age, sex)
        for country in ("US", "BR") for year in YEARS for age in AGES for sex in SEXES
    }
    if set(population) != expected_keys:
        raise PilotError("Frozen denominator grid is not the full 936 country-year-age-sex grid")
    weights = {row["age_group"]: float(row["weight"]) for row in weight_rows}
    if len(weights) != 18 or set(weights) != set(AGES):
        raise PilotError("WHO18 weight file does not cover the 18 expected age groups")
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0, abs_tol=1e-12):
        raise PilotError("WHO18 weights must sum to 1")
    weight_0_74_total = sum(weights[age] for age in AGES_0_74)
    weights_0_74 = {age: weights[age] / weight_0_74_total for age in AGES_0_74}
    inputs = {
        "population_path": str(population_path),
        "population_sha256": sha256_file(population_path),
        "weights_path": str(weights_path),
        "weights_sha256": sha256_file(weights_path),
        "population_cells": len(population),
        "who18_weight_sum": sum(weights.values()),
        "who18_0_74_original_weight_sum": weight_0_74_total,
        "who18_0_74_renormalized_weight_sum": sum(weights_0_74.values()),
        "m0_population_gate_path": str(ROOT / "outputs" / "population" / "population_gate.json"),
    }
    gate = ROOT / "outputs" / "population" / "population_gate.json"
    if gate.exists():
        m0 = read_json(gate)
        if m0.get("status") != "PASS" or m0.get("cells") != 936 or m0.get("independent_reconstruction") is not True:
            raise PilotError("Parent M0 population gate exists but did not PASS")
        if m0.get("frozen_input_sha256") != inputs["population_sha256"]:
            raise PilotError("M0 population gate frozen input hash does not match the retained denominator input")
        inputs["m0_population_gate_sha256"] = sha256_file(gate)
        inputs["m0_population_gate_status"] = m0["status"]
    return population, weights, inputs


def read_candidates(
    candidates: dict[tuple[str, int], Path],
    row_expectations: dict[tuple[str, int], int],
    population: dict[tuple[str, int, str, str], dict[str, int]],
) -> tuple[Counter, Counter, Counter, Counter, Counter, Counter, dict[str, Any]]:
    base = Counter()
    m1_joint = Counter()
    m1_cancer = Counter()
    m1_cancer_detail = Counter()
    m2 = Counter()
    ucd_codes = Counter()
    file_checks = []
    total_rows = 0
    for country, year in sorted(candidates):
        path = candidates[(country, year)]
        row_count = 0
        previous_source_row = 0
        seen_keys: set[int] = set()
        expected_fields = (
            {"country", "year", "source_row", "age_group", "sex", "ucd_code", "ucd_biliary", "any_common", "part_i_biliary", "part_ii_biliary", "entity_codes", "record_codes", "entity_biliary_codes", "position_codes"}
            if country == "US"
            else {"country", "year", "source_row", "age_group", "sex", "ucd_code", "ucd_biliary", "any_common", "part_i_biliary", "part_ii_biliary", "line_codes", "line_biliary_codes", "position_codes"}
        )
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = expected_fields - set(reader.fieldnames or [])
            if missing:
                raise PilotError(f"Candidate schema missing fields in {path}: {sorted(missing)}")
            for row in reader:
                row_count += 1
                try:
                    source_row = int(row["source_row"])
                    row_year = int(row["year"])
                except (KeyError, ValueError) as exc:
                    raise PilotError(f"Invalid source_row/year in {path}") from exc
                if source_row <= previous_source_row or source_row in seen_keys:
                    raise PilotError(f"Candidate source_row is not unique/increasing for {country}/{year}")
                previous_source_row = source_row
                seen_keys.add(source_row)
                if row.get("country") != country or row_year != year:
                    raise PilotError(f"Candidate country/year mismatch in {path}")
                age, sex = row.get("age_group", ""), row.get("sex", "")
                cell = (country, year, age, sex)
                if cell not in population:
                    raise PilotError(f"Candidate does not map to a frozen population cell: {cell}")
                any_common = as_flag(row.get("any_common"), "any_common")
                ucd_biliary = as_flag(row.get("ucd_biliary"), "ucd_biliary")
                if not any_common:
                    raise PilotError("Current retained candidate stream unexpectedly contains non-primary rows")
                base[(country, year, age, sex, "candidate")] += 1
                base[(country, year, age, sex, "ucd_biliary")] += int(ucd_biliary)
                base[(country, year, age, sex, "any_common")] += int(any_common)
                base[(country, year, age, sex, "extra")] += int(any_common and not ucd_biliary)

                code_set = source_any_codes(row, country)
                malignant_any = any(is_malignant(code) for code in code_set)
                malignant_record_union = (
                    any(is_malignant(code) for code in us_record_union_codes(row))
                    if country == "US" else malignant_any
                )
                ucg = ucd_group(row.get("ucd_code"))
                detail = ucd_detail(row.get("ucd_code"))
                raw_ucd = str(row.get("ucd_code", "")).strip() or "<BLANK>"
                ucd_codes[(country, year, raw_ucd, norm_code(row.get("ucd_code", "")) or "<BLANK>", ucg)] += 1
                m1_cancer[(country, year, age, sex, ucg, "yes" if malignant_any else "no")] += 1
                m1_cancer_detail[(country, year, age, sex, detail, "yes" if malignant_any else "no")] += 1
                base[(country, year, age, sex, "malignant_any_source")] += int(malignant_any)
                base[(country, year, age, sex, "malignant_any_record_union")] += int(malignant_record_union)

                if any_common and not ucd_biliary:
                    codes = primary_biliary_codes(row, country)
                    family = family_set(codes)
                    position = source_position(row, country)
                    m1_joint[(country, year, age, sex, family, position, ucg)] += 1

                for definition in M2_DEFINITIONS:
                    if not include_m2_case_definition(definition, row.get("ucd_code"), code_set):
                        continue
                    base[(country, year, age, sex, "m2_definition", definition)] += 1
                    for group in M2_COUNT_GROUPS + ("K80-83_total",):
                        ucd_hit = matches_group(norm_code(row.get("ucd_code", "")), group)
                        any_hit = any(matches_group(code, group) for code in code_set)
                        if ucd_hit:
                            m2[(country, year, age, sex, definition, group, "UCD")] += 1
                        if any_hit:
                            m2[(country, year, age, sex, definition, group, "any_mention")] += 1
        if row_count != row_expectations[(country, year)]:
            raise PilotError(f"Candidate rows mismatch summary for {country}/{year}: {row_count} vs {row_expectations[(country, year)]}")
        total_rows += row_count
        file_checks.append({"country": country, "year": year, "candidate_rows": row_count, "unique_source_rows": len(seen_keys), "source_row_order": "strictly_increasing"})
    return base, m1_joint, m1_cancer, m1_cancer_detail, m2, ucd_codes, {"candidate_rows": total_rows, "file_checks": file_checks}


def check_936_match(
    base: Counter, population: dict[tuple[str, int, str, str], dict[str, int]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = []
    errors = []
    for key, frozen in sorted(population.items()):
        country, year, age, sex = key
        got_ucd = base[(country, year, age, sex, "ucd_biliary")]
        got_any = base[(country, year, age, sex, "any_common")]
        got_population = frozen["population"]
        for measure, got, wanted in (
            ("population", got_population, frozen["population"]),
            ("ucd", got_ucd, frozen["ucd"]),
            ("any_common", got_any, frozen["any_common"]),
        ):
            rows.append({"country": country, "year": year, "age_group": age, "sex": sex, "measure": measure, "rebuilt": got, "frozen": wanted, "difference": got - wanted})
            if got != wanted:
                errors.append({"cell": list(key), "measure": measure, "rebuilt": got, "frozen": wanted})
    if errors:
        raise PilotError(f"Frozen 936-cell match failed ({len(errors)} count mismatches)")
    return rows, errors


def standard_rate(
    counts: Counter,
    country: str,
    category: tuple[Any, ...],
    population: dict[tuple[str, int, str, str], dict[str, int]],
    weights: dict[str, float],
    *,
    age_scope: str = "all_age",
    years: tuple[int, ...] = YEARS,
    key_builder,
) -> float:
    if age_scope == "all_age":
        ages, age_weights = AGES, weights
    elif age_scope == "0_74":
        ages = AGES_0_74
        sub_total = sum(weights[age] for age in ages)
        age_weights = {age: weights[age] / sub_total for age in ages}
    else:
        raise PilotError(f"Unknown age_scope={age_scope}")
    result = 0.0
    year_weight = 1.0 / len(years)
    for year in years:
        for sex in SEXES:
            for age in ages:
                cell = (country, year, age, sex)
                pop = population[cell]["population"]
                key = key_builder(country, year, age, sex, category)
                count = counts[key]
                result += year_weight * 0.5 * age_weights[age] * (count / pop) * 100_000.0
    return result


def raw_total(counts: Counter, country: str, category: tuple[Any, ...], years: tuple[int, ...], key_builder, ages: tuple[str, ...] = AGES) -> int:
    return sum(
        counts[key_builder(country, year, age, sex, category)]
        for year in years for age in ages for sex in SEXES
    )


def rate_for_base(base: Counter, country: str, measure: str, population, weights, years=YEARS, age_scope="all_age") -> float:
    return standard_rate(
        base, country, (measure,), population, weights, years=years, age_scope=age_scope,
        key_builder=lambda c, y, a, s, cat: (c, y, a, s, cat[0]),
    )


def empty_grid_counts(
    path: Path,
    fields: list[str],
    cells,
    categories,
    counter: Counter,
    key_builder,
    category_names: list[str],
) -> None:
    def generate():
        for country, year, age, sex in cells:
            for category in categories:
                key = key_builder(country, year, age, sex, category)
                result = {"country": country, "year": year, "age_group": age, "sex": sex}
                result.update(dict(zip(category_names, category)))
                result["deaths"] = counter[key]
                yield result
    write_csv(path, fields, generate())


def verify_m1_closure(base: Counter, m1_joint: Counter, cells) -> dict[str, Any]:
    cell_checks = 0
    closure_failures = []
    for country, year, age, sex in cells:
        observed = sum(
            m1_joint[(country, year, age, sex, family, position, ucd)]
            for family in FAMILY_SETS for position in POSITIONS for ucd in UCD_GROUPS
        )
        expected = base[(country, year, age, sex, "extra")]
        if observed != expected:
            closure_failures.append({"country": country, "year": year, "age_group": age, "sex": sex, "expected_extra": expected, "joint_partition_sum": observed})
        for dim_categories, index in ((FAMILY_SETS, 4), (POSITIONS, 5), (UCD_GROUPS, 6)):
            marginal = Counter()
            for family in FAMILY_SETS:
                for position in POSITIONS:
                    for ucd in UCD_GROUPS:
                        key = (country, year, age, sex, family, position, ucd)
                        marginal[key[index]] += m1_joint[key]
            if any(marginal[value] < 0 for value in dim_categories) or sum(marginal.values()) != expected:
                closure_failures.append({"country": country, "year": year, "age_group": age, "sex": sex, "dimension_index": index, "expected_extra": expected, "marginal_sum": sum(marginal.values())})
        cell_checks += 1
    if closure_failures:
        raise PilotError(f"M1 joint partition closure failed ({len(closure_failures)} cells/margins)")
    return {"status": "PASS", "cells_checked": cell_checks, "categories": {"family_sets": len(FAMILY_SETS), "positions": len(POSITIONS), "ucd_groups": len(UCD_GROUPS)}, "zero_filled_joint_combinations": cell_checks * len(FAMILY_SETS) * len(POSITIONS) * len(UCD_GROUPS)}


def verify_m2_dedup(m2: Counter, base: Counter, cells) -> dict[str, Any]:
    checks = 0
    definition_totals = Counter()
    group_totals = Counter()
    groups = M2_COUNT_GROUPS + ("K80-83_total",)
    for country, year, age, sex in cells:
        candidate_n = base[(country, year, age, sex, "candidate")]
        per_cell_definition_counts = {
            definition: base[(country, year, age, sex, "m2_definition", definition)]
            for definition in M2_DEFINITIONS
        }
        if not (
            per_cell_definition_counts["no_malignancy_source_any"]
            <= per_cell_definition_counts["exclude_malignant_UCD"]
            <= per_cell_definition_counts["full"]
            <= candidate_n
        ):
            raise PilotError("M2 record-definition counts are not nested within the retained candidate set")
        definition_totals.update(per_cell_definition_counts)
        for definition in M2_DEFINITIONS:
            for group in groups:
                for endpoint in ENDPOINTS:
                    count = m2[(country, year, age, sex, definition, group, endpoint)]
                    if count < 0 or count > candidate_n:
                        raise PilotError(f"M2 death/group dedup bound failed for {(country, year, age, sex, definition, group, endpoint)}")
                    checks += 1
                    group_totals[(definition, group, endpoint)] += count
            for group in groups:
                if m2[(country, year, age, sex, definition, group, "UCD")] > m2[(country, year, age, sex, definition, group, "any_mention")]:
                    raise PilotError("UCD death count exceeds the source-ANY per-death union count")
            def any_count(group_name):
                return m2[(country, year, age, sex, definition, group_name, "any_mention")]
            if not max(any_count("K80"), any_count("K81")) <= any_count("K80-81") <= any_count("K80") + any_count("K81"):
                raise PilotError("K80-81 union count is inconsistent with component counts")
            if not max(any_count("K82"), any_count("K83")) <= any_count("K82-83_context") <= any_count("K82") + any_count("K83"):
                raise PilotError("K82-83 context union count is inconsistent with component counts")
            if not max(any_count(group) for group in PREFIXES) <= any_count("K80-83_total") <= sum(any_count(group) for group in PREFIXES):
                raise PilotError("K80-83 total union count is inconsistent with component counts")
            if any_count("K83.0") > any_count("K83") or any_count("K83.1") > any_count("K83"):
                raise PilotError("K83.0/K83.1 count exceeds K83")
            if any_count("K82.9") > any_count("K82"):
                raise PilotError("K82.9 count exceeds K82")

    if not (
        definition_totals["no_malignancy_source_any"]
        < definition_totals["exclude_malignant_UCD"]
        < definition_totals["full"]
    ):
        raise PilotError("M2 filters did not show the expected strict record-level effect in the retained data")

    strict_group_effects = []
    group_subset_audit = []
    for group in groups:
        for endpoint in ENDPOINTS:
            nested = {
                definition: group_totals[(definition, group, endpoint)]
                for definition in M2_DEFINITIONS
            }
            if not (
                nested["no_malignancy_source_any"]
                <= nested["exclude_malignant_UCD"]
                <= nested["full"]
            ):
                raise PilotError(f"M2 subgroup counts are not nested for {group}/{endpoint}")
            audit_row = {"code_group": group, "endpoint": endpoint, **nested}
            audit_row["excluded_by_malignant_UCD"] = nested["full"] - nested["exclude_malignant_UCD"]
            audit_row["excluded_additionally_by_source_any"] = nested["exclude_malignant_UCD"] - nested["no_malignancy_source_any"]
            group_subset_audit.append(audit_row)
            if nested["no_malignancy_source_any"] < nested["exclude_malignant_UCD"] < nested["full"]:
                strict_group_effects.append(audit_row)
    if not any(row["endpoint"] == "any_mention" for row in strict_group_effects):
        raise PilotError("No M2 any-mention subgroup demonstrated a strict source-ANY filter effect")
    total_union_matches = [row for row in strict_group_effects if row["code_group"] == "K80-83_total" and row["endpoint"] == "any_mention"]
    if not total_union_matches:
        raise PilotError("K80-83 any-mention did not show a strict filter effect in the retained data")
    total_union = total_union_matches[0]
    return {
        "status": "PASS",
        "cell_endpoint_counts_checked": checks,
        "record_definition_totals": dict(definition_totals),
        "strict_K80_83_any_mention_subset_counts": total_union,
        "group_subset_audit": group_subset_audit,
        "strict_group_effects": strict_group_effects,
        "unique_death_keys": "strict source_row monotonicity/uniqueness per country-year; source code lists converted to per-death sets before group indicators",
    }


def rate_rows_for_m2(m2, base, population, weights, period_years=YEARS, age_scope="all_age"):
    """Return fixed-window and annual point estimates for the M2 code groups."""
    del base, period_years  # retained in the call signature for compatibility with the draft.
    rows = []
    periods = list(PERIODS) + [(str(year), (year,)) for year in YEARS]
    age_labels = AGES if age_scope == "all_age" else AGES_0_74
    key_builder = lambda c, y, a, s, cat: (c, y, a, s, cat[0], cat[1], cat[2])
    for period, years in periods:
        for country in ("BR", "US"):
            for definition in M2_DEFINITIONS:
                for group in CODE_GROUPS:
                    for endpoint in ENDPOINTS:
                        category = (definition, group, endpoint)
                        rows.append({
                            "period": period, "country": country, "case_definition": definition,
                            "code_group": group, "endpoint": endpoint,
                            "deaths": raw_total(m2, country, category, years, key_builder, age_labels),
                            "standardized_rate_per_100k": standard_rate(
                                m2, country, category, population, weights, years=years,
                                age_scope=age_scope, key_builder=key_builder,
                            ),
                            "age_scope": age_scope, "sex_weights": "0.5_each", "year_weights": "equal_within_period",
                        })
    return rows


def contrasts_from_rates(rate_rows, *, period_filter=None):
    """Compute BR-US direct-standardized contrasts for the two fixed windows."""
    periods = (period_filter,) if period_filter else tuple(period for period, _ in PERIODS)
    index = {
        (row["period"], row["case_definition"], row["code_group"], row["endpoint"], row["age_scope"], row["country"]): row
        for row in rate_rows
    }
    output = []
    for period in periods:
        for definition in M2_DEFINITIONS:
            for group in CODE_GROUPS:
                pair = {}
                for endpoint in ENDPOINTS:
                    br = index[(period, definition, group, endpoint, "all_age", "BR")]
                    us = index[(period, definition, group, endpoint, "all_age", "US")]
                    b, u = float(br["standardized_rate_per_100k"]), float(us["standardized_rate_per_100k"])
                    pair[endpoint] = (b, u)
                    output.append({
                        "period": period, "case_definition": definition, "code_group": group,
                        "endpoint": endpoint, "BR_rate_per_100k": b, "US_rate_per_100k": u,
                        "BR_minus_US_rate_difference_per_100k": b - u,
                        "BR_over_US_rate_ratio": b / u if u > 0 else "",
                        "interpretation": "descriptive_point_estimate_only",
                    })
                br_any, us_any = pair["any_mention"]
                br_ucd, us_ucd = pair["UCD"]
                rd_any, rd_ucd = br_any - us_any, br_ucd - us_ucd
                output.append({
                    "period": period, "case_definition": definition, "code_group": group,
                    "endpoint": "DD_any_minus_UCD", "BR_rate_per_100k": "", "US_rate_per_100k": "",
                    "BR_minus_US_rate_difference_per_100k": rd_any - rd_ucd,
                    "BR_over_US_rate_ratio": (br_any / us_any) / (br_ucd / us_ucd) if us_any > 0 and us_ucd > 0 and br_ucd > 0 else "",
                    "interpretation": "direct_standardized_rate_difference_in_differences; not model_based_DD",
                })
    return output


def m1_outputs(base, m1_joint, m1_cancer, m1_cancer_detail, ucd_codes, population, weights, output_dir: Path, cells):
    joint_fields = ["country", "year", "age_group", "sex", "family_set", "position_group", "ucd_group", "deaths"]
    empty_grid_counts(
        output_dir / "M1_extra_joint_counts_by_stratum.csv", joint_fields, sorted(cells),
        list(itertools.product(FAMILY_SETS, POSITIONS, UCD_GROUPS)), m1_joint,
        lambda c, y, a, s, cat: (c, y, a, s, *cat), ["family_set", "position_group", "ucd_group"],
    )
    joint_builder = lambda c, y, a, s, cat: (c, y, a, s, *cat)
    standardized = []
    contribution_rows = []
    for period, years in PERIODS:
        for family, position, ucd in itertools.product(FAMILY_SETS, POSITIONS, UCD_GROUPS):
            category = (family, position, ucd)
            rates = {}
            counts = {}
            for country in ("BR", "US"):
                rates[country] = standard_rate(m1_joint, country, category, population, weights, years=years, key_builder=joint_builder)
                counts[country] = raw_total(m1_joint, country, category, years, joint_builder)
                standardized.append({
                    "period": period, "country": country, "family_set": family,
                    "position_group": position, "ucd_group": ucd, "deaths": counts[country],
                    "std_rate_per_100k": rates[country], "age_weights": "WHO18",
                    "sex_weights": "0.5_each", "year_weights": "equal_within_period",
                })
            contribution_rows.append({
                "period": period, "family_set": family, "position_group": position, "ucd_group": ucd,
                "BR_deaths": counts["BR"], "US_deaths": counts["US"],
                "BR_std_rate_per_100k": rates["BR"], "US_std_rate_per_100k": rates["US"],
                "DD_contribution_BR_minus_US_per_100k": rates["BR"] - rates["US"],
            })
    write_csv(output_dir / "M1_extra_joint_standardized_rates.csv", ["period", "country", "family_set", "position_group", "ucd_group", "deaths", "std_rate_per_100k", "age_weights", "sex_weights", "year_weights"], standardized)
    write_csv(output_dir / "M1_extra_joint_DD_contributions.csv", ["period", "family_set", "position_group", "ucd_group", "BR_deaths", "US_deaths", "BR_std_rate_per_100k", "US_std_rate_per_100k", "DD_contribution_BR_minus_US_per_100k"], contribution_rows)

    cancer_fields = ["country", "year", "age_group", "sex", "ucd_group", "any_malignancy_mention", "deaths"]
    cancer_categories = list(itertools.product(UCD_GROUPS, ANY_MALIGNANT))
    empty_grid_counts(output_dir / "M1_malignancy_mention_counts_by_stratum.csv", cancer_fields, sorted(cells), cancer_categories, m1_cancer,
                      lambda c, y, a, s, cat: (c, y, a, s, *cat), ["ucd_group", "any_malignancy_mention"])
    detail_fields = ["country", "year", "age_group", "sex", "ucd_subgroup", "any_malignancy_mention", "deaths"]
    empty_grid_counts(output_dir / "M1_cancer_UCD_subgroups_by_stratum.csv", detail_fields, sorted(cells), list(itertools.product(UCD_DETAILS, ANY_MALIGNANT)), m1_cancer_detail,
                      lambda c, y, a, s, cat: (c, y, a, s, *cat), ["ucd_subgroup", "any_malignancy_mention"])

    cancer_rates = []
    detail_builder = lambda c, y, a, s, cat: (c, y, a, s, *cat)
    for period, years in PERIODS:
        for subgroup, any_mal in itertools.product(UCD_DETAILS, ANY_MALIGNANT):
            for country in ("BR", "US"):
                cancer_rates.append({
                    "period": period, "country": country, "ucd_subgroup": subgroup,
                    "any_malignancy_mention": any_mal,
                    "deaths": raw_total(m1_cancer_detail, country, (subgroup, any_mal), years, detail_builder),
                    "std_rate_per_100k": standard_rate(m1_cancer_detail, country, (subgroup, any_mal), population, weights, years=years, key_builder=detail_builder),
                })
    write_csv(output_dir / "M1_cancer_UCD_subgroups_standardized.csv", ["period", "country", "ucd_subgroup", "any_malignancy_mention", "deaths", "std_rate_per_100k"], cancer_rates)

    code_audit = [
        {"country": country, "year": year, "ucd_code_raw": raw_code, "ucd_code_normalized": normalized, "ucd_class": group, "deaths": n}
        for (country, year, raw_code, normalized, group), n in sorted(ucd_codes.items())
    ]
    write_csv(output_dir / "M1_ucd_code_classification_audit.csv", ["country", "year", "ucd_code_raw", "ucd_code_normalized", "ucd_class", "deaths"], code_audit)

    sensitivity = []
    for period, years in PERIODS:
        primary_n = sum(base[("US", year, age, sex, "malignant_any_source")] for year in years for age in AGES for sex in SEXES)
        union_n = sum(base[("US", year, age, sex, "malignant_any_record_union")] for year in years for age in AGES for sex in SEXES)
        primary_rate = rate_for_base(base, "US", "malignant_any_source", population, weights, years=years)
        union_rate = rate_for_base(base, "US", "malignant_any_record_union", population, weights, years=years)
        sensitivity.extend([
            {"period": period, "scope": "primary_source_any_UCD_plus_entity", "country": "US", "deaths": primary_n,
             "std_rate_per_100k": primary_rate, "increment_vs_primary_deaths": 0,
             "increment_vs_primary_std_rate_per_100k": 0.0,
             "comparability_note": "Primary M1/M2 definition; aligns US UCD+entity with BR UCD+cause-lines."},
            {"period": period, "scope": "US_UCD_plus_entity_plus_record_axis", "country": "US", "deaths": union_n,
             "std_rate_per_100k": union_rate, "increment_vs_primary_deaths": union_n - primary_n,
             "increment_vs_primary_std_rate_per_100k": union_rate - primary_rate,
             "comparability_note": "Structural sensitivity only; do not compare across countries or call equivalent to BR cause-line capture."},
        ])
    write_csv(output_dir / "M1_US_record_axis_structural_sensitivity.csv", ["period", "scope", "country", "deaths", "std_rate_per_100k", "increment_vs_primary_deaths", "increment_vs_primary_std_rate_per_100k", "comparability_note"], sensitivity)

    focus = []
    focus_definitions = (
        ("K83_in_family_set__any_PartI", lambda value: "K83" in value.split("|"), lambda value: value in {"only_part_i", "both_part_i_ii"}),
        ("K83_in_family_set__only_PartI", lambda value: "K83" in value.split("|"), lambda value: value == "only_part_i"),
        ("K83_only_family_set__only_PartI", lambda value: value == "K83", lambda value: value == "only_part_i"),
    )
    for period, years in PERIODS:
        for label, family_pred, position_pred in focus_definitions:
            result = {"period": period, "focus_definition": label, "ucd_group": "malignant_C00_C97"}
            for country in ("BR", "US"):
                count = 0
                rate = 0.0
                for family in FAMILY_SETS:
                    for position in POSITIONS:
                        if not family_pred(family) or not position_pred(position):
                            continue
                        category = (family, position, "malignant_C00_C97")
                        count += raw_total(m1_joint, country, category, years, joint_builder)
                        rate += standard_rate(m1_joint, country, category, population, weights, years=years, key_builder=joint_builder)
                result[f"{country}_deaths"] = count
                result[f"{country}_std_rate_per_100k"] = rate
            result["DD_contribution_BR_minus_US_per_100k"] = result["BR_std_rate_per_100k"] - result["US_std_rate_per_100k"]
            focus.append(result)
    write_csv(output_dir / "M1_focus_K83_PartI_malignant_DD.csv", ["period", "focus_definition", "ucd_group", "BR_deaths", "US_deaths", "BR_std_rate_per_100k", "US_std_rate_per_100k", "DD_contribution_BR_minus_US_per_100k"], focus)

    closure = {}
    for period, years in PERIODS:
        period_rows = [row for row in contribution_rows if row["period"] == period]
        dd_sum = math.fsum(row["DD_contribution_BR_minus_US_per_100k"] for row in period_rows)
        direct_extra_br = rate_for_base(base, "BR", "extra", population, weights, years=years)
        direct_extra_us = rate_for_base(base, "US", "extra", population, weights, years=years)
        rd_any = rate_for_base(base, "BR", "any_common", population, weights, years=years) - rate_for_base(base, "US", "any_common", population, weights, years=years)
        rd_ucd = rate_for_base(base, "BR", "ucd_biliary", population, weights, years=years) - rate_for_base(base, "US", "ucd_biliary", population, weights, years=years)
        if not math.isclose(dd_sum, direct_extra_br - direct_extra_us, rel_tol=0, abs_tol=1e-10):
            raise PilotError(f"M1 category DD contributions do not sum to direct EXTRA BR-US contrast for {period}")
        if not math.isclose(dd_sum, rd_any - rd_ucd, rel_tol=0, abs_tol=1e-10):
            raise PilotError(f"M1 category DD contributions do not equal direct RD_any - RD_UCD identity for {period}")
        closure[period] = {
            "dd_contribution_sum": dd_sum, "extra_rate_br": direct_extra_br,
            "extra_rate_us": direct_extra_us, "rd_any_minus_rd_ucd": rd_any - rd_ucd,
            "negative_joint_category_contributions": sum(
                row["DD_contribution_BR_minus_US_per_100k"] < 0 for row in period_rows
            ),
        }
    return {"joint_categories": len(FAMILY_SETS) * len(POSITIONS) * len(UCD_GROUPS), "windows": closure, "focus_rows": focus}


def m2_outputs(m2, population, weights, output_dir: Path, cells):
    fields = ["country", "year", "age_group", "sex", "case_definition", "code_group", "endpoint", "deaths"]
    cats = list(itertools.product(M2_DEFINITIONS, CODE_GROUPS, ENDPOINTS))
    empty_grid_counts(output_dir / "M2_code_group_counts_by_stratum.csv", fields, sorted(cells), cats, m2,
                      lambda c, y, a, s, cat: (c, y, a, s, *cat), ["case_definition", "code_group", "endpoint"])
    rates = rate_rows_for_m2(m2, None, population, weights)
    rate_fields = ["period", "country", "case_definition", "code_group", "endpoint", "deaths", "standardized_rate_per_100k", "age_scope", "sex_weights", "year_weights"]
    write_csv(output_dir / "M2_standardized_rates.csv", rate_fields, rates)
    contrasts = contrasts_from_rates(rates)
    write_csv(output_dir / "M2_direct_contrasts.csv", ["period", "case_definition", "code_group", "endpoint", "BR_rate_per_100k", "US_rate_per_100k", "BR_minus_US_rate_difference_per_100k", "BR_over_US_rate_ratio", "interpretation"], contrasts)
    return rates, contrasts


def m3_outputs(m2, population, weights, output_dir: Path):
    rows = []
    periods = list(PERIODS) + [(str(year), (year,)) for year in YEARS]
    key_builder = lambda c, y, a, s, cat: (c, y, a, s, cat[0], cat[1], cat[2])
    for period, years in periods:
        for age_scope in ("all_age", "0_74"):
            age_labels = AGES if age_scope == "all_age" else AGES_0_74
            for country in ("BR", "US"):
                for definition in M2_DEFINITIONS:
                    for group in M3_GROUPS:
                        for endpoint in ENDPOINTS:
                            category = (definition, group, endpoint)
                            rows.append({
                                "period": period, "age_scope": age_scope, "country": country,
                                "case_definition": definition, "code_group": group, "endpoint": endpoint,
                                "deaths": raw_total(m2, country, category, years, key_builder, age_labels),
                                "standardized_rate_per_100k": standard_rate(m2, country, category, population, weights, years=years, age_scope=age_scope, key_builder=key_builder),
                                "age_weights": "WHO18" if age_scope == "all_age" else "WHO18_0_74_renormalized",
                                "sex_weights": "0.5_each", "year_weights": "equal_within_period",
                                "population_denominator": "full_all_cause_population; never cancer-subsetted",
                            })
    fields = ["period", "age_scope", "country", "case_definition", "code_group", "endpoint", "deaths", "standardized_rate_per_100k", "age_weights", "sex_weights", "year_weights", "population_denominator"]
    write_csv(output_dir / "M3_age_under75_standardized_rates.csv", fields, rows)
    bykey = {(r["period"], r["age_scope"], r["case_definition"], r["code_group"], r["endpoint"], r["country"]): r for r in rows}
    contrasts = []
    for period in ("2012-2024", "2012-2023"):
        for age_scope in ("all_age", "0_74"):
            for definition in M2_DEFINITIONS:
                for group in M3_GROUPS:
                    pairs = {}
                    for endpoint in ENDPOINTS:
                        br = bykey[(period, age_scope, definition, group, endpoint, "BR")]
                        us = bykey[(period, age_scope, definition, group, endpoint, "US")]
                        b, u = float(br["standardized_rate_per_100k"]), float(us["standardized_rate_per_100k"])
                        pairs[endpoint] = (b, u)
                        contrasts.append({
                            "period": period, "age_scope": age_scope, "case_definition": definition,
                            "code_group": group, "endpoint": endpoint, "BR_rate_per_100k": b,
                            "US_rate_per_100k": u, "BR_minus_US_rate_difference_per_100k": b - u,
                            "BR_over_US_rate_ratio": b / u if u > 0 else "",
                        })
                    br_any, us_any = pairs["any_mention"]
                    br_ucd, us_ucd = pairs["UCD"]
                    contrasts.append({
                        "period": period, "age_scope": age_scope, "case_definition": definition,
                        "code_group": group, "endpoint": "DD_any_minus_UCD",
                        "BR_rate_per_100k": "", "US_rate_per_100k": "",
                        "BR_minus_US_rate_difference_per_100k": (br_any - us_any) - (br_ucd - us_ucd),
                        "BR_over_US_rate_ratio": (br_any / us_any) / (br_ucd / us_ucd) if us_any > 0 and us_ucd > 0 and br_ucd > 0 else "",
                    })
    write_csv(output_dir / "M3_age_under75_contrasts.csv", ["period", "age_scope", "case_definition", "code_group", "endpoint", "BR_rate_per_100k", "US_rate_per_100k", "BR_minus_US_rate_difference_per_100k", "BR_over_US_rate_ratio"], contrasts)
    return rows, contrasts


def make_report(read_checks, population_inputs, m1_metrics, m2_dedup, independent_reference, m2_contrasts, m3_contrasts):
    country_totals = Counter()
    for file_check in read_checks["file_checks"]:
        country_totals[file_check["country"]] += file_check["candidate_rows"]

    def show(value):
        return f"{float(value):.6f}"

    m2_index = {
        (row["period"], row["case_definition"], row["code_group"], row["endpoint"]): row
        for row in m2_contrasts
    }
    m3_index = {
        (row["period"], row["age_scope"], row["case_definition"], row["code_group"], row["endpoint"]): row
        for row in m3_contrasts
    }
    lines = [
        "# 保留记录 M1–M3 预分析结果",
        "",
        "状态：COMPUTED。此为已知既有结果后的探索性预分析，不追称预注册。仅分析已保留的当前解析器候选记录；本模块没有重新扫描原始死亡记录。",
        "",
        "## 数据与审计",
        "",
        f"- 当前解析器保留候选共 {read_checks['candidate_rows']:,} 条：US {country_totals['US']:,}，BR {country_totals['BR']:,}。US/BR分别匹配既有摘要的固定记录数。",
        f"- M0 人口门：{population_inputs['m0_population_gate_status']}；冻结人口网格 {population_inputs['population_cells']} 格。原始分母未改动；2012–2024使用冻结的WHO18、男女各0.5及年内等权直接标准化。",
        f"- 固定支持窗另报 2012–2023；巴西2024载荷状态为 UNKNOWN，因此该窗用于不依赖该状态的方向核查。冻结人口 SHA-256：`{population_inputs['population_sha256']}`；WHO18权重 SHA-256：`{population_inputs['weights_sha256']}`。",
        "- 所有率与差异均为描述性点估计；未拟合模型、未计算置信区间。",
        "",
        "## M1：EXTRA 联合互斥分区",
        "",
        f"EXTRA按15个K80/K81/K82/K83家族集合 × 4种Part I/II位置 × 4种UCD组别分成 {m1_metrics['joint_categories']} 个互斥类别。各国年龄×性别×年份的零填充分区闭合、边际闭合均通过。UCD恶性亚组固定为C22.1、C23、C24、C25、其他C22和其他恶性肿瘤；C22未整体视作胆管癌。恶性提及主定义为UCD加US实体轴/BR死因行；US记录轴并集另列为结构敏感性，不作跨国比较。",
        "",
        "| 时窗 | EXTRA BR 标准化率 | EXTRA US 标准化率 | 分区DD贡献和 | RD_any − RD_UCD |",
        "|---|---:|---:|---:|---:|",
    ]
    for period, values in m1_metrics["windows"].items():
        lines.append(f"| {period} | {show(values['extra_rate_br'])} | {show(values['extra_rate_us'])} | {show(values['dd_contribution_sum'])} | {show(values['rd_any_minus_rd_ucd'])} |")
    negative_counts = {period: values["negative_joint_category_contributions"] for period, values in m1_metrics["windows"].items()}
    lines.extend([
        "",
        f"焦点行见 `M1_focus_K83_PartI_malignant_DD.csv`；2012–2024有{negative_counts['2012-2024']}个联合类别贡献为负（2012–2023为{negative_counts['2012-2023']}个）。分区贡献按正负号代数相加得到净差异；焦点行只是其中一个联合子集，不作病因归因或全为正贡献的比例图。",
        "",
        "## M2：固定代码组及记录定义",
        "",
        "代码组按每条死亡记录的代码集合做并集指示；K80–K81、K82–K83和K80–K83均按死亡去重，组间可重叠，不能相加归因。`no_malignancy_source_any` 明确指UCD加US实体轴或BR死因行中均未见C00–C97；`exclude_malignant_UCD` 只排除恶性UCD，并不代表无癌症患者。",
        f"实际筛选核查通过：候选记录 full={m2_dedup['record_definition_totals']['full']:,}，排除恶性UCD后={m2_dedup['record_definition_totals']['exclude_malignant_UCD']:,}，no-malignancy source-ANY={m2_dedup['record_definition_totals']['no_malignancy_source_any']:,}；K80–K83 any-mention依次为{m2_dedup['strict_K80_83_any_mention_subset_counts']['full']:,}、{m2_dedup['strict_K80_83_any_mention_subset_counts']['exclude_malignant_UCD']:,}、{m2_dedup['strict_K80_83_any_mention_subset_counts']['no_malignancy_source_any']:,}，严格嵌套。",
        "",
        "| 时窗 | 组别 | UCD BR−US | any-mention BR−US | DD(any−UCD) | 相对RRR |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for period in ("2012-2024", "2012-2023"):
        for group in ("K80-81", "K83"):
            ucd = m2_index[(period, "no_malignancy_source_any", group, "UCD")]["BR_minus_US_rate_difference_per_100k"]
            any_rate = m2_index[(period, "no_malignancy_source_any", group, "any_mention")]["BR_minus_US_rate_difference_per_100k"]
            dd_row = m2_index[(period, "no_malignancy_source_any", group, "DD_any_minus_UCD")]
            dd = dd_row["BR_minus_US_rate_difference_per_100k"]
            rrr = dd_row["BR_over_US_rate_ratio"]
            lines.append(f"| {period} | {group} | {show(ucd)} | {show(any_rate)} | {show(dd)} | {show(rrr)} |")
    lines.extend([
        "",
        "完整的八个预固定代码组、UCD/any-mention、两个固定时窗及逐年点估计见M2结果表。",
        "",
        "## M3：0–74岁与全龄并列",
        "",
        "0–74岁使用WHO18中0–74岁年龄权重新归一化，性别权重各0.5、年权重等权；分母仍是完整全死因人口。该结果是官方代码/年龄兼容的研究标准化率，不称为精确复现ONS或ESP2013发布率。",
        "",
        "| 时窗 | 年龄范围 | UCD BR−US | any-mention BR−US | DD(any−UCD) | 相对RRR |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for period in ("2012-2024", "2012-2023"):
        for age_scope in ("all_age", "0_74"):
            ucd = m3_index[(period, age_scope, "no_malignancy_source_any", "K80-83_total", "UCD")]["BR_minus_US_rate_difference_per_100k"]
            any_rate = m3_index[(period, age_scope, "no_malignancy_source_any", "K80-83_total", "any_mention")]["BR_minus_US_rate_difference_per_100k"]
            dd_row = m3_index[(period, age_scope, "no_malignancy_source_any", "K80-83_total", "DD_any_minus_UCD")]
            dd = dd_row["BR_minus_US_rate_difference_per_100k"]
            age_label = "全龄" if age_scope == "all_age" else "0–74岁"
            lines.append(f"| {period} | {age_label} | {show(ucd)} | {show(any_rate)} | {show(dd)} | {show(dd_row['BR_over_US_rate_ratio'])} |")
    full = m3_index[("2012-2024", "all_age", "full", "K80-83_total", "DD_any_minus_UCD")]
    no_mal = m3_index[("2012-2024", "all_age", "no_malignancy_source_any", "K80-83_total", "DD_any_minus_UCD")]
    lines.extend([
        "",
        f"2012–2024全龄K80–83的no-malignancy绝对DD为{show(no_mal['BR_minus_US_rate_difference_per_100k'])} per 100k，相较full定义{show(full['BR_minus_US_rate_difference_per_100k'])}较小；对应相对RRR分别为{show(no_mal['BR_over_US_rate_ratio'])}与{show(full['BR_over_US_rate_ratio'])}。这是描述性绝对与相对尺度并列，不表示因果机制。",
        f"独立一遍参考核对：M3 2012–2024 K80–83 `no_malignancy_source_any` 的{independent_reference['country_endpoint_rows_checked']}个国家×端点死亡数及标准化率均匹配（见validation.json）。",
        "",
        "## 交付文件",
        "",
        "修正结果CSV和验证JSON位于 `outputs/retained_m2_filter_fix_20261021/`；固定输入及生成输出SHA-256见该目录下的 `hash_manifest.json`。本报告及数据只支持探索性描述，不把编码共现解释为病因、可避免死亡或医疗失败。",
        "",
    ])
    return "\n".join(lines)


def verify_independent_reference_metrics(m3_rates) -> dict[str, Any]:
    """Guard the corrected M3 endpoint against an independent one-pass reference."""
    expected = {
        ("all_age", "BR", "any_mention"): (95041, 3.0732183601241),
        ("all_age", "BR", "UCD"): (73079, 2.3496898174257),
        ("all_age", "US", "any_mention"): (102802, 1.2271043138039),
        ("all_age", "US", "UCD"): (66197, 0.7642375898631),
        ("0_74", "BR", "any_mention"): (49776, 1.7494194740209),
        ("0_74", "BR", "UCD"): (37014, 1.2939374950240),
        ("0_74", "US", "any_mention"): (37202, 0.6059665951609),
        ("0_74", "US", "UCD"): (21755, 0.3441389546995),
    }
    observed = {
        (row["age_scope"], row["country"], row["endpoint"]): row
        for row in m3_rates
        if row["period"] == "2012-2024"
        and row["case_definition"] == "no_malignancy_source_any"
        and row["code_group"] == "K80-83_total"
    }
    if set(observed) != set(expected):
        raise PilotError("Corrected M3 reference grid is incomplete")
    for key, (expected_deaths, expected_rate) in expected.items():
        row = observed[key]
        if int(row["deaths"]) != expected_deaths:
            raise PilotError(f"Corrected M3 death count differs from independent reference for {key}")
        if not math.isclose(float(row["standardized_rate_per_100k"]), expected_rate, rel_tol=0, abs_tol=1e-9):
            raise PilotError(f"Corrected M3 rate differs from independent reference for {key}")
    return {
        "status": "PASS",
        "reference": "independent one-pass M3 K80-83 source-ANY check",
        "period": "2012-2024",
        "age_scopes": ["all_age", "0_74"],
        "country_endpoint_rows_checked": len(expected),
        "comparison_tolerance_per_100k": 1e-9,
    }


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise PilotError(f"Use the specified bundled Python 3.12 runtime; observed {sys.version.split()[0]}")
    expected_runtime = str(PYTHON_RUNTIME.resolve()).casefold()
    observed_runtime = str(Path(sys.executable).resolve()).casefold()
    if observed_runtime != expected_runtime:
        raise PilotError(f"Use the specified bundled Python runtime: {PYTHON_RUNTIME}; observed {sys.executable}")

    output_dir = ROOT / "outputs" / "retained_m2_filter_fix_20261021"
    report_path = ROOT / "reports" / "retained_M1_M3_preanalysis_m2_filter_fix_20261021.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise PilotError(f"Refusing to overwrite existing output artifacts in {output_dir}")
    if report_path.exists():
        raise PilotError(f"Refusing to overwrite existing module report {report_path}")

    print("[1/6] Checking retained inputs and frozen population gate...", flush=True)
    candidates, summaries, manifest_info = get_input_paths()
    row_expectations, identity = check_input_identity(candidates, summaries, manifest_info)
    for country in ("US", "BR"):
        observed = sum(row_expectations[(country, year)] for year in YEARS)
        if observed != EXPECTED_RETAINED_ROWS[country]:
            raise PilotError(f"Expected {EXPECTED_RETAINED_ROWS[country]} retained {country} rows, found {observed}")
    population, weights, population_inputs = load_frozen_tables()
    gate_path = Path(population_inputs["m0_population_gate_path"])
    if not gate_path.exists() or population_inputs.get("m0_population_gate_status") != "PASS":
        raise PilotError("M1-M3 requires a present, passing M0 population gate")

    print("[2/6] Streaming retained candidate files once...", flush=True)
    base, m1_joint, m1_cancer, m1_cancer_detail, m2, ucd_codes, read_checks = read_candidates(candidates, row_expectations, population)
    expected_total = sum(EXPECTED_RETAINED_ROWS.values())
    if read_checks["candidate_rows"] != expected_total:
        raise PilotError(f"Expected {expected_total} retained rows, found {read_checks['candidate_rows']}")

    print("[3/6] Verifying frozen-cell matching, M1 closure, and M2 de-duplication...", flush=True)
    cells = sorted(population)
    cell_match_rows, _ = check_936_match(base, population)
    m1_closure = verify_m1_closure(base, m1_joint, cells)
    m2_dedup = verify_m2_dedup(m2, base, cells)

    print("[4/6] Computing M1-M3 fixed-window estimates...", flush=True)
    script_path = Path(__file__).resolve()
    protocol_path = ROOT / "PILOT_PROTOCOL.md"
    input_hashes = list(identity["hashes"])
    input_hashes.extend([
        {"role": "release_manifest", "path": str(EXT / "release_manifest.json"), "sha256": sha256_file(EXT / "release_manifest.json")},
        {"role": "frozen_population", "path": population_inputs["population_path"], "sha256": population_inputs["population_sha256"]},
        {"role": "who18_weights", "path": population_inputs["weights_path"], "sha256": population_inputs["weights_sha256"]},
        {"role": "m0_population_gate", "path": str(gate_path), "sha256": population_inputs["m0_population_gate_sha256"]},
        {"role": "pilot_protocol", "path": str(protocol_path), "sha256": sha256_file(protocol_path)},
        {"role": "analysis_script", "path": str(script_path), "sha256": sha256_file(script_path)},
    ])
    with tempfile.TemporaryDirectory(prefix=".retained_m1_m3_", dir=output_dir) as temporary_directory:
        stage = Path(temporary_directory)
        write_csv(stage / "M1_frozen_936_cell_match.csv", ["country", "year", "age_group", "sex", "measure", "rebuilt", "frozen", "difference"], cell_match_rows)
        m1_metrics = m1_outputs(base, m1_joint, m1_cancer, m1_cancer_detail, ucd_codes, population, weights, stage, cells)
        m2_rates, m2_contrasts = m2_outputs(m2, population, weights, stage, cells)
        m3_rates, m3_contrasts = m3_outputs(m2, population, weights, stage)
        independent_reference = verify_independent_reference_metrics(m3_rates)

        validation = {
            "status": "PASS",
            "modules": {"M1": "COMPUTED", "M2": "COMPUTED", "M3": "COMPUTED"},
            "input_scope": "current-parser retained candidate files only; no raw-death rescan",
            "runtime": {"python": sys.version.split()[0], "executable": sys.executable, "processing_threads": 1},
            "retained_candidate_rows": read_checks["candidate_rows"],
            "retained_rows_by_country": {country: sum(row["candidate_rows"] for row in read_checks["file_checks"] if row["country"] == country) for country in ("US", "BR")},
            "current_parser_sha256": identity["parser_sha256"],
            "input_file_checks": read_checks["file_checks"],
            "frozen_population": population_inputs,
            "m0_population_gate": {"path": str(gate_path), "status": population_inputs["m0_population_gate_status"], "sha256": population_inputs["m0_population_gate_sha256"]},
            "frozen_cell_match": {"status": "PASS", "unique_cells_checked": len(cells), "rows_written": len(cell_match_rows)},
            "m1_joint_partition": m1_closure,
            "m1_standardized_closure": m1_metrics,
            "m2_death_group_dedup": m2_dedup,
            "corrected_m3_independent_reference": independent_reference,
            "m2_standardized_rows": len(m2_rates),
            "m2_fixed_window_contrast_rows": len(m2_contrasts),
            "m3_standardized_rows": len(m3_rates),
            "m3_fixed_window_contrast_rows": len(m3_contrasts),
            "intervals_or_models": "none; direct standardized point estimates only",
        }
        write_json(stage / "validation.json", validation)
        report_name = report_path.name
        staged_report = stage / report_name
        staged_report.write_text(make_report(read_checks, population_inputs, m1_metrics, m2_dedup, independent_reference, m2_contrasts, m3_contrasts), encoding="utf-8")

        output_hashes = []
        for artifact in sorted(stage.iterdir()):
            if not artifact.is_file():
                continue
            final_path = report_path if artifact.name == report_name else output_dir / artifact.name
            output_hashes.append({"path": str(final_path), "sha256": sha256_file(artifact), "bytes": artifact.stat().st_size})
        manifest = {
            "schema": "retained_m1_m3_hash_manifest_v1",
            "input_scope": "current-parser retained candidate files and frozen population/WHO18 standardization inputs",
            "inputs": input_hashes,
            "outputs": output_hashes,
            "manifest_note": "The manifest does not hash itself; all other generated result files and the module report are included.",
        }
        write_json(stage / "hash_manifest.json", manifest)

        for artifact in sorted(stage.iterdir()):
            if not artifact.is_file():
                continue
            destination = report_path if artifact.name == report_name else output_dir / artifact.name
            if destination.exists():
                raise PilotError(f"Refusing to overwrite output artifact {destination}")
        for artifact in sorted(stage.iterdir()):
            if artifact.is_file():
                destination = report_path if artifact.name == report_name else output_dir / artifact.name
                artifact.replace(destination)

    print(f"[6/6] PASS — {expected_total:,} retained records; M1-M3 outputs written to {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PilotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
