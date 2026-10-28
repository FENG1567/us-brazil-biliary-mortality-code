#!/usr/bin/env python3
"""Aggregate parser-bound Brazil/US composition features without raw rescans.

This script accepts only completed, hash-validated outputs from the current
country parsers. It streams their compressed de-identified candidate CSVs and
uses the frozen population denominators and WHO-18 age weights for descriptive
direct standardization. It does not make causal, severity, or publication-ready
claims.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "composition"
DEFAULT_BR_DIR = PROJECT_ROOT / "outputs" / "br"
DEFAULT_US_DIR = PROJECT_ROOT / "outputs" / "us"
DEFAULT_IDENTITY = PROJECT_ROOT / "outputs" / "identity" / "source_identity.csv"
DEFAULT_FROZEN_DIR = Path(
    r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build"
)
FROZEN_ANALYSIS = DEFAULT_FROZEN_DIR / "data" / "derived" / "analysis_input_936.csv"
FROZEN_SUBCODE = DEFAULT_FROZEN_DIR / "data" / "derived" / "subcode_analysis_input_3744.csv"
WHO_WEIGHTS = (
    DEFAULT_FROZEN_DIR
    / "data"
    / "metadata"
    / "who_2000_2025_18_group_weights_correct.csv"
)
WHO_BILIARY_REFERENCE = PROJECT_ROOT / "WHO_ICD10_2019_biliary_reference.csv"

COUNTRIES = ("BR", "US")
SEXES = ("F", "M")
PREFIXES = ("K80", "K81", "K82", "K83")
PATTERN_CATEGORIES = tuple(
    "|".join(combo)
    for size in range(1, len(PREFIXES) + 1)
    for combo in itertools.combinations(PREFIXES, size)
)
POSITION_GROUPS = (
    "only_part_i",
    "only_part_ii",
    "both_part_i_ii",
    "neither_captured",
)
CHAPTERS = (
    ("I", "Certain infectious and parasitic diseases"),
    ("II", "Neoplasms"),
    ("III", "Diseases of the blood and blood-forming organs and certain disorders involving the immune mechanism"),
    ("IV", "Endocrine, nutritional and metabolic diseases"),
    ("V", "Mental and behavioural disorders"),
    ("VI", "Diseases of the nervous system"),
    ("VII", "Diseases of the eye and adnexa"),
    ("VIII", "Diseases of the ear and mastoid process"),
    ("IX", "Diseases of the circulatory system"),
    ("X", "Diseases of the respiratory system"),
    ("XI", "Diseases of the digestive system"),
    ("XII", "Diseases of the skin and subcutaneous tissue"),
    ("XIII", "Diseases of the musculoskeletal system and connective tissue"),
    ("XIV", "Diseases of the genitourinary system"),
    ("XV", "Pregnancy, childbirth and the puerperium"),
    ("XVI", "Certain conditions originating in the perinatal period"),
    ("XVII", "Congenital malformations, deformations and chromosomal abnormalities"),
    ("XVIII", "Symptoms, signs and abnormal clinical and laboratory findings, not elsewhere classified"),
    ("XIX", "Injury, poisoning and certain other consequences of external causes"),
    ("XX", "External causes of morbidity and mortality"),
    ("XXI", "Factors influencing health status and contact with health services"),
    ("XXII", "Codes for special purposes"),
)
CHAPTER_TITLES = dict(CHAPTERS)
CHAPTER_CATEGORIES = tuple(code for code, _ in CHAPTERS) + ("UNKNOWN",)
CHAPTER_TITLE_BY_CODE = {**CHAPTER_TITLES, "UNKNOWN": "Unknown or non-standard underlying-cause code"}
EXTRA_DIMENSIONS = {
    "source_prefix_set": PATTERN_CATEGORIES,
    "position_group": POSITION_GROUPS,
    "ucd_chapter": CHAPTER_CATEGORIES,
}
ANY_DIMENSIONS = {
    "source_prefix_set": PATTERN_CATEGORIES,
    "position_group": POSITION_GROUPS,
}
CODE_PREFIX_RE = re.compile(r"^K8[0-3]")
# A normalized ICD-10 token is a three-character category with an optional
# numeric subcategory and up to three further extension characters. Requiring
# the whole token to match keeps arbitrary suffix text out of chapter totals.
UC_CODE_RE = re.compile(r"^([A-Z])(\d{2})(?:\d[A-Z0-9]{0,3})?$")
RATE_SCALE = 100_000.0
PERCENT_DENOMINATOR_EPSILON = 1e-9
RATE_IDENTITY_TOLERANCE = 1e-9


class CompositionError(RuntimeError):
    """A validation failure that prevents a composition completion marker."""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_years(value: str) -> list[int]:
    years: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            bits = token.split("-")
            if len(bits) != 2:
                raise argparse.ArgumentTypeError(f"Invalid year range: {token}")
            try:
                first, last = (int(bit) for bit in bits)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid year range: {token}") from exc
            if first > last:
                raise argparse.ArgumentTypeError(f"Reversed year range: {token}")
            years.extend(range(first, last + 1))
        else:
            try:
                years.append(int(token))
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid year: {token}") from exc
    if not years or any(year < 2012 or year > 2024 for year in years):
        raise argparse.ArgumentTypeError("Years must be selected from 2012 through 2024")
    if len(years) != len(set(years)):
        raise argparse.ArgumentTypeError("Duplicate years are not allowed")
    return sorted(years)


def run_stem(years: list[int]) -> str:
    window = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
    return f"BR_US_{window}"


def normalize_code(raw_code: Any) -> str:
    if raw_code is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw_code).upper())


def parse_flag(value: Any, field: str) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "t", "yes"}:
        return True
    if text in {"0", "false", "f", "no"}:
        return False
    raise CompositionError(f"Invalid boolean flag in {field}: {value!r}")


def parse_json_list(value: Any, field: str) -> list[Any]:
    if value is None or str(value).strip() == "":
        return []
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise CompositionError(f"Invalid JSON in {field}") from exc
    if not isinstance(decoded, list):
        raise CompositionError(f"Expected a JSON list in {field}")
    return decoded


def classify_primary(any_common: bool, ucd_biliary: bool) -> str:
    if ucd_biliary:
        if not any_common:
            raise CompositionError("UCD-biliary record is not marked any_common")
        return "UCD_BILIARY"
    return "EXTRA" if any_common else "OUTSIDE_PRIMARY"


def extract_any_codes(row: dict[str, Any], country: str) -> set[str]:
    """Return distinct biliary source-ANY codes (UCD union entity/line)."""
    codes: set[str] = set()
    ucd_biliary = parse_flag(row.get("ucd_biliary"), "ucd_biliary")
    ucd_code = normalize_code(row.get("ucd_code"))
    if ucd_biliary:
        if len(ucd_code) < 3 or ucd_code[:3] not in PREFIXES:
            raise CompositionError(f"UCD-biliary flag/code mismatch: {ucd_code!r}")
        codes.add(ucd_code)

    if country == "US":
        source_codes = parse_json_list(row.get("entity_biliary_codes"), "entity_biliary_codes")
        for raw_code in source_codes:
            code = normalize_code(raw_code)
            if len(code) < 3 or code[:3] not in PREFIXES:
                raise CompositionError(f"Non-biliary code in entity_biliary_codes: {raw_code!r}")
            codes.add(code)
    elif country == "BR":
        source_codes = parse_json_list(row.get("line_biliary_codes"), "line_biliary_codes")
        for pair in source_codes:
            if not isinstance(pair, dict) or "code" not in pair:
                raise CompositionError("BR line_biliary_codes must contain {field, code} pairs")
            code = normalize_code(pair["code"])
            if len(code) < 3 or code[:3] not in PREFIXES:
                raise CompositionError(f"Non-biliary code in line_biliary_codes: {pair!r}")
            codes.add(code)
    else:
        raise CompositionError(f"Unsupported country: {country}")
    return codes


def prefix_pattern(codes: Iterable[str]) -> str:
    prefixes = {code[:3] for code in codes if len(code) >= 3 and code[:3] in PREFIXES}
    if not prefixes:
        raise CompositionError("Primary any_common record has no captured source prefix")
    return "|".join(prefix for prefix in PREFIXES if prefix in prefixes)


def position_group(part_i: bool, part_ii: bool) -> str:
    if part_i and part_ii:
        return "both_part_i_ii"
    if part_i:
        return "only_part_i"
    if part_ii:
        return "only_part_ii"
    return "neither_captured"


def extract_position_group(row: dict[str, Any], country: str) -> str:
    if country == "US":
        part_i = bool(parse_json_list(row.get("part_i_biliary"), "part_i_biliary"))
        part_ii = bool(parse_json_list(row.get("part_ii_biliary"), "part_ii_biliary"))
    elif country == "BR":
        part_i = parse_flag(row.get("part_i_biliary"), "part_i_biliary")
        part_ii = parse_flag(row.get("part_ii_biliary"), "part_ii_biliary")
    else:
        raise CompositionError(f"Unsupported country: {country}")
    return position_group(part_i, part_ii)


def ucd_chapter(raw_code: Any) -> str:
    """Map the normalized ICD-10 underlying cause to its WHO chapter."""
    code = normalize_code(raw_code)
    match = UC_CODE_RE.match(code)
    if not match:
        return "UNKNOWN"
    letter, number_text = match.groups()
    number = int(number_text)
    if letter in {"A", "B"}:
        return "I"
    if letter == "C":
        return "II" if number <= 97 else "UNKNOWN"
    if letter == "D":
        if number <= 48:
            return "II"
        if 50 <= number <= 89:
            return "III"
        return "UNKNOWN"
    direct = {
        "E": ("IV", 90),
        "F": ("V", 99),
        "G": ("VI", 99),
        "I": ("IX", 99),
        "J": ("X", 99),
        "L": ("XII", 99),
        "M": ("XIII", 99),
        "N": ("XIV", 99),
        "O": ("XV", 99),
        "Q": ("XVII", 99),
        "R": ("XVIII", 99),
        "U": ("XXII", 99),
        "Z": ("XXI", 99),
    }
    if letter in direct:
        chapter, maximum = direct[letter]
        minimum = 0 if letter != "U" else 0
        return chapter if minimum <= number <= maximum else "UNKNOWN"
    if letter == "H":
        if number <= 59:
            return "VII"
        if number <= 95:
            return "VIII"
        return "UNKNOWN"
    if letter == "K":
        return "XI" if number <= 93 else "UNKNOWN"
    if letter == "P":
        return "XVI" if number <= 96 else "UNKNOWN"
    if letter in {"S", "T"}:
        return "XIX" if letter == "S" or number <= 98 else "UNKNOWN"
    if letter in {"V", "W", "X", "Y"}:
        if letter == "V" and number == 0:
            return "UNKNOWN"
        if letter == "Y" and number > 98:
            return "UNKNOWN"
        return "XX"
    return "UNKNOWN"


def code_reference_status(code: str, who_reference: dict[str, dict[str, str]]) -> dict[str, str]:
    normalized = normalize_code(code)
    if len(normalized) == 3 and normalized[:3] in PREFIXES:
        return {
            "normalized_code": normalized,
            "code4": "",
            "code_class": "three_character_component",
            "reference_status": "not_a_terminal_four_character_code",
            "reference_title": "",
        }
    if len(normalized) == 4 and normalized[:3] in PREFIXES:
        reference = who_reference.get(normalized)
        if reference:
            return {
                "normalized_code": normalized,
                "code4": normalized,
                "code_class": "four_character_reference_match",
                "reference_status": "matched_WHO_2019_reference",
                "reference_title": reference["title"],
            }
        return {
            "normalized_code": normalized,
            "code4": normalized,
            "code_class": "four_character_unmatched",
            "reference_status": "unmatched_WHO_2019_reference",
            "reference_title": "",
        }
    if len(normalized) > 4 and normalized[:3] in PREFIXES:
        return {
            "normalized_code": normalized,
            "code4": normalized[:4],
            "code_class": "longer_than_four_characters_preserved",
            "reference_status": "not_exactly_a_reference_terminal_code",
            "reference_title": "",
        }
    return {
        "normalized_code": normalized,
        "code4": "",
        "code_class": "short_or_nonstandard_preserved",
        "reference_status": "not_matched",
        "reference_title": "",
    }


def load_who_reference(path: Path) -> dict[str, dict[str, str]]:
    reference: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"category", "code", "title", "reference_version"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise CompositionError("WHO biliary reference lacks required columns")
        for row in reader:
            code = normalize_code(row.get("code"))
            category = (row.get("category") or "").strip().upper()
            if len(code) != 4 or code[:3] != category or category not in PREFIXES:
                raise CompositionError(f"Invalid code/category in WHO reference: {row}")
            if code in reference:
                raise CompositionError(f"Duplicate WHO reference code: {code}")
            reference[code] = {
                "category": category,
                "title": (row.get("title") or "").strip(),
                "reference_version": (row.get("reference_version") or "").strip(),
            }
    if len(reference) != 26:
        raise CompositionError(f"Expected 26 WHO 2019 biliary terminal codes, found {len(reference)}")
    return reference


def load_age_weights(path: Path) -> tuple[dict[str, float], tuple[str, ...]]:
    raw: dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not {"age_group", "weight"}.issubset(reader.fieldnames):
            raise CompositionError("WHO-18 weight file lacks age_group/weight columns")
        for row in reader:
            age = (row.get("age_group") or "").strip()
            try:
                weight = float(row.get("weight", ""))
            except ValueError as exc:
                raise CompositionError(f"Invalid WHO-18 weight for {age!r}") from exc
            if not age or age in raw or weight <= 0:
                raise CompositionError(f"Invalid or duplicated WHO-18 age weight: {age!r}")
            raw[age] = weight
    if len(raw) != 18 or abs(sum(raw.values()) - 1.0) > 1e-10:
        raise CompositionError("WHO-18 weights must contain 18 age groups and sum to one")
    ages = tuple(raw)
    normalized = {age: value / sum(raw.values()) for age, value in raw.items()}
    return normalized, ages


def load_source_identity(path: Path, years: list[int]) -> dict[tuple[str, int], dict[str, str]]:
    wanted = set(years)
    identity: dict[tuple[str, int], dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"country", "year", "expected_sha256", "actual_sha256", "status"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise CompositionError("source_identity.csv lacks required columns")
        for row in reader:
            country = (row.get("country") or "").strip().upper()
            try:
                year = int(row.get("year", ""))
            except ValueError:
                continue
            if country not in COUNTRIES or year not in wanted:
                continue
            key = (country, year)
            if key in identity:
                raise CompositionError(f"Duplicate source identity row: {key}")
            if (row.get("status") or "").strip().upper() != "MATCH":
                raise CompositionError(f"Raw source identity is not MATCH for {key}")
            expected = (row.get("expected_sha256") or "").strip().lower()
            actual = (row.get("actual_sha256") or "").strip().lower()
            if not expected or expected != actual:
                raise CompositionError(f"Raw source identity SHA mismatch for {key}")
            identity[key] = {column: (row.get(column) or "").strip() for column in reader.fieldnames}
    missing = [(country, year) for country in COUNTRIES for year in years if (country, year) not in identity]
    if missing:
        raise CompositionError(f"Missing source identity rows: {missing}")
    return identity


def safe_output(path: Path, expected_dir: Path) -> Path:
    resolved_path = path.resolve()
    resolved_dir = expected_dir.resolve()
    if resolved_path.parent != resolved_dir:
        raise CompositionError(f"Parser output is outside its expected directory: {path}")
    return resolved_path


def validate_hash_manifest(summary_path: Path, output_hashes: dict[str, str]) -> dict[str, Path]:
    if not output_hashes:
        raise CompositionError(f"No output hash manifest in {summary_path.name}")
    paths: dict[str, Path] = {}
    for name, expected_hash in output_hashes.items():
        if Path(name).name != name:
            raise CompositionError(f"Unsafe output name in parser summary: {name!r}")
        path = summary_path.parent / name
        if not path.is_file():
            raise CompositionError(f"Parser output is missing: {path}")
        observed_hash = sha256_file(path)
        if observed_hash.lower() != str(expected_hash).lower():
            raise CompositionError(f"Parser output hash mismatch: {path.name}")
        paths[name] = path
    return paths


def validate_empty_csv(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = sum(1 for _ in reader)
    if rows != 0:
        raise CompositionError(f"Parser mismatch CSV is not empty: {path.name} has {rows} data rows")


def validate_br_run(
    year: int,
    br_dir: Path,
    identity_row: dict[str, str],
    parser_sha: str,
    frozen_analysis_sha: str,
    frozen_subcode_sha: str,
) -> dict[str, Any]:
    summary_path = br_dir / f"summary_BR_{year}.json"
    marker_path = br_dir / f"COMPLETE_BR_{year}.txt"
    if not summary_path.is_file() or not marker_path.is_file():
        raise CompositionError(f"Completed BR parser outputs are missing for {year}")
    summary_hash = sha256_file(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "complete" or summary.get("gate_passed") is not True:
        raise CompositionError(f"BR parser gate is not complete/pass for {year}")
    if summary.get("years") != [year] or summary.get("country") != "BR":
        raise CompositionError(f"BR parser summary year/country mismatch for {year}")
    if summary.get("parser_sha256") != parser_sha:
        raise CompositionError(f"BR parser summary is not bound to current parser for {year}")
    if summary.get("mismatch_count") != 0 or summary.get("processing_error") is not None:
        raise CompositionError(f"BR parser mismatch/error for {year}")
    raw_sha = summary.get("source_identity", {}).get("raw_sha256_by_year", {}).get(str(year), "")
    ledger_audit = summary.get("source_identity", {}).get("raw_ledger_audit", {}).get(str(year), {})
    if raw_sha.lower() != identity_row["actual_sha256"].lower() or not ledger_audit.get("raw_sha256_match"):
        raise CompositionError(f"BR parser raw source identity is not validated for {year}")
    if summary.get("source_identity", {}).get("frozen_analysis_sha256") != frozen_analysis_sha:
        raise CompositionError(f"BR parser used a different frozen 936 input for {year}")
    if summary.get("source_identity", {}).get("frozen_subcode_sha256") != frozen_subcode_sha:
        raise CompositionError(f"BR parser used a different frozen subcode input for {year}")
    marker = marker_path.read_text(encoding="utf-8")
    if f"gate=PASS" not in marker or f"run_key={summary.get('run_key')}" not in marker:
        raise CompositionError(f"BR completion marker does not match summary for {year}")
    if f"summary_sha256={summary_hash}" not in marker:
        raise CompositionError(f"BR completion marker summary hash mismatch for {year}")

    expected_names = {
        "candidate": f"candidate_records_BR_{year}.csv.gz",
        "strata": f"strata_counts_BR_{year}.csv",
        "components": f"component_counts_BR_{year}.csv",
        "mismatches": f"mismatches_BR_{year}.csv",
    }
    artifact_hashes = summary.get("artifact_sha256", {})
    outputs: dict[str, Path] = {}
    for key, name in expected_names.items():
        if artifact_hashes.get(name) is None:
            raise CompositionError(f"BR parser omitted the expected artifact hash: {name}")
        path = safe_output(br_dir / name, br_dir)
        if not path.is_file() or sha256_file(path).lower() != str(artifact_hashes[name]).lower():
            raise CompositionError(f"BR parser artifact hash mismatch: {name}")
        outputs[key] = path
    validate_empty_csv(outputs["mismatches"])
    return {
        "country": "BR",
        "year": year,
        "summary_path": summary_path,
        "summary_sha256": summary_hash,
        "summary": summary,
        "outputs": outputs,
        "parser_sha256": parser_sha,
        "source_sha256": raw_sha,
    }


def validate_us_run(
    year: int,
    us_dir: Path,
    identity_row: dict[str, str],
    parser_sha: str,
    frozen_936_sha: str,
    frozen_3744_sha: str,
) -> dict[str, Any]:
    source_sha = identity_row["actual_sha256"].lower()
    stem = f"us_{year}_{source_sha[:12]}_{parser_sha[:12]}"
    summary_path = us_dir / f"{stem}_summary.json"
    if not summary_path.is_file():
        raise CompositionError(
            f"Current-parser US summary missing at exact input/parser path for {year}: {summary_path.name}"
        )
    summary_hash = sha256_file(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "complete" or summary.get("exact_frozen_gate_pass") is not True:
        raise CompositionError(f"US parser gate is not complete/pass for {year}")
    if summary.get("country") != "US" or int(summary.get("year", -1)) != year:
        raise CompositionError(f"US parser summary year/country mismatch for {year}")
    if summary.get("parser_sha256") != parser_sha:
        raise CompositionError(f"US parser summary is not bound to current parser for {year}")
    if summary.get("source_sha256", "").lower() != source_sha:
        raise CompositionError(f"US parser raw source SHA differs from identity ledger for {year}")
    if summary.get("zip_crc_verified") is not True or summary.get("member_eof_reached") is not True:
        raise CompositionError(f"US parser did not validate the source ZIP through EOF for {year}")
    if summary.get("frozen_mismatches") != 0:
        raise CompositionError(f"US parser frozen mismatch count is nonzero for {year}")
    input_manifest = summary.get("input_manifest", {})
    if input_manifest.get("frozen_936_sha256") != frozen_936_sha:
        raise CompositionError(f"US parser used a different frozen 936 input for {year}")
    if input_manifest.get("frozen_3744_sha256") != frozen_3744_sha:
        raise CompositionError(f"US parser used a different frozen subcode input for {year}")

    paths = validate_hash_manifest(summary_path, summary.get("outputs", {}))
    retained_name = f"{stem}_retained.csv.gz"
    counts_name = f"{stem}_counts.csv"
    mismatch_name = f"{stem}_mismatch.csv"
    if not {retained_name, counts_name, mismatch_name}.issubset(paths):
        raise CompositionError(f"US current-parser summary lacks required output files for {year}")
    if len([name for name in paths if name.endswith("_retained.csv.gz")]) != 1:
        raise CompositionError(f"US summary does not identify exactly one retained file for {year}")
    validate_empty_csv(paths[mismatch_name])
    return {
        "country": "US",
        "year": year,
        "summary_path": summary_path,
        "summary_sha256": summary_hash,
        "summary": summary,
        "outputs": {
            "candidate": safe_output(paths[retained_name], us_dir),
            "strata": safe_output(paths[counts_name], us_dir),
            "mismatches": safe_output(paths[mismatch_name], us_dir),
        },
        "parser_sha256": parser_sha,
        "source_sha256": source_sha,
    }


def load_frozen_rows(
    path: Path, years: list[int], age_groups: tuple[str, ...]
) -> dict[tuple[str, int, str, str], dict[str, int]]:
    wanted = set(years)
    rows: dict[tuple[str, int, str, str], dict[str, int]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"country", "year", "age_group", "sex", "population", "ucd", "any_common", "alternative"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise CompositionError("Frozen analysis_input_936.csv lacks required columns")
        for row in reader:
            country = (row.get("country") or "").strip().upper()
            try:
                year = int(row.get("year", ""))
            except ValueError:
                continue
            age = (row.get("age_group") or "").strip()
            sex = (row.get("sex") or "").strip().upper()
            if country not in COUNTRIES or year not in wanted:
                continue
            if age not in age_groups or sex not in SEXES:
                raise CompositionError(f"Unexpected frozen stratum: {(country, year, age, sex)}")
            key = (country, year, age, sex)
            if key in rows:
                raise CompositionError(f"Duplicate frozen 936 cell: {key}")
            try:
                cell = {
                    field: int(row[field])
                    for field in ("population", "ucd", "any_common", "alternative")
                }
            except (ValueError, KeyError) as exc:
                raise CompositionError(f"Invalid frozen count/population for {key}") from exc
            if cell["population"] <= 0 or min(cell["ucd"], cell["any_common"], cell["alternative"]) < 0:
                raise CompositionError(f"Nonpositive population or negative count for {key}")
            rows[key] = cell
    expected = len(COUNTRIES) * len(years) * len(age_groups) * len(SEXES)
    if len(rows) != expected:
        raise CompositionError(f"Expected {expected} selected frozen cells, found {len(rows)}")
    return rows


def load_parser_counts(run: dict[str, Any], age_groups: tuple[str, ...]) -> dict[tuple[str, int, str, str], dict[str, int]]:
    country = run["country"]
    year = run["year"]
    counts: dict[tuple[str, int, str, str], dict[str, int]] = {}
    with run["outputs"]["strata"].open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"country", "year", "age_group", "sex", "ucd", "any_common", "alternative"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise CompositionError(f"Parser count table schema invalid: {run['outputs']['strata'].name}")
        for row in reader:
            row_country = (row.get("country") or "").strip().upper()
            try:
                row_year = int(row.get("year", ""))
            except ValueError as exc:
                raise CompositionError("Parser count table has a nonnumeric year") from exc
            age = (row.get("age_group") or "").strip()
            sex = (row.get("sex") or "").strip().upper()
            if row_country != country or row_year != year:
                raise CompositionError("Parser count table contains a different country/year")
            if age not in age_groups or sex not in SEXES:
                raise CompositionError(f"Unexpected parser count stratum: {(country, year, age, sex)}")
            key = (country, year, age, sex)
            if key in counts:
                raise CompositionError(f"Duplicate parser count cell: {key}")
            counts[key] = {field: int(row[field]) for field in ("ucd", "any_common", "alternative")}
    expected = len(age_groups) * len(SEXES)
    if len(counts) != expected:
        raise CompositionError(f"Expected {expected} parser strata for {country}/{year}, found {len(counts)}")
    return counts


def reader_for_gzip_csv(path: Path) -> tuple[Any, csv.DictReader]:
    stream = gzip.open(path, "rt", encoding="utf-8", newline="")
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        stream.close()
        raise CompositionError(f"Compressed parser output has no header: {path.name}")
    return stream, reader


def code_status_summary(code: str, who_reference: dict[str, dict[str, str]]) -> dict[str, str]:
    return code_reference_status(code, who_reference)


class CompositionAccumulator:
    """Fixed-size aggregate state; candidate records are never retained."""

    def __init__(self, years: list[int], age_groups: tuple[str, ...], who_reference: dict[str, dict[str, str]]):
        self.years = years
        self.age_groups = age_groups
        self.who_reference = who_reference
        self.candidate_counts: Counter[tuple[str, int, str, str, str]] = Counter()
        self.any_prefix_counts: Counter[tuple[str, int, str, str, str]] = Counter()
        self.any_position_counts: Counter[tuple[str, int, str, str, str]] = Counter()
        self.extra_category_counts: Counter[tuple[str, int, str, str, str, str]] = Counter()
        self.source_code_counts: Counter[tuple[str, int, str, str, str]] = Counter()
        self.k82_code_counts: Counter[tuple[str, int, str]] = Counter()
        self.k82_any_counts: Counter[tuple[str, int]] = Counter()
        self.extra_ucd_counts: Counter[tuple[str, int, str, str, str]] = Counter()
        self.input_stats: dict[tuple[str, int], dict[str, int]] = {}
        self.code_class_counts: Counter[tuple[str, int, str]] = Counter()
        self.reference_title_by_code = {
            code: value["title"] for code, value in who_reference.items()
        }

    def add_row(self, row: dict[str, Any], country: str, year: int) -> None:
        try:
            row_year = int(row.get("year", ""))
            source_row = int(row.get("source_row", ""))
        except ValueError as exc:
            raise CompositionError(f"Nonnumeric source year/source_row in {country}/{year}") from exc
        if (row.get("country") or "").strip().upper() != country or row_year != year:
            raise CompositionError(f"Candidate country/year mismatch for {country}/{year}")
        age = (row.get("age_group") or "").strip()
        sex = (row.get("sex") or "").strip().upper()
        if age not in self.age_groups or sex not in SEXES or source_row < 1:
            raise CompositionError(f"Invalid candidate stratum/source row for {country}/{year}")

        ucd_biliary = parse_flag(row.get("ucd_biliary"), "ucd_biliary")
        any_common = parse_flag(row.get("any_common"), "any_common")
        alternative = parse_flag(row.get("alternative"), "alternative")
        cohort = classify_primary(any_common, ucd_biliary)
        cell = (country, year, age, sex)
        self.candidate_counts[cell + ("retained_rows",)] += 1
        self.candidate_counts[cell + ("ucd",)] += int(ucd_biliary)
        self.candidate_counts[cell + ("any_common",)] += int(any_common)
        self.candidate_counts[cell + ("alternative",)] += int(alternative)
        if cohort == "OUTSIDE_PRIMARY":
            if not alternative:
                raise CompositionError("Retained parser row is not UCD, any-common, or alternative")
            self.candidate_counts[cell + ("alternative_only",)] += 1
            return

        codes = extract_any_codes(row, country)
        if not codes:
            raise CompositionError(f"Primary any-common record has no source-ANY codes at row {source_row}")
        pattern = prefix_pattern(codes)
        pos_group = extract_position_group(row, country)
        self.any_prefix_counts[cell + (pattern,)] += 1
        self.any_position_counts[cell + (pos_group,)] += 1
        for code in codes:
            status = code_status_summary(code, self.who_reference)
            self.source_code_counts[cell + (code,)] += 1
            self.code_class_counts[(country, year, status["code_class"])] += 1
        k82_codes = {code for code in codes if code.startswith("K82")}
        if k82_codes:
            self.k82_any_counts[(country, year)] += 1
            for code in k82_codes:
                self.k82_code_counts[(country, year, code)] += 1

        if cohort == "EXTRA":
            chapter = ucd_chapter(row.get("ucd_code"))
            categories = {
                "source_prefix_set": pattern,
                "position_group": pos_group,
                "ucd_chapter": chapter,
            }
            for dimension, category in categories.items():
                if category not in EXTRA_DIMENSIONS[dimension]:
                    raise CompositionError(f"Unexpected extra category: {dimension}={category}")
                self.extra_category_counts[cell + (dimension, category)] += 1
            ucd_code = normalize_code(row.get("ucd_code")) or "UNKNOWN"
            self.extra_ucd_counts[cell + (ucd_code,)] += 1


def load_candidates(
    run: dict[str, Any],
    accumulator: CompositionAccumulator,
    parser_counts: dict[tuple[str, int, str, str], dict[str, int]],
) -> dict[str, Any]:
    country = run["country"]
    year = run["year"]
    path: Path = run["outputs"]["candidate"]
    stream, reader = reader_for_gzip_csv(path)
    if country == "BR":
        required = {
            "country", "year", "source_row", "age_group", "sex", "ucd_code",
            "ucd_biliary", "any_common", "alternative", "part_i_biliary",
            "part_ii_biliary", "line_biliary_codes",
        }
        expected_rows = run["summary"]["source_audit_by_year"][str(year)]["candidate_records"]
    else:
        required = {
            "country", "year", "source_row", "age_group", "sex", "ucd_code",
            "ucd_biliary", "any_common", "alternative", "part_i_biliary",
            "part_ii_biliary", "entity_biliary_codes", "record_biliary_codes",
        }
        expected_rows = run["summary"]["retained_biliary_records"]
    missing = required - set(reader.fieldnames or [])
    if missing:
        stream.close()
        raise CompositionError(f"Candidate schema missing fields for {country}/{year}: {sorted(missing)}")

    row_count = 0
    previous_source_row = 0
    primary_rows = 0
    extra_rows = 0
    alt_only_rows = 0
    try:
        for row in reader:
            row_count += 1
            try:
                source_row = int(row.get("source_row", ""))
            except ValueError as exc:
                raise CompositionError(f"Invalid source_row in {country}/{year}") from exc
            if source_row <= previous_source_row:
                raise CompositionError(f"Candidate source_row is not strictly increasing: {country}/{year}")
            previous_source_row = source_row
            accumulator.add_row(row, country, year)
            any_common = parse_flag(row.get("any_common"), "any_common")
            ucd_biliary = parse_flag(row.get("ucd_biliary"), "ucd_biliary")
            alternative = parse_flag(row.get("alternative"), "alternative")
            if any_common:
                primary_rows += 1
                if not ucd_biliary:
                    extra_rows += 1
            elif alternative:
                alt_only_rows += 1
    finally:
        stream.close()
    if row_count != int(expected_rows):
        raise CompositionError(
            f"Candidate row count differs from parser summary for {country}/{year}: "
            f"{row_count} != {expected_rows}"
        )

    for key, expected in parser_counts.items():
        _, _, age, sex = key
        cell = (country, year, age, sex)
        observed = {
            field: accumulator.candidate_counts[cell + (field,)]
            for field in ("ucd", "any_common", "alternative")
        }
        for field in ("ucd", "any_common", "alternative"):
            if observed[field] != expected[field]:
                raise CompositionError(
                    f"Candidate-vs-count table mismatch at {cell}/{field}: "
                    f"{observed[field]} != {expected[field]}"
                )
        if expected["any_common"] < expected["ucd"]:
            raise CompositionError(f"Frozen any count is below UCD count at {cell}")
        observed_extra = accumulator.candidate_counts[cell + ("any_common",)] - accumulator.candidate_counts[cell + ("ucd",)]
        expected_extra = expected["any_common"] - expected["ucd"]
        if observed_extra != expected_extra:
            raise CompositionError(f"EXTRA count is not any_common minus UCD at {cell}")

    return {
        "candidate_rows": row_count,
        "primary_any_rows": primary_rows,
        "extra_rows": extra_rows,
        "us_alternative_only_rows": alt_only_rows if country == "US" else 0,
        "candidate_sha256": sha256_file(path),
    }


def load_parser_rows(
    runs: dict[tuple[str, int], dict[str, Any]],
    frozen_rows: dict[tuple[str, int, str, str], dict[str, int]],
    age_groups: tuple[str, ...],
) -> tuple[dict[tuple[str, int, str, str], dict[str, int]], dict[tuple[str, int], dict[str, int]]]:
    parser_counts: dict[tuple[str, int, str, str], dict[str, int]] = {}
    populations: dict[tuple[str, int, str, str], dict[str, int]] = {}
    for (country, year), run in sorted(runs.items()):
        current = load_parser_counts(run, age_groups)
        for key, values in current.items():
            if key not in frozen_rows:
                raise CompositionError(f"Parser cell absent from frozen 936 table: {key}")
            frozen = frozen_rows[key]
            for field in ("ucd", "any_common", "alternative"):
                if values[field] != frozen[field]:
                    raise CompositionError(f"Parser table differs from frozen 936 at {key}/{field}")
            parser_counts[key] = values
            populations[key] = {"population": frozen["population"]}
    if len(parser_counts) != len(frozen_rows):
        raise CompositionError("Parser count tables do not cover every selected frozen cell")
    return parser_counts, populations


def count_or_zero(counter: Counter, key: tuple[Any, ...]) -> int:
    return int(counter.get(key, 0))


def make_count_rows(
    years: list[int],
    age_groups: tuple[str, ...],
    parser_counts: dict[tuple[str, int, str, str], dict[str, int]],
    populations: dict[tuple[str, int, str, str], dict[str, int]],
    accumulator: CompositionAccumulator,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    record_rows: list[dict[str, Any]] = []
    any_rows: list[dict[str, Any]] = []
    extra_rows: list[dict[str, Any]] = []
    for country in COUNTRIES:
        for year in years:
            for age in age_groups:
                for sex in SEXES:
                    cell = (country, year, age, sex)
                    counts = parser_counts[cell]
                    extra = counts["any_common"] - counts["ucd"]
                    if extra < 0:
                        raise CompositionError(f"Negative EXTRA count at {cell}")
                    record_rows.append(
                        {
                            "country": country,
                            "year": year,
                            "age_group": age,
                            "sex": sex,
                            "population": populations[cell]["population"],
                            "ucd_count": counts["ucd"],
                            "any_common_count": counts["any_common"],
                            "alternative_count": counts["alternative"],
                            "extra_count": extra,
                            "us_alternative_only_count": count_or_zero(
                                accumulator.candidate_counts, cell + ("alternative_only",)
                            ),
                        }
                    )
                    for dimension, categories in ANY_DIMENSIONS.items():
                        for category in categories:
                            counter = (
                                accumulator.any_prefix_counts
                                if dimension == "source_prefix_set"
                                else accumulator.any_position_counts
                            )
                            any_rows.append(
                                {
                                    "country": country,
                                    "year": year,
                                    "age_group": age,
                                    "sex": sex,
                                    "dimension": dimension,
                                    "category": category,
                                    "any_primary_records": count_or_zero(
                                        counter, cell + (category,)
                                    ),
                                    "exclusive_partition": "true",
                                    "aggregation_scope": "ANY_PRIMARY_any_common_equals_1",
                                }
                            )
                    for dimension, categories in EXTRA_DIMENSIONS.items():
                        for category in categories:
                            extra_rows.append(
                                {
                                    "country": country,
                                    "year": year,
                                    "age_group": age,
                                    "sex": sex,
                                    "dimension": dimension,
                                    "category": category,
                                    "category_label": CHAPTER_TITLE_BY_CODE.get(category, category),
                                    "extra_records": count_or_zero(
                                        accumulator.extra_category_counts,
                                        cell + (dimension, category),
                                    ),
                                    "exclusive_partition": "true",
                                    "aggregation_scope": "EXTRA_any_common_1_ucd_biliary_0",
                                }
                            )
    return record_rows, any_rows, extra_rows


def direct_rate(
    count_for_cell: Any,
    country: str,
    years: list[int],
    age_groups: tuple[str, ...],
    age_weights: dict[str, float],
    populations: dict[tuple[str, int, str, str], dict[str, int]],
    *,
    year_equal: bool = True,
) -> float:
    year_weight = 1.0 / len(years) if year_equal else 1.0
    rate = 0.0
    for year in years:
        for age in age_groups:
            for sex in SEXES:
                cell = (country, year, age, sex)
                population = populations[cell]["population"]
                count = float(count_for_cell(cell))
                rate += year_weight * age_weights[age] * 0.5 * (count / population) * RATE_SCALE
    return rate


def build_standardized_rows(
    years: list[int],
    age_groups: tuple[str, ...],
    age_weights: dict[str, float],
    populations: dict[tuple[str, int, str, str], dict[str, int]],
    parser_counts: dict[tuple[str, int, str, str], dict[str, int]],
    accumulator: CompositionAccumulator,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rates_by_country: dict[str, dict[str, float]] = {}
    for country in COUNTRIES:
        rates_by_country[country] = {
            measure: direct_rate(
                lambda cell, m=measure: parser_counts[cell][m],
                country,
                years,
                age_groups,
                age_weights,
                populations,
            )
            for measure in ("ucd", "any_common")
        }
        rates_by_country[country]["extra"] = (
            rates_by_country[country]["any_common"] - rates_by_country[country]["ucd"]
        )
    rd_any = rates_by_country["BR"]["any_common"] - rates_by_country["US"]["any_common"]
    rd_ucd = rates_by_country["BR"]["ucd"] - rates_by_country["US"]["ucd"]
    direct_rd_extra = rd_any - rd_ucd
    direct_extra_difference = rates_by_country["BR"]["extra"] - rates_by_country["US"]["extra"]
    base_checks = {
        "direct_rd_any_per_100000": rd_any,
        "direct_rd_ucd_per_100000": rd_ucd,
        "direct_rd_any_minus_rd_ucd_per_100000": direct_rd_extra,
        "direct_rd_extra_from_country_rates_per_100000": direct_extra_difference,
        "direct_rd_identity_error": direct_extra_difference - direct_rd_extra,
        "identity_tolerance": RATE_IDENTITY_TOLERANCE,
    }
    if abs(base_checks["direct_rd_identity_error"]) > RATE_IDENTITY_TOLERANCE:
        raise CompositionError("Direct RD_any - RD_ucd does not equal direct RD_extra")

    output: list[dict[str, Any]] = []
    dimension_checks: dict[str, Any] = {}
    for dimension, categories in EXTRA_DIMENSIONS.items():
        country_category_rates: dict[str, dict[str, float]] = {country: {} for country in COUNTRIES}
        for country in COUNTRIES:
            for category in categories:
                country_category_rates[country][category] = direct_rate(
                    lambda cell, d=dimension, c=category: count_or_zero(
                        accumulator.extra_category_counts, cell + (d, c)
                    ),
                    country,
                    years,
                    age_groups,
                    age_weights,
                    populations,
                )
        for country in COUNTRIES:
            category_sum = sum(country_category_rates[country].values())
            error = category_sum - rates_by_country[country]["extra"]
            dimension_checks[f"{dimension}:{country}"] = {
                "sum_category_rates_per_100000": category_sum,
                "direct_extra_rate_per_100000": rates_by_country[country]["extra"],
                "absolute_error": abs(error),
                "pass": abs(error) <= RATE_IDENTITY_TOLERANCE,
            }
            if abs(error) > RATE_IDENTITY_TOLERANCE:
                raise CompositionError(f"Category rates do not partition EXTRA rate for {dimension}/{country}")

        total_contrast = direct_rd_extra
        for category in categories:
            br_rate = country_category_rates["BR"][category]
            us_rate = country_category_rates["US"][category]
            difference = br_rate - us_rate
            percent = (
                difference / total_contrast * 100.0
                if abs(total_contrast) > PERCENT_DENOMINATOR_EPSILON
                else ""
            )
            output.extend(
                [
                    {
                        "country_or_contrast": "BR",
                        "dimension": dimension,
                        "category": category,
                        "category_label": CHAPTER_TITLE_BY_CODE.get(category, category),
                        "record_count": sum(
                            count_or_zero(accumulator.extra_category_counts, ("BR", year, age, sex, dimension, category))
                            for year in years for age in age_groups for sex in SEXES
                        ),
                        "standardized_extra_rate_per_100000": br_rate,
                        "paired_difference_contribution_per_100000": "",
                        "contribution_percent_of_direct_RD_extra": "",
                        "overall_direct_RD_extra_denominator": total_contrast,
                    },
                    {
                        "country_or_contrast": "US",
                        "dimension": dimension,
                        "category": category,
                        "category_label": CHAPTER_TITLE_BY_CODE.get(category, category),
                        "record_count": sum(
                            count_or_zero(accumulator.extra_category_counts, ("US", year, age, sex, dimension, category))
                            for year in years for age in age_groups for sex in SEXES
                        ),
                        "standardized_extra_rate_per_100000": us_rate,
                        "paired_difference_contribution_per_100000": "",
                        "contribution_percent_of_direct_RD_extra": "",
                        "overall_direct_RD_extra_denominator": total_contrast,
                    },
                    {
                        "country_or_contrast": "BR-US",
                        "dimension": dimension,
                        "category": category,
                        "category_label": CHAPTER_TITLE_BY_CODE.get(category, category),
                        "record_count": "",
                        "standardized_extra_rate_per_100000": difference,
                        "paired_difference_contribution_per_100000": difference,
                        "contribution_percent_of_direct_RD_extra": percent,
                        "overall_direct_RD_extra_denominator": total_contrast,
                    },
                ]
            )
        contrast_sum = sum(
            country_category_rates["BR"][category] - country_category_rates["US"][category]
            for category in categories
        )
        contrast_error = contrast_sum - direct_rd_extra
        dimension_checks[f"{dimension}:BR-US"] = {
            "sum_category_rate_differences_per_100000": contrast_sum,
            "direct_rd_any_minus_rd_ucd_per_100000": direct_rd_extra,
            "absolute_error": abs(contrast_error),
            "pass": abs(contrast_error) <= RATE_IDENTITY_TOLERANCE,
            "estimand": "direct RD_any - direct RD_ucd; not model-based interaction DD",
        }
        if abs(contrast_error) > RATE_IDENTITY_TOLERANCE:
            raise CompositionError(
                f"Category rate differences do not equal direct RD_any - RD_ucd for {dimension}"
            )
    return output, {**base_checks, "category_partition_checks": dimension_checks}


def make_source_code_rows(
    years: list[int],
    age_groups: tuple[str, ...],
    accumulator: CompositionAccumulator,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for country in COUNTRIES:
        for year in years:
            for age in age_groups:
                for sex in SEXES:
                    cell = (country, year, age, sex)
                    for code in sorted(
                        key[-1]
                        for key, value in accumulator.source_code_counts.items()
                        if key[:4] == cell and value > 0
                    ):
                        info = code_status_summary(code, accumulator.who_reference)
                        rows.append(
                            {
                                "country": country,
                                "year": year,
                                "age_group": age,
                                "sex": sex,
                                "source_any_code": info["normalized_code"],
                                "code4": info["code4"],
                                "code_class": info["code_class"],
                                "reference_status": info["reference_status"],
                                "reference_title": info["reference_title"],
                                "records_with_code": accumulator.source_code_counts[cell + (code,)],
                                "count_scope": "unique record/code within UCD union entity (US) or UCD union lines (BR)",
                                "exclusive_across_codes": "false",
                            }
                        )
    return rows


def make_k82_rows(
    years: list[int], who_reference: dict[str, dict[str, str]], accumulator: CompositionAccumulator
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reference_k82 = sorted(code for code in who_reference if code.startswith("K82"))
    rows: list[dict[str, Any]] = []
    comparison: dict[str, Any] = {}
    for country in COUNTRIES:
        for year in years:
            denominator = accumulator.k82_any_counts[(country, year)]
            observed_codes = {
                code
                for c, y, code in accumulator.k82_code_counts
                if c == country and y == year
            }
            all_codes = set(reference_k82) | observed_codes
            per_code_shares: dict[str, float | None] = {}
            for code in sorted(all_codes):
                count = accumulator.k82_code_counts[(country, year, code)]
                info = code_status_summary(code, who_reference)
                share = count / denominator if denominator else None
                per_code_shares[code] = share
                rows.append(
                    {
                        "country": country,
                        "year": year,
                        "k82_source_code": code,
                        "code4": info["code4"],
                        "code_class": info["code_class"],
                        "reference_status": info["reference_status"],
                        "reference_title": info["reference_title"],
                        "records_with_code": count,
                        "unique_k82_any_records": denominator,
                        "record_prevalence_among_k82_any": "" if share is None else share,
                        "prevalence_is_nonexclusive": "true",
                    }
                )
            share_828 = per_code_shares.get("K828")
            share_829 = per_code_shares.get("K829")
            comparison[f"{country}_{year}"] = {
                "unique_k82_any_records": denominator,
                "K828_record_prevalence": share_828,
                "K829_record_prevalence": share_829,
                "K828_minus_K829_prevalence": (
                    share_828 - share_829 if share_828 is not None and share_829 is not None else None
                ),
                "interpretation": "nonexclusive record prevalence among unique K82-any records; not a mutually exclusive distribution",
            }
    return rows, comparison


def make_ucd_rows(
    years: list[int], age_groups: tuple[str, ...], accumulator: CompositionAccumulator
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (country, year, age, sex, code), count in sorted(accumulator.extra_ucd_counts.items()):
        chapter = ucd_chapter(code)
        rows.append(
            {
                "country": country,
                "year": year,
                "age_group": age,
                "sex": sex,
                "nonbiliary_ucd_code": code,
                "who_chapter": chapter,
                "chapter_title": CHAPTER_TITLE_BY_CODE[chapter],
                "extra_records": count,
                "cohort": "EXTRA_any_common_1_ucd_biliary_0",
            }
        )
    return rows


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def output_names(stem: str) -> dict[str, str]:
    return {
        "records": f"record_counts_by_stratum_{stem}.csv",
        "any_categories": f"any_category_counts_by_stratum_{stem}.csv",
        "extra_categories": f"extra_disjoint_category_counts_by_stratum_{stem}.csv",
        "extra_rates": f"extra_disjoint_standardized_rates_{stem}.csv",
        "source_codes": f"source_any_code_counts_{stem}.csv",
        "extra_ucd": f"extra_nonbiliary_ucd_counts_{stem}.csv",
        "k82": f"k82_code_prevalence_{stem}.csv",
        "readme": f"README_{stem}.md",
        "validation": f"validation_{stem}.json",
        "complete": f"COMPLETE_{stem}.txt",
    }


def validate_reuse(paths: dict[str, Path], run_key: str, names: dict[str, str]) -> dict[str, Any] | None:
    try:
        validation_path = paths["validation"]
        marker_path = paths["complete"]
        if not validation_path.is_file() or not marker_path.is_file():
            return None
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        marker = marker_path.read_text(encoding="utf-8")
        if validation.get("run_key") != run_key or validation.get("gate_passed") is not True:
            return None
        if f"run_key={run_key}" not in marker or "gate=PASS" not in marker:
            return None
        if f"validation_sha256={sha256_file(validation_path)}" not in marker:
            return None
        hashes = validation.get("output_sha256", {})
        for key, name in names.items():
            if key == "complete" or key == "validation":
                continue
            path = paths[key]
            if not path.is_file() or hashes.get(name) != sha256_file(path):
                return None
        return validation
    except (OSError, ValueError, TypeError):
        return None


def make_readme(years: list[int], rate_checks: dict[str, Any], who_reference_count: int) -> str:
    window = ", ".join(str(year) for year in years)
    return f"""# Parser-bound composition summary: Brazil and United States

