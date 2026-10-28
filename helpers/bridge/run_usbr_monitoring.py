#!/usr/bin/env python3
"""Recompute the frozen US/BR retained-record margins and monitoring contrasts.

The source stream is limited to the already validated retained US and candidate
BR gzip CSVs.  It does not scan raw death records.  All rate estimands, time
windows, groups, and uncertainty rules are bound to the bridge protocol.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

for _thread_env in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_thread_env] = "1"

import numpy as np


ROOT = Path(r"D:\MCOD_Research\USBR_MCOD_bridge_decision_20261022")
EXT = Path(r"D:\MCOD_Research\USBR_MCOD_extension_20261021")
FROZEN = Path(r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build")
PILOT = Path(r"D:\MCOD_Research\USBR_MCOD_upgrade_pilot_20261021")
PYTHON_RUNTIME = Path(
    r"C:\Users\aaa12\.cache\python-runtimes\python-primary-runtime\dependencies\python\python.exe"
)

PROTOCOL_SHA256 = "804913347cb889bffd150ab2ee38a7c5ddb62ee961c934e1748248f338720356"
IMPLEMENTATION_NOTE_001_SHA256 = "2190f4ca5cb1b18e6a3f0ff74632957d1d84e9440e9f3bf3c0335d1fb3682469"
IMPLEMENTATION_NOTE_002_SHA256 = "f3c00f58e773ebbfc90bbc4a7fcbb619b4fe3d93d04b273ba9e1d4657014ad7e"
IMPLEMENTATION_NOTE_003_SHA256 = "8552545aad65c56b2a59fb93eaadb6a181ce6b51ec014d79a8ca7786bc93a9b6"

FROZEN_INPUT_HASHES = {
    "analysis_input_936.csv": "02664f35f119a700383ccde1bb97de76cbd392750dd1a96186d8892c446c13c6",
    "subcode_analysis_input_3744.csv": "e8e36abadb134c32dfc4b35f31a050afdbe1ea74d723cd80b407622abe3059b9",
    "who_2000_2025_18_group_weights_correct.csv": "5922fabf2758762c188eb91dae5e745d9328fee4978debef8e01f4bb9d5f9ba9",
}
M2_REFERENCE_SHA256 = "ed01951947b7fd43c4eaf73c87dae4a2e9ce044090cf3edc09df1597c86100f6"
CANONICAL_RETAINED_SCRIPT = PILOT / "scripts" / "retained" / "run_retained_m1_m3.py"
CANONICAL_RETAINED_SCRIPT_SHA256 = "37db07018ccdc237fc4fa32ecb574382c482aece5a09dc3080181d6ac9b75ba1"
EXPECTED_PARSER_SHA256 = {
    "US": "b8ac39e0a07049160c833aa8cccffac9732d7834216c6d2a7632cf60d67f9c97",
    "BR": "b7c575e20929b4bbaf06452bd27db2bf4e4930516b2adf0d6f99c0211d9fa7bb",
}
EXPECTED_RETAINED_ROWS = {"US": 141449, "BR": 130198}

AGE_GROUPS = tuple(["0-4"] + [f"{start}-{start + 4}" for start in range(5, 85, 5)] + ["85+"])
AGE_GROUPS_0_74 = AGE_GROUPS[:15]
SEXES = ("F", "M")
YEARS = tuple(range(2012, 2025))
EARLY_YEARS = (2012, 2013, 2014)
LATE_YEARS = (2021, 2022, 2023)
EXTERNAL_YEARS = (2019, 2022)
CASE_DEFINITIONS = ("full", "exclude_malignant_UCD", "no_malignancy_source_any")
MONITORING_DEFINITIONS = ("full", "no_malignancy_source_any")
ENDPOINTS = ("UCD", "any_mention")

# Protocol order is frozen for external CO joint-pattern masks. Bit i corresponds
# to group_order[i], with bit 0 the K80-K83 total.
GROUP_ORDER = (
    "K80-83_total",
    "K80",
    "K81",
    "K82",
    "K83",
    "K80-81",
    "K83.0",
    "K83.1",
    "K82.9",
    "K25-28",
    "K35-38",
    "K40-46",
    "K82-83",
    "K85.0/.1/.3/.8/.9",
    "K86.1/.2/.3/.8/.9",
)
GROUP_BITS = {name: 1 << i for i, name in enumerate(GROUP_ORDER)}
M2_GROUPS = ("K80", "K81", "K82", "K83", "K80-81", "K83.0", "K83.1", "K82.9")
ANALYSIS_GROUPS = ("K80-83_total",) + M2_GROUPS
M2_DEFINITIONS = CASE_DEFINITIONS
M2_REFERENCE = PILOT / "outputs" / "retained_m2_filter_fix_20261021" / "M2_code_group_counts_by_stratum.csv"

# Frozen support estimands: all-age total, primary-age total after excluding
# records with any malignant source code, and the eight M2 groups at 0-74/full.
# They share the same pattern-level Poisson weights as the primary estimands.
BOOTSTRAP_TARGETS = (
    ("0_74", "full", "K80-83_total", "primary"),
    ("all_age", "full", "K80-83_total", "support_all_age"),
    ("0_74", "no_malignancy_source_any", "K80-83_total", "support_no_malignancy"),
) + tuple(("0_74", "full", group, "support_M2_group") for group in M2_GROUPS)

BOOTSTRAP_REPS = 20_000
BOOTSTRAP_SEEDS = (20261023, 20261024)
BOOTSTRAP_BLOCK = 200
BOOTSTRAP_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
MAX_THREADS = 8
NATIVE_THREADS = 1
UNIT_PER_100K = 100_000.0


class MonitoringError(RuntimeError):
    """Raised when a frozen input, source schema, or closure invariant fails."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def valid_code_syntax(value: Any) -> bool:
    code = norm_code(value)
    return bool(re.fullmatch(r"[A-Z][0-9]{2}[A-Z0-9]{0,5}", code))


def is_malignant(value: Any) -> bool:
    match = re.match(r"^C(\d{2})", norm_code(value))
    return bool(match and int(match.group(1)) <= 97)


def matches_group(code: str, group: str) -> bool:
    code = norm_code(code)
    prefix = code[:3]
    if group == "K80-83_total":
        return prefix in {"K80", "K81", "K82", "K83"}
    if group in {"K80", "K81", "K82", "K83"}:
        return code.startswith(group)
    if group == "K80-81":
        return prefix in {"K80", "K81"}
    if group == "K83.0":
        return code.startswith("K830")
    if group == "K83.1":
        return code.startswith("K831")
    if group == "K82.9":
        return code.startswith("K829")
    if group == "K25-28":
        return bool(re.match(r"^K2[5-8]", code))
    if group == "K35-38":
        return bool(re.match(r"^K3[5-8]", code))
    if group == "K40-46":
        return bool(re.match(r"^K4[0-6]", code))
    if group == "K82-83":
        return prefix in {"K82", "K83"}
    if group == "K85.0/.1/.3/.8/.9":
        return any(code.startswith(prefix) for prefix in ("K850", "K851", "K853", "K858", "K859"))
    if group == "K86.1/.2/.3/.8/.9":
        return any(code.startswith(prefix) for prefix in ("K861", "K862", "K863", "K868", "K869"))
    raise MonitoringError(f"Unknown code group {group!r}")


def mask_for_codes(codes: Iterable[str]) -> int:
    mask = 0
    normalized = {norm_code(code) for code in codes if norm_code(code)}
    for code in normalized:
        for group, bit in GROUP_BITS.items():
            if matches_group(code, group):
                mask |= bit
    return mask


def parse_json_list(row: dict[str, str], field: str) -> list[Any]:
    value = row.get(field, "")
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise MonitoringError(f"Invalid JSON array in {field}") from error
    if not isinstance(decoded, list):
        raise MonitoringError(f"Expected a JSON list in {field}")
    return decoded


def decode_source_codes(row: dict[str, str], country: str) -> tuple[set[str], set[str]]:
    ucd = norm_code(row.get("ucd_code", ""))
    source_codes = {ucd} if ucd else set()
    if country == "US":
        extra_raw = parse_json_list(row, "entity_codes")
        if any(not isinstance(item, str) for item in extra_raw):
            raise MonitoringError("US entity_codes must contain strings")
        extra_codes = {norm_code(item) for item in extra_raw if norm_code(item)}
    else:
        extra_raw = parse_json_list(row, "line_codes")
        if any(not isinstance(item, dict) or "code" not in item for item in extra_raw):
            raise MonitoringError("BR line_codes must contain {field,code} objects")
        extra_codes = {norm_code(item["code"]) for item in extra_raw if norm_code(item["code"])}
    source_codes.update(extra_codes)
    return source_codes, extra_codes


def include_definition(definition: str, malignant_ucd: bool, malignant_any: bool) -> bool:
    if definition == "full":
        return True
    if definition == "exclude_malignant_UCD":
        return not malignant_ucd
    if definition == "no_malignancy_source_any":
        return not malignant_any
    raise MonitoringError(f"Unknown case definition {definition!r}")


def expected_candidate_inputs() -> tuple[dict[tuple[str, int], Path], dict[tuple[str, int], Path], dict[str, Any]]:
    manifest_path = EXT / "release_manifest.json"
    manifest = read_json(manifest_path)
    entries = {item["path"].replace("\\", "/"): item for item in manifest.get("files", [])}
    candidates: dict[tuple[str, int], Path] = {}
    summaries: dict[tuple[str, int], Path] = {}
    for country in ("BR", "US"):
        for year in YEARS:
            if country == "BR":
                candidate_rel = f"outputs/br/candidate_records_BR_{year}.csv.gz"
            else:
                prefix = f"us_{year}_"
                matches = [
                    path for path in (EXT / "outputs" / "us").glob(f"{prefix}*_{EXPECTED_PARSER_SHA256['US'][:12]}_retained.csv.gz")
                ]
                if len(matches) != 1:
                    raise MonitoringError(f"Expected one current-parser US retained file for {year}; found {len(matches)}")
                candidate_rel = matches[0].relative_to(EXT).as_posix()
            candidate_path = EXT / Path(candidate_rel)
            if country == "BR":
                summary_path = EXT / "outputs" / "br" / f"summary_BR_{year}.json"
            else:
                summary_path = EXT / Path(candidate_rel.removesuffix("_retained.csv.gz") + "_summary.json")
            summary_rel = summary_path.relative_to(EXT).as_posix()
            if candidate_rel not in entries or summary_rel not in entries:
                raise MonitoringError(f"Input absent from extension release manifest: {candidate_rel} or {summary_rel}")
            candidates[(country, year)] = candidate_path
            summaries[(country, year)] = summary_path
    return candidates, summaries, {"manifest_path": manifest_path, "manifest": manifest, "entries": entries}