Selected years: {window}.

This private, unsuppressed output is an aggregate analysis artifact, not a
publication-ready table. It reads only the completed, current-parser feature
CSV files. It does not reread raw death files or retain record-level rows.

## Cohorts and partitions

- `ANY_PRIMARY`: parser `any_common == 1`; UCD-biliary records are included.
- `EXTRA`: `any_common == 1` and `ucd_biliary == 0`; its count is checked in
  every age/sex cell against `any_common - ucd`.
- US records with `any_common == 0` and `alternative == 1` are counted
  separately as alternative-only and are excluded from the primary cohort.
- Part I/Part II groups describe where coded mentions were captured. They are
  not measures of clinical severity, causal order, or true underlying cause.
- The 15 `source_prefix_set` values are mutually exclusive sets across K80,
  K81, K82, and K83. Each EXTRA record contributes once to exactly one set.
- UCD chapter groups are mutually exclusive; unparseable/non-standard values
  are retained in `UNKNOWN` rather than dropped.

## Standardization and additive identity

Rates use the frozen 936-table population denominators, WHO-18 age weights
from the frozen metadata file, sex weights 0.5/0.5, and equal weights over the
selected years. All 18 age groups, both sexes, every selected year, and zero
category cells are included; there is no support-based reweighting.

For each disjoint EXTRA dimension, category rates are direct-standardized and
sum to the direct `RD_any - RD_ucd` identity. This is a descriptive rate-scale
decomposition and is distinct from the model-based interaction `DD`. Category
rate differences may be positive or negative. Percent contributions are left
blank when the total direct contrast is within {PERCENT_DENOMINATOR_EPSILON:g}
per 100,000 of zero.

Four-character WHO code counts are deduplicated within record/code but are
nonexclusive across codes; they must not be summed as if they partitioned
records. The {who_reference_count} WHO ICD-10 2019 reference codes are mapped
to their official titles. Three-character codes, unmatched four-character
codes, and longer-than-four-character source strings remain separately
classified. The K82 table reports each full code's nonexclusive prevalence
among unique K82-any records, including K828 and K829; it is not a mutually
exclusive distribution.

## Files

- `record_counts_by_stratum`: frozen counts, population, EXTRA, and US
  alternative-only counts for country/year/age/sex cells.
- `any_category_counts_by_stratum`: mutually exclusive source-prefix sets
  and Part I/Part II groups within ANY_PRIMARY.
- `extra_disjoint_category_counts_by_stratum`: zero-filled disjoint EXTRA
  partitions by prefix set, mention position, and nonbiliary UCD chapter.
- `extra_disjoint_standardized_rates`: BR and US category rates and BR-US
  rate-scale contributions.
- `source_any_code_counts`: nonexclusive, per-record-deduplicated source ANY
  code counts with WHO-reference status; code counts are not standardized or
  additive.
- `extra_nonbiliary_ucd_counts`: nonbiliary UCD code and chapter counts by
  country/year/age/sex.
- `k82_code_prevalence`: K82 full-code record counts and nonexclusive shares.
- `validation`: parser gates, additive identities, and input/output SHA-256s.