def check_input_identity(
    candidates: dict[tuple[str, int], Path],
    summaries: dict[tuple[str, int], Path],
    manifest_info: dict[str, Any],
) -> dict[str, Any]:
    entries = manifest_info["entries"]
    hash_rows: list[dict[str, Any]] = []
    observed_parsers: dict[str, str] = {}
    for country in ("US", "BR"):
        path = EXT / "scripts" / f"extract_{country.lower()}.py"
        observed = sha256_file(path)
        if observed != EXPECTED_PARSER_SHA256[country]:
            raise MonitoringError(f"Frozen current parser SHA mismatch for {country}: {observed}")
        observed_parsers[country] = observed
        hash_rows.append({"role": f"parser_{country}", "path": str(path), "sha256": observed, "bytes": path.stat().st_size})

    canonical_script_sha = sha256_file(CANONICAL_RETAINED_SCRIPT)
    if canonical_script_sha != CANONICAL_RETAINED_SCRIPT_SHA256:
        raise MonitoringError(f"Canonical retained script SHA mismatch: {canonical_script_sha}")
    hash_rows.append({"role": "canonical_retained_script", "path": str(CANONICAL_RETAINED_SCRIPT), "sha256": canonical_script_sha, "bytes": CANONICAL_RETAINED_SCRIPT.stat().st_size})

    expected_rows: dict[tuple[str, int], int] = {}
    for key in sorted(candidates):
        country, year = key
        candidate = candidates[key]
        summary = summaries[key]
        for role, path in (("candidate_record_stream", candidate), ("candidate_summary", summary)):
            rel = path.relative_to(EXT).as_posix()
            observed = sha256_file(path)
            registered = entries[rel]
            if observed != registered.get("sha256") or path.stat().st_size != int(registered.get("bytes", -1)):
                raise MonitoringError(f"Release manifest identity mismatch for {rel}")
            hash_rows.append({"role": role, "path": str(path), "sha256": observed, "bytes": path.stat().st_size})
        summary_json = read_json(summary)
        if summary_json.get("parser_sha256") != EXPECTED_PARSER_SHA256[country]:
            raise MonitoringError(f"Candidate summary is not bound to the frozen {country} parser: {summary}")
        try:
            if country == "US":
                n = int(summary_json["retained_biliary_records"])
            else:
                n = int(summary_json["source_audit_by_year"][str(year)]["candidate_records"])
        except (KeyError, TypeError, ValueError) as error:
            raise MonitoringError(f"Missing retained row count in {summary}") from error
        expected_rows[key] = n

    identity_path = EXT / "outputs" / "identity" / "source_identity.csv"
    identity_rows = csv_rows(identity_path)
    if len(identity_rows) != 26 or any(row.get("status") != "MATCH" for row in identity_rows):
        raise MonitoringError("Source identity ledger must contain 26 MATCH rows")
    binding_path = EXT / "outputs" / "review" / "full_window_artifact_bindings.json"
    bindings = read_json(binding_path)
    if (
        bindings.get("status") != "PASS"
        or bindings.get("current_parser_summaries") != 26
        or bindings.get("source_identity_MATCH_bindings") != 26
        or bindings.get("raw_rescan") is not False
    ):
        raise MonitoringError("Full-window artifact binding audit did not PASS")
    audit_path = EXT / "outputs" / "review" / "final_independent_review.json"
    audit = read_json(audit_path)
    if (
        audit.get("status") != "PASS"
        or audit.get("retained_candidates_independently_recounted") != sum(EXPECTED_RETAINED_ROWS.values())
        or audit.get("duplicate_keys") != 0
        or audit.get("flag_evidence_discrepancies") != 0
    ):
        raise MonitoringError("Independent retained-record audit is not a clean PASS")
    if sum(expected_rows.values()) != sum(EXPECTED_RETAINED_ROWS.values()):
        raise MonitoringError("Candidate summary totals differ from frozen retained-row totals")
    for country in ("US", "BR"):
        country_rows = sum(value for (c, _), value in expected_rows.items() if c == country)
        if country_rows != EXPECTED_RETAINED_ROWS[country]:
            raise MonitoringError(f"Expected {EXPECTED_RETAINED_ROWS[country]} retained {country} records, got {country_rows}")

    for path in (identity_path, binding_path, audit_path):
        hash_rows.append({"role": "source_identity_audit", "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    manifest_path = manifest_info["manifest_path"]
    return {
        "release_manifest_path": str(manifest_path),
        "release_manifest_sha256": sha256_file(manifest_path),
        "parser_sha256": observed_parsers,
        "canonical_retained_script_sha256": canonical_script_sha,
        "expected_rows_by_country_year": {f"{c}_{y}": n for (c, y), n in sorted(expected_rows.items())},
        "candidate_rows_by_country": EXPECTED_RETAINED_ROWS,
        "source_identity_match_rows": len(identity_rows),
        "full_window_bindings": bindings,
        "independent_review": audit,
        "input_hashes": hash_rows,
    }


def load_frozen_inputs() -> tuple[dict[tuple[str, int, str, str], dict[str, int]], dict[str, float], list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    paths = {
        "analysis_input_936.csv": FROZEN / "data" / "derived" / "analysis_input_936.csv",
        "subcode_analysis_input_3744.csv": FROZEN / "data" / "derived" / "subcode_analysis_input_3744.csv",
        "who_2000_2025_18_group_weights_correct.csv": FROZEN / "data" / "metadata" / "who_2000_2025_18_group_weights_correct.csv",
    }
    hashes: dict[str, str] = {}
    for name, path in paths.items():
        observed = sha256_file(path)
        if observed != FROZEN_INPUT_HASHES[name]:
            raise MonitoringError(f"Frozen table SHA mismatch for {name}: {observed}")
        hashes[name] = observed

    rows_936 = csv_rows(paths["analysis_input_936.csv"])
    rows_3744 = csv_rows(paths["subcode_analysis_input_3744.csv"])
    weight_rows = csv_rows(paths["who_2000_2025_18_group_weights_correct.csv"])
    population: dict[tuple[str, int, str, str], dict[str, int]] = {}
    for row in rows_936:
        try:
            key = (row["country"], int(row["year"]), row["age_group"], row["sex"])
            parsed = {name: int(row[name]) for name in ("population", "ucd", "any_common")}
        except (KeyError, ValueError) as error:
            raise MonitoringError("Invalid row in frozen 936 table") from error
        if key in population:
            raise MonitoringError(f"Duplicate frozen 936 key {key}")
        population[key] = parsed
    expected_keys = {
        (country, year, age, sex)
        for country in ("US", "BR")
        for year in YEARS
        for age in AGE_GROUPS
        for sex in SEXES
    }
    if len(rows_936) != 936 or set(population) != expected_keys:
        raise MonitoringError("Frozen 936 table is not a complete unique country/year/age/sex grid")

    weights = {row["age_group"]: float(row["weight"]) for row in weight_rows}
    if len(weight_rows) != 18 or set(weights) != set(AGE_GROUPS):
        raise MonitoringError("WHO18 weights do not cover exactly the frozen 18 age groups")
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0, abs_tol=1e-12):
        raise MonitoringError("WHO18 weights do not sum to one")

    if len(rows_3744) != 3744:
        raise MonitoringError(f"Frozen 3744 component table has {len(rows_3744)} rows")
    component_keys: set[tuple[str, int, str, str, str]] = set()
    for row in rows_3744:
        key = (row["country"], int(row["year"]), row["age_group"], row["sex"], row["subcode"])
        if key in component_keys:
            raise MonitoringError(f"Duplicate frozen subcode cell {key}")
        component_keys.add(key)
    expected_component_keys = {
        (country, year, age, sex, group)
        for country in ("US", "BR")
        for year in YEARS
        for age in AGE_GROUPS
        for sex in SEXES
        for group in ("K80", "K81", "K82", "K83")
    }
    if component_keys != expected_component_keys:
        raise MonitoringError("Frozen 3744 table is not a complete unique four-code-family grid")
    metadata = {
        "paths": {name: str(path) for name, path in paths.items()},
        "sha256": hashes,
        "population_cells": len(rows_936),
        "subcode_cells": len(rows_3744),
        "who18_original_weight_sum": sum(weights.values()),
        "who18_0_74_original_weight_sum": sum(weights[age] for age in AGE_GROUPS_0_74),
        "who18_0_74_renormalized_weight_sum": 1.0,
    }
    return population, weights, rows_3744, rows_936, metadata


def read_and_aggregate(
    candidates: dict[tuple[str, int], Path],
    expected_rows: dict[tuple[str, int], int],
) -> tuple[Counter, Counter, Counter, dict[str, Any]]:
    pattern_counts: Counter[tuple[Any, ...]] = Counter()
    group_counts: Counter[tuple[Any, ...]] = Counter()
    cell_counts: Counter[tuple[Any, ...]] = Counter()
    checks: list[dict[str, Any]] = []
    total = 0
    for country, year in sorted(candidates):
        path = candidates[(country, year)]
        row_count = 0
        previous_source_row = 0
        expected_fields = (
            {"country", "year", "source_row", "age_group", "sex", "ucd_code", "entity_codes"}
            if country == "US"
            else {"country", "year", "source_row", "age_group", "sex", "ucd_code", "line_codes"}
        )
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not expected_fields.issubset(reader.fieldnames or []):
                raise MonitoringError(f"Candidate schema mismatch in {path}")
            for row in reader:
                try:
                    observed_country = row["country"]
                    observed_year = int(row["year"])
                    source_row = int(row["source_row"])
                    age_group = row["age_group"]
                    sex = row["sex"]
                except (KeyError, ValueError) as error:
                    raise MonitoringError(f"Invalid identity or stratum field in {path}") from error
                if observed_country != country or observed_year != year:
                    raise MonitoringError(f"Candidate row country/year differs from expected file {path}")
                if source_row <= previous_source_row:
                    raise MonitoringError(f"source_row is not strictly increasing in {path}")
                previous_source_row = source_row
                if age_group not in AGE_GROUPS or sex not in SEXES:
                    raise MonitoringError(f"US/BR candidate has unsupported age/sex stratum: {age_group}/{sex}")
                if (country, year, age_group, sex) not in EXPECTED_POPULATION_KEYS:
                    raise MonitoringError(f"Candidate stratum absent from frozen population grid: {(country, year, age_group, sex)}")

                ucd_code = norm_code(row.get("ucd_code", ""))
                if not ucd_code:
                    ucd_status = "MISSING"
                elif valid_code_syntax(ucd_code):
                    ucd_status = "PARSED"
                else:
                    ucd_status = "UNPARSED"
                source_codes, extra_codes = decode_source_codes(row, country)
                malformed_extra = any(not valid_code_syntax(code) for code in extra_codes)
                cause_status = "EMPTY" if not extra_codes else ("PARTIAL_UNPARSED" if malformed_extra else "PARSED")
                ucd_mask = mask_for_codes((ucd_code,))
                any_mask = mask_for_codes(source_codes)
                if not any_mask & GROUP_BITS["K80-83_total"]:
                    raise MonitoringError(f"Retained candidate lacks a source-ANY K80-K83 code in {path}, source_row={source_row}")
                if ucd_mask & ~any_mask:
                    raise MonitoringError("UCD mask is not a subset of source-ANY mask")
                malignant_ucd = is_malignant(ucd_code)
                malignant_any = any(is_malignant(code) for code in source_codes)
                cell = (country, year, age_group, sex)
                cell_counts[cell + ("candidate",)] += 1
                cell_counts[cell + ("malignant_source_any",)] += int(malignant_any)

                for definition in CASE_DEFINITIONS:
                    if not include_definition(definition, malignant_ucd, malignant_any):
                        continue
                    for index, group in enumerate(GROUP_ORDER):
                        bit = 1 << index
                        if ucd_mask & bit:
                            group_counts[cell + (definition, group, "UCD")] += 1
                        if any_mask & bit:
                            group_counts[cell + (definition, group, "any_mention")] += 1

                pattern_counts[
                    (
                        country,
                        year,
                        age_group,
                        sex,
                        int(malignant_any),
                        int(malignant_ucd),
                        ucd_mask,
                        any_mask,
                        ucd_status,
                        cause_status,
                        int(not ucd_code),
                    )
                ] += 1
                row_count += 1
        total += row_count
        if row_count != expected_rows[(country, year)]:
            raise MonitoringError(f"Candidate stream row count mismatch for {country}/{year}: {row_count} != {expected_rows[(country, year)]}")
        checks.append(
            {
                "country": country,
                "year": year,
                "candidate_rows": row_count,
                "source_row_order": "strictly_increasing",
                "file_sha256": sha256_file(path),
                "file_bytes": path.stat().st_size,
            }
        )
    if total != sum(EXPECTED_RETAINED_ROWS.values()):
        raise MonitoringError(f"Expected {sum(EXPECTED_RETAINED_ROWS.values())} retained candidate rows, read {total}")
    return pattern_counts, group_counts, cell_counts, {"candidate_rows": total, "file_checks": checks, "pattern_rows": len(pattern_counts)}


def pattern_rows(pattern_counts: Counter) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, n in sorted(pattern_counts.items()):
        country, year, age_group, sex, malig_any, malig_ucd, ucd_mask, any_mask, ucd_status, cause_status, ucd_missing = key
        result.append(
            {
                "country": country,
                "year": year,
                "age_group": age_group,
                "sex": sex,
                "residency": "NA",
                "ucd_mask_lower": ucd_mask,
                "ucd_mask_upper": ucd_mask,
                "any_mask_lower": any_mask,
                "any_mask_upper": any_mask,
                "malignancy_source_any_lower": malig_any,
                "malignancy_source_any_upper": malig_any,
                "malignant_ucd_lower": malig_ucd,
                "malignant_ucd_upper": malig_ucd,
                "ucd_parse_status": ucd_status,
                "cause_parse_status": cause_status,
                "ucd_missing_flag": ucd_missing,
                "n_pattern": n,
            }
        )
    return result


def group_count(
    counts: Counter,
    country: str,
    year: int,
    age: str,
    sex: str,
    definition: str,
    group: str,
    endpoint: str,
) -> int:
    return int(counts.get((country, year, age, sex, definition, group, endpoint), 0))


def verify_closures(
    group_counts: Counter,
    cell_counts: Counter,
    population: dict[tuple[str, int, str, str], dict[str, int]],
    rows_936: list[dict[str, str]],
    rows_3744: list[dict[str, str]],
    output_dir: Path,
) -> dict[str, Any]:
    closure_936: list[dict[str, Any]] = []
    for row in rows_936:
        country, year, age, sex = row["country"], int(row["year"]), row["age_group"], row["sex"]
        for endpoint, field in (("UCD", "ucd"), ("any_mention", "any_common")):
            observed = group_count(group_counts, country, year, age, sex, "full", "K80-83_total", endpoint)
            expected = int(row[field])
            closure_936.append(
                {"country": country, "year": year, "age_group": age, "sex": sex, "endpoint": endpoint, "observed": observed, "expected_936": expected, "match": observed == expected}
            )
            if observed != expected:
                raise MonitoringError(f"936 closure mismatch for {(country, year, age, sex, endpoint)}: {observed} != {expected}")
        any_n = group_count(group_counts, country, year, age, sex, "full", "K80-83_total", "any_mention")
        candidate_n = int(cell_counts.get((country, year, age, sex, "candidate"), 0))
        if any_n != candidate_n:
            raise MonitoringError(f"Retained candidate/union total mismatch for {(country, year, age, sex)}")
    if len(closure_936) != 1872 or not all(row["match"] for row in closure_936):
        raise MonitoringError("936 closure table failed or has unexpected dimensionality")

    closure_3744: list[dict[str, Any]] = []
    for row in rows_3744:
        country, year, age, sex, group = row["country"], int(row["year"]), row["age_group"], row["sex"], row["subcode"]
        for endpoint, field in (("UCD", "ucd"), ("any_mention", "any_common")):
            observed = group_count(group_counts, country, year, age, sex, "full", group, endpoint)
            expected = int(row[field])
            closure_3744.append(
                {"country": country, "year": year, "age_group": age, "sex": sex, "code_group": group, "endpoint": endpoint, "observed": observed, "expected_3744": expected, "match": observed == expected}
            )
            if observed != expected:
                raise MonitoringError(f"3744 closure mismatch for {(country, year, age, sex, group, endpoint)}: {observed} != {expected}")
    if len(closure_3744) != 7488 or not all(row["match"] for row in closure_3744):
        raise MonitoringError("3744 closure table failed or has unexpected dimensionality")

    ref_hash = sha256_file(M2_REFERENCE)
    if ref_hash != M2_REFERENCE_SHA256:
        raise MonitoringError(f"Corrected M2 reference SHA mismatch: {ref_hash}")
    ref_rows = csv_rows(M2_REFERENCE)
    reference: dict[tuple[Any, ...], int] = {}
    for row in ref_rows:
        key = (row["country"], int(row["year"]), row["age_group"], row["sex"], row["case_definition"], row["code_group"], row["endpoint"])
        if key in reference:
            raise MonitoringError(f"Duplicate corrected M2 reference key {key}")
        reference[key] = int(row["deaths"])
    expected_ref_keys = {
        (country, year, age, sex, definition, group, endpoint)
        for country in ("US", "BR")
        for year in YEARS
        for age in AGE_GROUPS
        for sex in SEXES
        for definition in CASE_DEFINITIONS
        for group in M2_GROUPS
        for endpoint in ENDPOINTS
    }
    if set(reference) != expected_ref_keys:
        raise MonitoringError(f"Corrected M2 reference key set differs from contract: {len(reference)} vs {len(expected_ref_keys)}")
    m2_comparison: list[dict[str, Any]] = []
    for key in sorted(expected_ref_keys):
        observed = group_counts.get(key, 0)
        expected = reference[key]
        matches = observed == expected
        m2_comparison.append(
            {
                "country": key[0],
                "year": key[1],
                "age_group": key[2],
                "sex": key[3],
                "case_definition": key[4],
                "code_group": key[5],
                "endpoint": key[6],
                "recomputed_deaths": observed,
                "corrected_M2_deaths": expected,
                "match": matches,
            }
        )
        if not matches:
            raise MonitoringError(f"Corrected M2 count mismatch at {key}: {observed} != {expected}")
    if len(m2_comparison) != 44928:
        raise MonitoringError(f"Unexpected M2 comparison dimensions: {len(m2_comparison)}")

    write_csv(output_dir / "closure_936.csv", list(closure_936[0]), closure_936)
    write_csv(output_dir / "closure_3744.csv", list(closure_3744[0]), closure_3744)
    write_csv(output_dir / "corrected_m2_comparison.csv", list(m2_comparison[0]), m2_comparison)
    return {
        "status": "PASS",
        "936_endpoint_cells_checked": len(closure_936),
        "3744_endpoint_cells_checked": len(closure_3744),
        "corrected_m2_rows_checked": len(m2_comparison),
        "corrected_m2_sha256": ref_hash,
        "mismatches": 0,
    }


def standardized_weight(age: str, scope: str, weights: dict[str, float]) -> float:
    ages = AGE_GROUPS_0_74 if scope == "0_74" else AGE_GROUPS
    if age not in ages:
        return 0.0
    denominator = sum(weights[item] for item in ages)
    return weights[age] / denominator


def compute_annual_rates(
    group_counts: Counter,
    population: dict[tuple[str, int, str, str], dict[str, int]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for country in ("US", "BR"):
        for year in YEARS:
            for scope in ("0_74", "all_age"):
                for definition in MONITORING_DEFINITIONS:
                    for group in ANALYSIS_GROUPS:
                        for endpoint in ENDPOINTS:
                            rate = 0.0
                            raw_deaths = 0
                            for age in (AGE_GROUPS_0_74 if scope == "0_74" else AGE_GROUPS):
                                w_age = standardized_weight(age, scope, weights)
                                for sex in SEXES:
                                    pop_row = population[(country, year, age, sex)]
                                    denominator = pop_row["population"]
                                    if denominator <= 0:
                                        raise MonitoringError(f"Nonpositive population denominator at {(country, year, age, sex)}")
                                    deaths = group_count(group_counts, country, year, age, sex, definition, group, endpoint)
                                    raw_deaths += deaths
                                    rate += 0.5 * w_age * deaths / denominator * UNIT_PER_100K
                            rows.append(
                                {
                                    "country": country,
                                    "year": year,
                                    "age_scope": scope,
                                    "case_definition": definition,
                                    "code_group": group,
                                    "endpoint": endpoint,
                                    "raw_deaths_in_scope": raw_deaths,
                                    "standardized_rate_per_100k": rate,
                                    "rate_unit": "per_100000_population",
                                }
                            )
    return rows


def average_rate(annual: dict[tuple[Any, ...], float], key: tuple[Any, ...], years: tuple[int, ...]) -> float:
    values = [annual[key + (year,)] for year in years]
    return sum(values) / len(values)


def build_monitoring_estimates(annual_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annual = {
        (row["country"], row["age_scope"], row["case_definition"], row["code_group"], row["endpoint"], row["year"]): float(row["standardized_rate_per_100k"])
        for row in annual_rows
    }
    rows: list[dict[str, Any]] = []
    for scope in ("0_74", "all_age"):
        for definition in MONITORING_DEFINITIONS:
            for group in ANALYSIS_GROUPS:
                by_endpoint: dict[str, dict[str, float]] = {}
                for endpoint in ENDPOINTS:
                    us_early = average_rate(annual, ("US", scope, definition, group, endpoint), EARLY_YEARS)
                    br_early = average_rate(annual, ("BR", scope, definition, group, endpoint), EARLY_YEARS)
                    us_late = average_rate(annual, ("US", scope, definition, group, endpoint), LATE_YEARS)
                    br_late = average_rate(annual, ("BR", scope, definition, group, endpoint), LATE_YEARS)
                    if min(us_early, br_early, us_late, br_late) <= 0:
                        rr_early = rr_late = logrr_early = logrr_late = math.nan
                    else:
                        rr_early, rr_late = br_early / us_early, br_late / us_late
                        logrr_early, logrr_late = math.log(rr_early), math.log(rr_late)
                    rd_early, rd_late = br_early - us_early, br_late - us_late
                    gapr_early = abs(logrr_early) if math.isfinite(logrr_early) else math.nan
                    gapr_late = abs(logrr_late) if math.isfinite(logrr_late) else math.nan
                    gapa_early, gapa_late = abs(rd_early), abs(rd_late)
                    t_r = gapr_late - gapr_early if math.isfinite(gapr_early + gapr_late) else math.nan
                    t_a = gapa_late - gapa_early
                    by_endpoint[endpoint] = {"T_R": t_r, "T_A": t_a}
                    rows.append(
                        {
                            "age_scope": scope,
                            "case_definition": definition,
                            "code_group": group,
                            "endpoint": endpoint,
                            "US_early_rate_per_100k": us_early,
                            "BR_early_rate_per_100k": br_early,
                            "US_late_rate_per_100k": us_late,
                            "BR_late_rate_per_100k": br_late,
                            "RR_early_BR_over_US": rr_early,
                            "RR_late_BR_over_US": rr_late,
                            "logRR_early": logrr_early,
                            "logRR_late": logrr_late,
                            "RD_early_BR_minus_US_per_100k": rd_early,
                            "RD_late_BR_minus_US_per_100k": rd_late,
                            "relative_gap_early_abs_logRR": gapr_early,
                            "relative_gap_late_abs_logRR": gapr_late,
                            "absolute_gap_early_abs_RD_per_100k": gapa_early,
                            "absolute_gap_late_abs_RD_per_100k": gapa_late,
                            "T_relative_gap_change": t_r,
                            "T_absolute_gap_change_per_100k": t_a,
                            "direction_early_BR_vs_US": "BR_HIGHER" if br_early > us_early else ("US_HIGHER" if br_early < us_early else "TIE"),
                            "direction_late_BR_vs_US": "BR_HIGHER" if br_late > us_late else ("US_HIGHER" if br_late < us_late else "TIE"),
                            "window_rule": "equal_weighted_mean_of_annual_standardized_rates",
                        }
                    )
                ucd, any_ = by_endpoint["UCD"], by_endpoint["any_mention"]
                rows.append(
                    {
                        "age_scope": scope,
                        "case_definition": definition,
                        "code_group": group,
                        "endpoint": "DEFINITION_INTERACTION",
                        "T_relative_gap_change": any_["T_R"] - ucd["T_R"] if math.isfinite(any_["T_R"] + ucd["T_R"]) else math.nan,
                        "T_absolute_gap_change_per_100k": any_["T_A"] - ucd["T_A"],
                        "interaction_definition": "T_any_minus_T_UCD",
                        "window_rule": "equal_weighted_mean_of_annual_standardized_rates",
                    }
                )
    return rows


def bootstrap_coefficients(
    pattern_data: dict[str, Any],
    country: str,
    population: dict[tuple[str, int, str, str], dict[str, int]],
    weights: dict[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Build annual-rate coefficients for all frozen primary/support targets.

    A pattern row receives one Poisson weight per draw. Its coefficients are
    then reused across endpoints, case definitions, age scopes, and code groups,
    preserving the joint-pattern dependence required by the protocol.
    """
    rows = [row for row in pattern_data["rows"] if row["country"] == country]
    lambda_values: list[int] = []
    coefficient_rows: list[np.ndarray] = []
    feature_count = len(YEARS) * len(BOOTSTRAP_TARGETS) * len(ENDPOINTS)
    for row in rows:
        year = int(row["year"])
        age = row["age_group"]
        sex = row["sex"]
        stratum = (country, year, age, sex)
        denominator = population[stratum]["population"]
        coeff = np.zeros(feature_count, dtype=np.float64)
        year_index = YEARS.index(year)
        malignant_any = int(row["malignancy_source_any_lower"])
        ucd_mask = int(row["ucd_mask_lower"])
        any_mask = int(row["any_mask_lower"])
        for target_index, (scope, definition, group, _role) in enumerate(BOOTSTRAP_TARGETS):
            if scope == "0_74" and age not in AGE_GROUPS_0_74:
                continue
            if definition == "no_malignancy_source_any" and malignant_any:
                continue
            age_w = standardized_weight(age, scope, weights)
            group_bit = GROUP_BITS[group]
            for endpoint_index, mask in enumerate((ucd_mask, any_mask)):
                if mask & group_bit:
                    feature_index = (
                        year_index * len(BOOTSTRAP_TARGETS) * len(ENDPOINTS)
                        + target_index * len(ENDPOINTS)
                        + endpoint_index
                    )
                    coeff[feature_index] = 0.5 * age_w / denominator * UNIT_PER_100K
        if not np.any(coeff):
            continue
        lambda_values.append(int(row["n_pattern"]))
        coefficient_rows.append(coeff)
    if not lambda_values:
        raise MonitoringError(f"No analysis-group pattern rows for {country}")
    lambda_array = np.asarray(lambda_values, dtype=np.int64)
    coefficient_array = np.vstack(coefficient_rows)
    return lambda_array, coefficient_array


def metrics_from_rates(rate_us: np.ndarray, rate_br: np.ndarray) -> dict[str, np.ndarray]:
    """Convert arrays shaped (..., year, endpoint) into T/I and signed gaps."""
    early_idx = [YEARS.index(year) for year in EARLY_YEARS]
    late_idx = [YEARS.index(year) for year in LATE_YEARS]
    us_early = rate_us[..., early_idx, :].mean(axis=-2)
    br_early = rate_br[..., early_idx, :].mean(axis=-2)
    us_late = rate_us[..., late_idx, :].mean(axis=-2)
    br_late = rate_br[..., late_idx, :].mean(axis=-2)
    with np.errstate(divide="ignore", invalid="ignore"):
        logrr_early = np.log(br_early / us_early)
        logrr_late = np.log(br_late / us_late)
    rd_early = br_early - us_early
    rd_late = br_late - us_late
    gapr_early, gapr_late = np.abs(logrr_early), np.abs(logrr_late)
    gapa_early, gapa_late = np.abs(rd_early), np.abs(rd_late)
    t_r = gapr_late - gapr_early
    t_a = gapa_late - gapa_early
    values = {
        "T_UCD_R": t_r[..., 0],
        "T_any_R": t_r[..., 1],
        "I_R": t_r[..., 1] - t_r[..., 0],
        "T_UCD_A": t_a[..., 0],
        "T_any_A": t_a[..., 1],
        "I_A": t_a[..., 1] - t_a[..., 0],
        "signed_logRR_UCD_early": logrr_early[..., 0],
        "signed_logRR_UCD_late": logrr_late[..., 0],
        "signed_logRR_any_early": logrr_early[..., 1],
        "signed_logRR_any_late": logrr_late[..., 1],
        "signed_RD_UCD_early": rd_early[..., 0],
        "signed_RD_UCD_late": rd_late[..., 0],
        "signed_RD_any_early": rd_early[..., 1],
        "signed_RD_any_late": rd_late[..., 1],
    }
    return values


def target_metric_values(rate_us: np.ndarray, rate_br: np.ndarray) -> dict[tuple[str, str, str, str], dict[str, np.ndarray]]:
    """Convert flattened annual feature draws into metrics for each target."""
    us = np.asarray(rate_us, dtype=np.float64)
    br = np.asarray(rate_br, dtype=np.float64)
    if us.shape != br.shape:
        raise MonitoringError("US and BR annual-rate draw shapes do not match")
    if us.ndim == 1:
        us = us[np.newaxis, :]
        br = br[np.newaxis, :]
    expected = len(YEARS) * len(BOOTSTRAP_TARGETS) * len(ENDPOINTS)
    if us.ndim != 2 or us.shape[1] != expected:
        raise MonitoringError(f"Annual-rate feature shape is invalid: {us.shape}")
    shape = (us.shape[0], len(YEARS), len(BOOTSTRAP_TARGETS), len(ENDPOINTS))
    us = us.reshape(shape)
    br = br.reshape(shape)
    result: dict[tuple[str, str, str, str], dict[str, np.ndarray]] = {}
    for target_index, target in enumerate(BOOTSTRAP_TARGETS):
        result[target] = metrics_from_rates(us[:, :, target_index, :], br[:, :, target_index, :])
    return result


def primary_metric_values(rate_us: np.ndarray, rate_br: np.ndarray) -> dict[str, np.ndarray]:
    """Backward-compatible primary-only helper used by focused unit tests."""
    shape = rate_us.shape[:-1]
    us = rate_us.reshape(shape + (len(YEARS), len(ENDPOINTS)))
    br = rate_br.reshape(shape + (len(YEARS), len(ENDPOINTS)))
    return metrics_from_rates(us, br)


def classify_interval(lower: float, upper: float) -> str:
    if upper < 0:
        return "GAP_NARROWING"
    if lower > 0:
        return "GAP_WIDENING"
    return "DIRECTION_UNRESOLVED"


def classify_interval_row(row: dict[str, Any]) -> str:
    """Do not classify a finite-only interval when any bootstrap draw is invalid."""
    if int(row["valid_repetitions"]) != int(row["bootstrap_repetitions"]):
        return "NONFINITE_UNRESOLVED"
    return classify_interval(float(row["family_ci_lower"]), float(row["family_ci_upper"]))


def classify_interaction_interval(lower: float, upper: float) -> str:
    """Label interaction sign without misrepresenting it as a gap direction."""
    if upper < 0:
        return "NEGATIVE_INTERACTION"
    if lower > 0:
        return "POSITIVE_INTERACTION"
    return "INTERACTION_UNRESOLVED"


def percentile_interval(values: np.ndarray, alpha: float) -> tuple[float, float, int]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return math.nan, math.nan, 0
    lower, upper = np.quantile(finite, [alpha / 2, 1 - alpha / 2], method="linear")
    return float(lower), float(upper), int(finite.size)


def bootstrap_usbr(
    pattern_data: dict[str, Any],
    population: dict[tuple[str, int, str, str], dict[str, int]],
    weights: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    coefficients = {
        country: bootstrap_coefficients(pattern_data, country, population, weights)
        for country in ("US", "BR")
    }
    block_bytes = max(len(coefficients[country][0]) for country in coefficients) * BOOTSTRAP_BLOCK * np.dtype(np.int64).itemsize
    if block_bytes > BOOTSTRAP_MEMORY_LIMIT_BYTES:
        raise MonitoringError(f"Bootstrap block estimate {block_bytes} bytes exceeds the 256 MiB contract")
    interval_rows: list[dict[str, Any]] = []
    seed_metadata: dict[str, Any] = {}
    point_annual: dict[str, np.ndarray] = {}
    for country_index, country in enumerate(("US", "BR")):
        lam, coef = coefficients[country]
        point_annual[country] = lam @ coef
    point_by_target = target_metric_values(point_annual["US"], point_annual["BR"])

    alpha_primary = 0.05 / 6
    alpha_gap = 0.05 / 8
    for seed in BOOTSTRAP_SEEDS:
        child_seeds = np.random.SeedSequence(seed).spawn(2)
        rngs = {country: np.random.default_rng(child_seeds[i]) for i, country in enumerate(("US", "BR"))}
        draws: dict[tuple[str, str, str, str], dict[str, list[np.ndarray]]] = {
            target: {name: [] for name in point_by_target[target]}
            for target in BOOTSTRAP_TARGETS
        }
        valid_us = valid_br = 0
        for start in range(0, BOOTSTRAP_REPS, BOOTSTRAP_BLOCK):
            n = min(BOOTSTRAP_BLOCK, BOOTSTRAP_REPS - start)
            annual_rates: dict[str, np.ndarray] = {}
            for country in ("US", "BR"):
                lam, coef = coefficients[country]
                weights_draw = rngs[country].poisson(lam=lam, size=(n, lam.size))
                annual_rates[country] = weights_draw @ coef
                if country == "US":
                    valid_us += n
                else:
                    valid_br += n
                del weights_draw
            values_by_target = target_metric_values(annual_rates["US"], annual_rates["BR"])
            for target, values in values_by_target.items():
                for name, draws_for_metric in draws[target].items():
                    draws_for_metric.append(np.asarray(values[name]).reshape(-1))
        summaries: dict[str, Any] = {}
        for target in BOOTSTRAP_TARGETS:
            scope, definition, group, role = target
            target_summaries: dict[str, Any] = {}
            for name, blocks in draws[target].items():
                values = np.concatenate(blocks)
                is_primary = role == "primary"
                is_ti = name in {"T_UCD_R", "T_any_R", "I_R", "T_UCD_A", "T_any_A", "I_A"}
                alpha = (alpha_primary if is_ti else alpha_gap) if is_primary else 0.05
                family_lower, family_upper, valid = percentile_interval(values, alpha)
                lo95, hi95, valid95 = percentile_interval(values, 0.05)
                estimate = float(np.asarray(point_by_target[target][name]).reshape(-1)[0])
                row = {
                    "seed": seed,
                    "analysis_role": role,
                    "age_scope": scope,
                    "case_definition": definition,
                    "code_group": group,
                    "metric": name,
                    "estimate": estimate,
                    "ci95_lower": lo95,
                    "ci95_upper": hi95,
                    "ci95_valid_repetitions": valid95,
                    "family_ci_lower": family_lower,
                    "family_ci_upper": family_upper,
                    "family_alpha": alpha,
                    "family_confidence_level": 1 - alpha,
                    "interval_method": "two_sided_percentile_numpy_quantile_linear",
                    "interval_scope": "all_repetitions" if valid == BOOTSTRAP_REPS else "finite_only_descriptive_nonclassifying",
                    "bootstrap_repetitions": BOOTSTRAP_REPS,
                    "valid_repetitions": valid,
                    "lower_tail_theoretical_repetitions": BOOTSTRAP_REPS * alpha / 2,
                    "upper_tail_theoretical_repetitions": BOOTSTRAP_REPS * alpha / 2,
                }
                interval_rows.append(row)
                target_summaries[name] = {
                    "lower": family_lower,
                    "upper": family_upper,
                    "valid": valid,
                    "repetitions": BOOTSTRAP_REPS,
                    "alpha": alpha,
                    "interval_scope": row["interval_scope"],
                }
            summaries[f"{scope}|{definition}|{group}"] = target_summaries
        seed_metadata[str(seed)] = {
            "status": "PASS",
            "repetitions": BOOTSTRAP_REPS,
            "block_size_max": BOOTSTRAP_BLOCK,
            "block_array_estimate_bytes": block_bytes,
            "valid_US_repetitions": valid_us,
            "valid_BR_repetitions": valid_br,
            "metrics": summaries,
        }
    primary_target = BOOTSTRAP_TARGETS[0]
    point_values = {name: float(np.asarray(values).reshape(-1)[0]) for name, values in point_by_target[primary_target].items()}
    point_targets = {
        "|".join(target[:3]): {name: float(np.asarray(value).reshape(-1)[0]) for name, value in point_by_target[target].items()}
        for target in BOOTSTRAP_TARGETS
    }
    return interval_rows, {
        "status": "PASS",
        "point_metrics": point_values,
        "point_metrics_by_target": point_targets,
        "targets": [
            {"age_scope": target[0], "case_definition": target[1], "code_group": target[2], "analysis_role": target[3]}
            for target in BOOTSTRAP_TARGETS
        ],
        "seeds": seed_metadata,
        "bootstrap_contract": {
            "repetitions": BOOTSTRAP_REPS,
            "seeds": list(BOOTSTRAP_SEEDS),
            "block_size_max": BOOTSTRAP_BLOCK,
            "max_threads": MAX_THREADS,
            "native_threads": NATIVE_THREADS,
            "draws_shared_across_all_targets_within_country": True,
            "country_draws_independent": True,
        },
    }


def verify_bootstrap_point_estimates(bootstrap_meta: dict[str, Any], monitoring_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Require coefficient-derived point rates to reproduce annual-rate outputs."""
    indexed = {
        (row["age_scope"], row["case_definition"], row["code_group"], row["endpoint"]): row
        for row in monitoring_rows
    }
    targets = bootstrap_meta.get("point_metrics_by_target", {})
    checked = 0
    for target in BOOTSTRAP_TARGETS:
        scope, definition, group, _role = target
        target_key = "|".join(target[:3])
        point_values = targets.get(target_key)
        if point_values is None:
            raise MonitoringError(f"Missing bootstrap point metrics for {target_key}")
        ucd = indexed[(scope, definition, group, "UCD")]
        any_ = indexed[(scope, definition, group, "any_mention")]
        interaction = indexed[(scope, definition, group, "DEFINITION_INTERACTION")]
        expected = {
            "T_UCD_R": ucd["T_relative_gap_change"],
            "T_any_R": any_["T_relative_gap_change"],
            "I_R": interaction["T_relative_gap_change"],
            "T_UCD_A": ucd["T_absolute_gap_change_per_100k"],
            "T_any_A": any_["T_absolute_gap_change_per_100k"],
            "I_A": interaction["T_absolute_gap_change_per_100k"],
            "signed_logRR_UCD_early": ucd["logRR_early"],
            "signed_logRR_UCD_late": ucd["logRR_late"],
            "signed_logRR_any_early": any_["logRR_early"],
            "signed_logRR_any_late": any_["logRR_late"],
            "signed_RD_UCD_early": ucd["RD_early_BR_minus_US_per_100k"],
            "signed_RD_UCD_late": ucd["RD_late_BR_minus_US_per_100k"],
            "signed_RD_any_early": any_["RD_early_BR_minus_US_per_100k"],
            "signed_RD_any_late": any_["RD_late_BR_minus_US_per_100k"],
        }
        for metric, expected_raw in expected.items():
            observed = float(point_values[metric])
            expected_value = float(expected_raw)
            if math.isnan(expected_value) and math.isnan(observed):
                checked += 1
                continue
            if not math.isclose(observed, expected_value, rel_tol=1e-10, abs_tol=1e-12):
                raise MonitoringError(
                    f"Bootstrap point estimate differs from annual rate output for {target_key}/{metric}: "
                    f"{observed} != {expected_value}"
                )
            checked += 1
    return {"status": "PASS", "metrics_checked": checked, "targets_checked": len(BOOTSTRAP_TARGETS)}


def nonregular_and_decisions(interval_rows: list[dict[str, Any]], point_estimates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_seed_metric = {
        (
            int(row["seed"]),
            row["age_scope"],
            row["case_definition"],
            row["code_group"],
            row["metric"],
        ): row
        for row in interval_rows
    }
    decisions: list[dict[str, Any]] = []
    next(
        row for row in point_estimates
        if row.get("age_scope") == "0_74" and row.get("case_definition") == "full"
        and row.get("code_group") == "K80-83_total" and row.get("endpoint") == "DEFINITION_INTERACTION"
    )
    main_target = ("0_74", "full", "K80-83_total")
    get_ci = lambda seed, metric: by_seed_metric[(seed, *main_target, metric)]
    t_names = {"relative": {"UCD": "T_UCD_R", "any_mention": "T_any_R", "INTERACTION": "I_R"},
               "absolute": {"UCD": "T_UCD_A", "any_mention": "T_any_A", "INTERACTION": "I_A"}}
    gap_names = {
        "relative": {
            "UCD": ("signed_logRR_UCD_early", "signed_logRR_UCD_late"),
            "any_mention": ("signed_logRR_any_early", "signed_logRR_any_late"),
        },
        "absolute": {
            "UCD": ("signed_RD_UCD_early", "signed_RD_UCD_late"),
            "any_mention": ("signed_RD_any_early", "signed_RD_any_late"),
        },
    }

    def classification_for(seed: int, metric: str, relevant_gaps: tuple[str, ...]) -> dict[str, Any]:
        ci = get_ci(seed, metric)
        if classify_interval_row(ci) == "NONFINITE_UNRESOLVED":
            return {
                "classification": "NONFINITE_UNRESOLVED",
                "raw_classification": "NONFINITE_UNRESOLVED",
                "nonregular": None,
                "valid": False,
            }
        gap_cis = [get_ci(seed, gap) for gap in relevant_gaps]
        if any(classify_interval_row(gap) == "NONFINITE_UNRESOLVED" for gap in gap_cis):
            return {
                "classification": "NONFINITE_UNRESOLVED",
                "raw_classification": "NONFINITE_UNRESOLVED",
                "nonregular": None,
                "valid": False,
            }
        if metric in {"I_R", "I_A"}:
            raw = classify_interaction_interval(float(ci["family_ci_lower"]), float(ci["family_ci_upper"]))
        else:
            raw = classify_interval(float(ci["family_ci_lower"]), float(ci["family_ci_upper"]))
        nonregular = any(float(gap["family_ci_lower"]) <= 0 <= float(gap["family_ci_upper"]) for gap in gap_cis)
        return {
            "classification": "NONREGULAR_UNRESOLVED" if nonregular else raw,
            "raw_classification": raw,
            "nonregular": nonregular,
            "valid": True,
        }

    for scale, mapping in t_names.items():
        per_seed: dict[int, dict[str, str]] = {}
        details_by_seed: dict[int, dict[str, dict[str, Any]]] = {}
        for seed in BOOTSTRAP_SEEDS:
            classes: dict[str, str] = {}
            details: dict[str, dict[str, Any]] = {}
            for endpoint in ("UCD", "any_mention", "INTERACTION"):
                metric = mapping[endpoint]
                if endpoint == "INTERACTION":
                    relevant = gap_names[scale]["UCD"] + gap_names[scale]["any_mention"]
                else:
                    relevant = gap_names[scale][endpoint]
                details[endpoint] = classification_for(seed, metric, relevant)
                classes[endpoint] = details[endpoint]["classification"]
            per_seed[seed] = classes
            details_by_seed[seed] = details
        for endpoint, metric in mapping.items():
            classes = [per_seed[seed][endpoint] for seed in BOOTSTRAP_SEEDS]
            details = [details_by_seed[seed][endpoint] for seed in BOOTSTRAP_SEEDS]
            stable = all(
                details[0][key] == details[1][key]
                for key in ("classification", "raw_classification", "nonregular", "valid")
            )
            decisions.append(
                {
                    "age_scope": "0_74",
                    "case_definition": "full",
                    "code_group": "K80-83_total",
                    "scale": scale,
                    "metric": metric,
                    "seed_20261023_classification": classes[0],
                    "seed_20261024_classification": classes[1],
                    "seed_20261023_raw_classification": details[0]["raw_classification"],
                    "seed_20261024_raw_classification": details[1]["raw_classification"],
                    "seed_20261023_nonregular": details[0]["nonregular"],
                    "seed_20261024_nonregular": details[1]["nonregular"],
                    "mc_unstable": not stable,
                    "final_classification": classes[0] if stable else "MC_UNSTABLE",
                    "strict_definition_direction_reversal": False,
                    "decision_scope": "primary_only; support intervals are exploratory and are not direction-classified",
                }
            )
        seed_reversals: dict[int, bool] = {}
        for seed in BOOTSTRAP_SEEDS:
            ucd, any_, interaction = (per_seed[seed][endpoint] for endpoint in ("UCD", "any_mention", "INTERACTION"))
            seed_reversals[seed] = (
                ucd in {"GAP_NARROWING", "GAP_WIDENING"}
                and any_ in {"GAP_NARROWING", "GAP_WIDENING"}
                and ucd != any_
                and interaction in {"NEGATIVE_INTERACTION", "POSITIVE_INTERACTION"}
                and int(get_ci(seed, mapping["INTERACTION"])["valid_repetitions"])
                == int(get_ci(seed, mapping["INTERACTION"])["bootstrap_repetitions"])
            )
        related_classes_stable = all(
            all(
                details_by_seed[BOOTSTRAP_SEEDS[0]][endpoint][key]
                == details_by_seed[BOOTSTRAP_SEEDS[1]][endpoint][key]
                for key in ("classification", "raw_classification", "nonregular", "valid")
            )
            for endpoint in ("UCD", "any_mention", "INTERACTION")
        )
        if not related_classes_stable:
            strict_class = "MC_UNSTABLE"
        elif any(
            per_seed[seed][endpoint] == "NONFINITE_UNRESOLVED"
            for seed in BOOTSTRAP_SEEDS
            for endpoint in ("UCD", "any_mention", "INTERACTION")
        ):
            strict_class = "NONFINITE_UNRESOLVED"
        elif any(
            per_seed[seed][endpoint] == "NONREGULAR_UNRESOLVED"
            for seed in BOOTSTRAP_SEEDS
            for endpoint in ("UCD", "any_mention", "INTERACTION")
        ):
            strict_class = "NONREGULAR_UNRESOLVED"
        elif all(seed_reversals.values()):
            strict_class = "STRICT_DIRECTION_REVERSAL"
        else:
            strict_class = "STRICT_REVERSAL_NOT_SUPPORTED"
        decisions.append(
            {
                "age_scope": "0_74",
                "case_definition": "full",
                "code_group": "K80-83_total",
                "scale": scale,
                "metric": "STRICT_DEFINITION_DIRECTION_REVERSAL",
                "seed_20261023_UCD_T_classification": per_seed[BOOTSTRAP_SEEDS[0]]["UCD"],
                "seed_20261023_any_T_classification": per_seed[BOOTSTRAP_SEEDS[0]]["any_mention"],
                "seed_20261023_I_classification": per_seed[BOOTSTRAP_SEEDS[0]]["INTERACTION"],
                "seed_20261023_UCD_T_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[0]]["UCD"]["raw_classification"],
                "seed_20261023_any_T_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[0]]["any_mention"]["raw_classification"],
                "seed_20261023_I_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[0]]["INTERACTION"]["raw_classification"],
                "seed_20261023_UCD_T_nonregular": details_by_seed[BOOTSTRAP_SEEDS[0]]["UCD"]["nonregular"],
                "seed_20261023_any_T_nonregular": details_by_seed[BOOTSTRAP_SEEDS[0]]["any_mention"]["nonregular"],
                "seed_20261023_I_nonregular": details_by_seed[BOOTSTRAP_SEEDS[0]]["INTERACTION"]["nonregular"],
                "seed_20261024_UCD_T_classification": per_seed[BOOTSTRAP_SEEDS[1]]["UCD"],
                "seed_20261024_any_T_classification": per_seed[BOOTSTRAP_SEEDS[1]]["any_mention"],
                "seed_20261024_I_classification": per_seed[BOOTSTRAP_SEEDS[1]]["INTERACTION"],
                "seed_20261024_UCD_T_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[1]]["UCD"]["raw_classification"],
                "seed_20261024_any_T_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[1]]["any_mention"]["raw_classification"],
                "seed_20261024_I_raw_classification": details_by_seed[BOOTSTRAP_SEEDS[1]]["INTERACTION"]["raw_classification"],
                "seed_20261024_UCD_T_nonregular": details_by_seed[BOOTSTRAP_SEEDS[1]]["UCD"]["nonregular"],
                "seed_20261024_any_T_nonregular": details_by_seed[BOOTSTRAP_SEEDS[1]]["any_mention"]["nonregular"],
                "seed_20261024_I_nonregular": details_by_seed[BOOTSTRAP_SEEDS[1]]["INTERACTION"]["nonregular"],
                "strict_definition_direction_reversal": strict_class == "STRICT_DIRECTION_REVERSAL",
                "requires_opposite_endpoint_T_and_I_excluding_zero": True,
                "mc_unstable": strict_class == "MC_UNSTABLE",
                "final_classification": strict_class,
                "decision_scope": "both seeds and all relevant T/I/nonregular classifications must agree",
            }
        )
    return decisions


def build_output_rows(group_counts: Counter, pattern_counts: Counter, cell_counts: Counter) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pattern_out = pattern_rows(pattern_counts)
    group_out: list[dict[str, Any]] = []
    for country in ("US", "BR"):
        for year in YEARS:
            for age in AGE_GROUPS:
                for sex in SEXES:
                    for definition in CASE_DEFINITIONS:
                        for group in ANALYSIS_GROUPS:
                            for endpoint in ENDPOINTS:
                                group_out.append(
                                    {
                                        "country": country,
                                        "year": year,
                                        "age_group": age,
                                        "sex": sex,
                                        "case_definition": definition,
                                        "code_group": group,
                                        "endpoint": endpoint,
                                        "deaths": group_count(group_counts, country, year, age, sex, definition, group, endpoint),
                                    }
                                )
    return pattern_out, group_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--skip-bootstrap", action="store_true", help="run source/QC and point estimates only")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing outputs in the assigned output directory")
    args = parser.parse_args(argv)
    root = Path(args.root)
    script_path = Path(__file__).resolve()
    output_dir = root / "outputs" / "usbr_monitoring"
    script_dir = root / "scripts" / "usbr_monitoring"
    output_dir.mkdir(parents=True, exist_ok=True)
    if not script_dir.exists():
        raise MonitoringError(f"Expected assigned script directory is absent: {script_dir}")
    if not args.overwrite:
        existing = list(output_dir.glob("*.csv")) + list(output_dir.glob("*.json"))
        if existing:
            raise MonitoringError("Output directory already contains results; use --overwrite only for an intentional rerun")

    protocol_path = root / "BRIDGE_PROTOCOL.md"
    if sha256_file(protocol_path) != PROTOCOL_SHA256:
        raise MonitoringError("Frozen protocol SHA mismatch")
    implementation_hashes: dict[str, str] = {}
    note_files = {
        "IMPLEMENTATION_NOTE_001.md": IMPLEMENTATION_NOTE_001_SHA256,
        "IMPLEMENTATION_NOTE_002_CO_SCHEMA.md": IMPLEMENTATION_NOTE_002_SHA256,
        "IMPLEMENTATION_NOTE_003_BOUNDARY_CHECKS.md": IMPLEMENTATION_NOTE_003_SHA256,
    }
    for name, expected_hash in note_files.items():
        note_path = root / name
        if not note_path.is_file():
            raise MonitoringError(f"Required implementation note is absent: {note_path}")
        observed = sha256_file(note_path)
        if observed != expected_hash:
            raise MonitoringError(f"Implementation note hash mismatch for {name}")
        implementation_hashes[name] = observed

    candidates, summaries, manifest_info = expected_candidate_inputs()
    identity = check_input_identity(candidates, summaries, manifest_info)
    population, weights, rows_3744, rows_936, frozen_metadata = load_frozen_inputs()
    population_rows = [
        {
            "country": row["country"],
            "year": int(row["year"]),
            "age_group": row["age_group"],
            "sex": row["sex"],
            "population": int(row["population"]),
        }
        for row in rows_936
    ]
    write_csv(
        output_dir / "usbr_population_denominators.csv",
        ["country", "year", "age_group", "sex", "population"],
        population_rows,
    )
    global EXPECTED_POPULATION_KEYS
    EXPECTED_POPULATION_KEYS = set(population)

    pattern_counts, group_counts, cell_counts, read_checks = read_and_aggregate(candidates, identity_expected_rows(identity))
    p_rows, count_rows = build_output_rows(group_counts, pattern_counts, cell_counts)
    write_csv(
        output_dir / "usbr_joint_patterns.csv",
        list(p_rows[0]),
        p_rows,
    )
    write_csv(output_dir / "usbr_recomputed_group_counts.csv", list(count_rows[0]), count_rows)
    closure = verify_closures(group_counts, cell_counts, population, rows_936, rows_3744, output_dir)
    annual_rates = compute_annual_rates(group_counts, population, weights)
    monitoring_rows = build_monitoring_estimates(annual_rates)
    write_csv(output_dir / "usbr_annual_standardized_rates.csv", list(annual_rates[0]), annual_rates)
    monitoring_fields = list(dict.fromkeys(field for row in monitoring_rows for field in row))
    write_csv(output_dir / "usbr_monitoring_point_estimates.csv", monitoring_fields, monitoring_rows)
    pattern_data = {"rows": p_rows}

    bootstrap_meta: dict[str, Any] = {"status": "SKIPPED", "reason": "--skip-bootstrap"}
    decision_rows: list[dict[str, Any]] = []
    if not args.skip_bootstrap:
        interval_rows, bootstrap_meta = bootstrap_usbr(pattern_data, population, weights)
        bootstrap_meta["point_estimate_crosscheck"] = verify_bootstrap_point_estimates(bootstrap_meta, monitoring_rows)
        decision_rows = nonregular_and_decisions(interval_rows, monitoring_rows)
        write_csv(output_dir / "usbr_primary_bootstrap_intervals.csv", list(interval_rows[0]), interval_rows)
        decision_fields = list(dict.fromkeys(field for row in decision_rows for field in row))
        write_csv(output_dir / "usbr_primary_decisions.csv", decision_fields, decision_rows)

    qc = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "protocol_sha256": PROTOCOL_SHA256,
        "implementation_note_sha256": implementation_hashes,
        "source_scope": "2012-2024 validated retained US and BR candidate gzip streams only; no raw-death rescan",
        "input_identity": identity,
        "frozen_inputs": frozen_metadata,
        "read_checks": read_checks,
        "closure": closure,
        "joint_pattern_schema": {
            "fields": list(p_rows[0]),
            "mask_group_order": list(GROUP_ORDER),
            "mask_rule": "decimal bitmask; bit i is GROUP_ORDER[i]; lower=upper for fully parsed US/BR candidate stream",
            "US_source_any": "UCD plus entity_codes; entity_codes are not replaced by record_codes",
            "BR_source_any": "UCD plus line_codes",
        },
        "standardization": {
            "age_scopes": {"0_74": list(AGE_GROUPS_0_74), "all_age": list(AGE_GROUPS)},
            "original_weights": weights,
            "renormalized_weights_0_74": {age: weights[age] / sum(weights[a] for a in AGE_GROUPS_0_74) for age in AGE_GROUPS_0_74},
            "sex_weight_each": 0.5,
            "year_weights_within_window": "equal",
            "windows": {"early": list(EARLY_YEARS), "late": list(LATE_YEARS)},
            "external_year_rates": list(EXTERNAL_YEARS),
            "rate_unit": "per 100000 population",
            "population_export": {
                "path": str(output_dir / "usbr_population_denominators.csv"),
                "rows": len(population_rows),
                "columns": ["country", "year", "age_group", "sex", "population"],
                "sex_values": ["F", "M"],
                "age_values": list(AGE_GROUPS),
            },
        },
        "bootstrap": bootstrap_meta,
        "limitations": [
            "All outputs are exploratory, not original SAP or prospective validation.",
            "The primary bootstrap represents Poisson event-process model variation with fixed denominators, not population denominator or certification uncertainty.",
            "The 8 M2 support groups are directly checked to the corrected M2 table; K80-83 total is rebuilt as a death-level union and checked against frozen 936 margins.",
            "Rate/count outputs are restricted to the 9 frozen biliary analysis groups; joint-pattern masks retain the full 15-bit protocol interface.",
            "All-age, no-malignancy-source-any, and eight M2 support intervals use the same target-specific joint Poisson draws, are 95% exploratory intervals, and receive no robust-direction classification.",
            "Primary direction classification requires all B bootstrap draws for each relevant T/I and signed gap to be finite; finite-only intervals are descriptive and nonclassifying.",
            "Strict definition-dependent direction reversal is a final two-seed gate and is withheld as MC_UNSTABLE if any relevant T/I or nonregular classification changes between seeds.",
        ],
    }
    write_json(output_dir / "usbr_monitoring_qc.json", qc)
    output_hashes = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "usbr_monitoring_manifest.json":
            output_hashes.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    manifest = {
        "schema": "usbr_monitoring_manifest_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_path": str(script_path),
        "script_sha256": sha256_file(script_path),
        "python_runtime": str(PYTHON_RUNTIME),
        "python_version": sys.version,
        "numpy_version": np.__version__,
        "protocol_sha256": PROTOCOL_SHA256,
        "implementation_note_sha256": implementation_hashes,
        "source_scope": "validated US/BR retained candidate compressed CSVs only; no raw re-scan",
        "output_hashes": output_hashes,
        "bootstrap_status": bootstrap_meta.get("status", "PASS"),
    }
    write_json(output_dir / "usbr_monitoring_manifest.json", manifest)
    print(json.dumps({"status": "PASS", "output_dir": str(output_dir), "closure": closure, "bootstrap": bootstrap_meta.get("status")}, ensure_ascii=False))
    return 0


def identity_expected_rows(identity: dict[str, Any]) -> dict[tuple[str, int], int]:
    result: dict[tuple[str, int], int] = {}
    for key, n in identity["expected_rows_by_country_year"].items():
        country, year_text = key.split("_")
        result[(country, int(year_text))] = int(n)
    return result


EXPECTED_POPULATION_KEYS: set[tuple[str, int, str, str]] = set()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"US/BR monitoring failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