These are descriptive registry-data summaries. They do not support causal
claims, clinical severity interpretation, or policy/resource-allocation
inference. Cross-country certification, coding, and registration differences
remain measurement limitations.
"""


def run_composition(
    years: list[int],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    br_dir: Path = DEFAULT_BR_DIR,
    us_dir: Path = DEFAULT_US_DIR,
    identity_path: Path = DEFAULT_IDENTITY,
    frozen_analysis_path: Path = FROZEN_ANALYSIS,
    frozen_subcode_path: Path = FROZEN_SUBCODE,
    who_weights_path: Path = WHO_WEIGHTS,
    who_reference_path: Path = WHO_BILIARY_REFERENCE,
) -> dict[str, Any]:
    years = sorted(years)
    if not years or len(set(years)) != len(years):
        raise CompositionError("Selected years must be nonempty and unique")
    for path in (identity_path, frozen_analysis_path, frozen_subcode_path, who_weights_path, who_reference_path):
        if not path.is_file():
            raise CompositionError(f"Required composition input is missing: {path}")
    parser_br_path = PROJECT_ROOT / "scripts" / "extract_br.py"
    parser_us_path = PROJECT_ROOT / "scripts" / "extract_us.py"
    parser_br_sha = sha256_file(parser_br_path)
    parser_us_sha = sha256_file(parser_us_path)
    composition_parser_sha = sha256_file(Path(__file__).resolve())
    identity = load_source_identity(identity_path, years)
    frozen_analysis_sha = sha256_file(frozen_analysis_path)
    frozen_subcode_sha = sha256_file(frozen_subcode_path)
    weights_sha = sha256_file(who_weights_path)
    who_reference_sha = sha256_file(who_reference_path)

    runs: dict[tuple[str, int], dict[str, Any]] = {}
    input_hashes: dict[str, str] = {
        "source_identity.csv": sha256_file(identity_path),
        "analysis_input_936.csv": frozen_analysis_sha,
        "subcode_analysis_input_3744.csv": frozen_subcode_sha,
        "who_2000_2025_18_group_weights_correct.csv": weights_sha,
        "WHO_ICD10_2019_biliary_reference.csv": who_reference_sha,
        "scripts/extract_br.py": parser_br_sha,
        "scripts/extract_us.py": parser_us_sha,
        "scripts/summarize_composition.py": composition_parser_sha,
    }
    for year in years:
        br_run = validate_br_run(
            year,
            br_dir,
            identity[("BR", year)],
            parser_br_sha,
            frozen_analysis_sha,
            frozen_subcode_sha,
        )
        us_run = validate_us_run(
            year,
            us_dir,
            identity[("US", year)],
            parser_us_sha,
            frozen_analysis_sha,
            frozen_subcode_sha,
        )
        runs[("BR", year)] = br_run
        runs[("US", year)] = us_run
        for run in (br_run, us_run):
            key_prefix = f"{run['country']}_{year}"
            input_hashes[f"{key_prefix}_summary.json"] = run["summary_sha256"]
            for output_key, path in run["outputs"].items():
                input_hashes[f"{key_prefix}_{output_key}_{path.name}"] = sha256_file(path)

    input_manifest = {
        "years": years,
        "parser_sha256": {"BR": parser_br_sha, "US": parser_us_sha},
        "composition_parser_sha256": composition_parser_sha,
        "files_sha256": input_hashes,
    }
    run_key = stable_sha256(input_manifest)
    stem = run_stem(years)
    output_dir.mkdir(parents=True, exist_ok=True)
    names = output_names(stem)
    final_paths = {key: output_dir / name for key, name in names.items()}
    reusable = validate_reuse(final_paths, run_key, names)
    if reusable is not None:
        reusable["status"] = "reused_validated_output"
        reusable["output_paths"] = {key: str(path) for key, path in final_paths.items() if path.exists()}
        return reusable
    for path in final_paths.values():
        if path.exists():
            path.unlink()

    age_weights, age_groups = load_age_weights(who_weights_path)
    who_reference = load_who_reference(who_reference_path)
    frozen_rows = load_frozen_rows(frozen_analysis_path, years, age_groups)
    parser_counts, populations = load_parser_rows(runs, frozen_rows, age_groups)
    accumulator = CompositionAccumulator(years, age_groups, who_reference)
    candidate_summary: dict[str, Any] = {}
    for country in COUNTRIES:
        for year in years:
            key = (country, year)
            candidate_summary[f"{country}_{year}"] = load_candidates(
                runs[key], accumulator, {
                    cell: values for cell, values in parser_counts.items()
                    if cell[0] == country and cell[1] == year
                }
            )

    record_rows, any_category_rows, extra_category_rows = make_count_rows(
        years, age_groups, parser_counts, populations, accumulator
    )
    rate_rows, rate_checks = build_standardized_rows(
        years, age_groups, age_weights, populations, parser_counts, accumulator
    )
    source_code_rows = make_source_code_rows(years, age_groups, accumulator)
    k82_rows, k82_comparison = make_k82_rows(years, who_reference, accumulator)
    extra_ucd_rows = make_ucd_rows(years, age_groups, accumulator)

    # Validate that every disjoint table is an exact per-cell partition.
    cell_partition_checks: dict[str, Any] = {}
    for cell, counts in parser_counts.items():
        expected_extra = counts["any_common"] - counts["ucd"]
        for dimension, categories in ANY_DIMENSIONS.items():
            counter = accumulator.any_prefix_counts if dimension == "source_prefix_set" else accumulator.any_position_counts
            summed = sum(counter[cell + (category,)] for category in categories)
            if summed != counts["any_common"]:
                raise CompositionError(f"ANY_PRIMARY {dimension} counts fail to partition at {cell}")
        for dimension, categories in EXTRA_DIMENSIONS.items():
            summed = sum(accumulator.extra_category_counts[cell + (dimension, category)] for category in categories)
            if summed != expected_extra:
                raise CompositionError(f"EXTRA {dimension} counts fail to partition at {cell}")
        cell_partition_checks["|".join(map(str, cell))] = {
            "any_common": counts["any_common"],
            "ucd": counts["ucd"],
            "extra": expected_extra,
            "pass": True,
        }

    output_rows_by_key = {
        "records": record_rows,
        "any_categories": any_category_rows,
        "extra_categories": extra_category_rows,
        "extra_rates": rate_rows,
        "source_codes": source_code_rows,
        "extra_ucd": extra_ucd_rows,
        "k82": k82_rows,
    }
    fieldnames = {
        "records": ("country", "year", "age_group", "sex", "population", "ucd_count", "any_common_count", "alternative_count", "extra_count", "us_alternative_only_count"),
        "any_categories": ("country", "year", "age_group", "sex", "dimension", "category", "any_primary_records", "exclusive_partition", "aggregation_scope"),
        "extra_categories": ("country", "year", "age_group", "sex", "dimension", "category", "category_label", "extra_records", "exclusive_partition", "aggregation_scope"),
        "extra_rates": ("country_or_contrast", "dimension", "category", "category_label", "record_count", "standardized_extra_rate_per_100000", "paired_difference_contribution_per_100000", "contribution_percent_of_direct_RD_extra", "overall_direct_RD_extra_denominator"),
        "source_codes": ("country", "year", "age_group", "sex", "source_any_code", "code4", "code_class", "reference_status", "reference_title", "records_with_code", "count_scope", "exclusive_across_codes"),
        "extra_ucd": ("country", "year", "age_group", "sex", "nonbiliary_ucd_code", "who_chapter", "chapter_title", "extra_records", "cohort"),
        "k82": ("country", "year", "k82_source_code", "code4", "code_class", "reference_status", "reference_title", "records_with_code", "unique_k82_any_records", "record_prevalence_among_k82_any", "prevalence_is_nonexclusive"),
    }
    readme_text = make_readme(years, rate_checks, len(who_reference))

    with tempfile.TemporaryDirectory(prefix=f".composition-{stem}-", dir=output_dir) as temp_name:
        temp_dir = Path(temp_name)
        staged: dict[str, Path] = {}
        for key, rows in output_rows_by_key.items():
            path = temp_dir / names[key]
            write_csv(path, fieldnames[key], rows)
            staged[key] = path
        staged["readme"] = temp_dir / names["readme"]
        staged["readme"].write_text(readme_text, encoding="utf-8")

        output_hashes = {
            names[key]: sha256_file(path)
            for key, path in staged.items()
        }
        validation = {
            "status": "complete",
            "gate_passed": True,
            "country_pair": ["BR", "US"],
            "years": years,
            "run_key": run_key,
            "input_manifest": input_manifest,
            "input_hash_count": len(input_hashes),
            "validated_parser_runs": {
                f"{country}_{year}": {
                    "summary_path": str(run["summary_path"]),
                    "summary_sha256": run["summary_sha256"],
                    "parser_sha256": run["parser_sha256"],
                    "source_sha256": run["source_sha256"],
                    "candidate_path": str(run["outputs"]["candidate"]),
                    "candidate_sha256": sha256_file(run["outputs"]["candidate"]),
                    "candidate_audit": candidate_summary[f"{country}_{year}"],
                }
                for (country, year), run in sorted(runs.items())
            },
            "denominator_and_weights": {
                "population_source": str(frozen_analysis_path),
                "population_sha256": frozen_analysis_sha,
                "weight_source": str(who_weights_path),
                "weight_sha256": weights_sha,
                "weight_standard": "WHO 2000-2025 18 age groups; normalized frozen age weights",
                "sex_weights": {"F": 0.5, "M": 0.5},
                "year_weights": {str(year): 1.0 / len(years) for year in years},
                "zero_cells_included": True,
                "support_based_reweighting": False,
                "rate_scale": "per 100,000",
            },
            "cohort_definitions": {
                "ANY_PRIMARY": "any_common == 1",
                "EXTRA": "any_common == 1 and ucd_biliary == 0",
                "US_alternative_only": "any_common == 0 and alternative == 1; tracked separately and excluded from ANY_PRIMARY",
                "extra_identity": "extra count equals any_common minus UCD in every country/year/age/sex cell",
            },
            "who_reference": {
                "path": str(who_reference_path),
                "sha256": who_reference_sha,
                "version": "WHO_ICD10_2019",
                "terminal_codes": len(who_reference),
                "classification": "exact four-character match; three-character, unmatched four-character, and longer-than-four strings remain separately retained",
            },
            "direct_rate_identity": rate_checks,
            "cell_partition_check_count": len(cell_partition_checks),
            "cell_partition_checks": cell_partition_checks,
            "source_code_counts_nonexclusive": True,
            "k82_code_share_comparison": k82_comparison,
            "interpretive_scope": {
                "category_partition": "disjoint and exhaustive within the stated cohort",
                "position": "descriptive source location only; not cause order or clinical severity",
                "direct_rate_decomposition": "direct RD_any - direct RD_ucd; not the model-based interaction DD",
                "causal_claims": False,
                "publication_ready": False,
            },
            "output_sha256": output_hashes,
        }
        staged["validation"] = temp_dir / names["validation"]
        staged["validation"].write_text(
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validation_hash = sha256_file(staged["validation"])
        staged["complete"] = temp_dir / names["complete"]
        staged["complete"].write_text(
            f"gate=PASS\nrun_key={run_key}\nvalidation_sha256={validation_hash}\n",
            encoding="utf-8",
        )
        for key, path in staged.items():
            os.replace(path, final_paths[key])

    validation["output_paths"] = {key: str(path) for key, path in final_paths.items()}
    return validation


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=parse_years, default=[2019], help="Years, e.g. 2019 or 2012-2024 (default 2019).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--br-dir", type=Path, default=DEFAULT_BR_DIR)
    parser.add_argument("--us-dir", type=Path, default=DEFAULT_US_DIR)
    parser.add_argument("--identity-path", type=Path, default=DEFAULT_IDENTITY)
    parser.add_argument("--frozen-analysis-path", type=Path, default=FROZEN_ANALYSIS)
    parser.add_argument("--frozen-subcode-path", type=Path, default=FROZEN_SUBCODE)
    parser.add_argument("--who-weights-path", type=Path, default=WHO_WEIGHTS)
    parser.add_argument("--who-reference-path", type=Path, default=WHO_BILIARY_REFERENCE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        validation = run_composition(
            args.years,
            output_dir=args.output_dir,
            br_dir=args.br_dir,
            us_dir=args.us_dir,
            identity_path=args.identity_path,
            frozen_analysis_path=args.frozen_analysis_path,
            frozen_subcode_path=args.frozen_subcode_path,
            who_weights_path=args.who_weights_path,
            who_reference_path=args.who_reference_path,
        )
    except Exception as exc:
        print(f"Composition summary failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": validation.get("status"),
                "gate_passed": validation.get("gate_passed"),
                "years": validation.get("years"),
                "run_key": validation.get("run_key"),
                "output_paths": validation.get("output_paths"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if validation.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
