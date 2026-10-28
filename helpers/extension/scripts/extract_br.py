#!/usr/bin/env python3
"""Stream Brazil SIM mortality records and audit the frozen 2019 pilot.

The source is read directly from a CSV or a ZIP member; no raw file is extracted
to disk and no record-level identifiers or source lines are written. The pilot
is intentionally a strict reconstruction gate: a completion marker is emitted
only when source identity and every frozen Brazil count cell match exactly.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = Path(r"D:\数据库集合\巴西 SIM 数据库\raw")
DEFAULT_FROZEN_DIR = Path(
    r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "br"

ANALYSIS_INPUT_NAME = "analysis_input_936.csv"
SUBCODE_INPUT_NAME = "subcode_analysis_input_3744.csv"
LEDGER_RELATIVE_PATH = Path("data") / "source_data" / "raw_scan_cache_ledger.csv"

AGE_GROUPS = tuple(
    ["0-4"]
    + [f"{lower}-{lower + 4}" for lower in range(5, 85, 5)]
    + ["85+"]
)
SEXES = ("F", "M")
COMPONENTS = ("K80", "K81", "K82", "K83")
PART_I_FIELDS = ("LINHAA", "LINHAB", "LINHAC", "LINHAD")
LINE_FIELDS = PART_I_FIELDS + ("LINHAII",)
REQUIRED_HEADERS = frozenset(
    {
        "TIPOBITO",
        "DTOBITO",
        "IDADE",
        "SEXO",
        "CODMUNRES",
        "CAUSABAS",
        *LINE_FIELDS,
    }
)
OPTIONAL_AUDIT_HEADERS = ("ATESTADO", "ATESTANTE")
MISSING_TOKENS = frozenset({"", "NA", "N/A", "NULL", "NAN", "NONE", "NULO", "\\N"})

# The boundary expression is deliberately the one in the frozen analysis
# contract. Do not replace it with a broad K80-prefix search on line fields.
BILIARY_RE = re.compile(r"(?<![A-Z0-9])K8[0-3][0-9A-Z]?(?![A-Z0-9])")
# Tokens are extracted only from coded cause fields, never from ATESTADO text.
ICD_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4}|[0-9A-Z]{1,2})?)(?![A-Z0-9])"
)

CANDIDATE_FIELDS = (
    "country",
    "year",
    "source_row",
    "age_group",
    "sex",
    "ucd_code",
    "ucd_biliary",
    "any_common",
    "alternative",
    "part_i_biliary",
    "part_ii_biliary",
    "line_codes",
    "line_biliary_codes",
    "position_codes",
)


class ExtractionError(RuntimeError):
    """A source, schema, reference, or processing error that blocks the gate."""


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def is_missing(value: Any) -> tuple[bool, str]:
    """Preserve explicit CSV NA spellings and return an audit category."""
    if value is None:
        return True, "missing_blank"
    text = str(value).strip()
    if text == "":
        return True, "missing_blank"
    if text.upper() in MISSING_TOKENS - {""}:
        return True, "missing_na_string"
    return False, "present"


def parse_age(raw_value: Any) -> tuple[int | None, str]:
    """Decode SIM IDADE (unit + two digits), leaving unknowns un-imputed."""
    missing, missing_status = is_missing(raw_value)
    if missing:
        return None, missing_status
    text = str(raw_value).strip()
    if len(text) != 3:
        return None, "invalid_length"
    if not text.isdigit():
        return None, "invalid_nondigit"
    unit = text[0]
    value = int(text[1:])
    if unit in {"1", "2", "3"}:
        return 0, f"unit_{unit}"
    if unit == "4":
        return value, "unit_4_years"
    if unit == "5":
        return 100 + value, "unit_5_centenarian"
    return None, "unknown_unit"


def age_group_for_age(age: int) -> str:
    if age < 5:
        return "0-4"
    if age >= 85:
        return "85+"
    lower = 5 + ((age - 5) // 5) * 5
    return f"{lower}-{lower + 4}"


def parse_sex(raw_value: Any) -> tuple[str | None, str]:
    missing, missing_status = is_missing(raw_value)
    if missing:
        return None, missing_status
    text = str(raw_value).strip().upper()
    if text in {"1", "M"}:
        return "M", "valid_male"
    if text in {"2", "F"}:
        return "F", "valid_female"
    return None, "invalid_value"


def normalize_ucd(raw_value: Any) -> str:
    missing, _ = is_missing(raw_value)
    if missing:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw_value).upper())


def extract_line_codes(row: dict[str, Any]) -> dict[str, Any]:
    """Extract/deduplicate ICD tokens as source-field/code pairs.

    A repeated code within one field is emitted once; the same code in two
    different certificate fields remains two distinct pairs. Position metadata
    is based only on LINHAA-D and LINHAII, not free-text certificate content.
    """
    all_pairs: list[dict[str, str]] = []
    biliary_pairs: list[dict[str, str]] = []
    positioned_pairs: list[dict[str, str]] = []
    biliary_components: set[str] = set()
    raw_biliary_fields: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    for field in LINE_FIELDS:
        raw_value = row.get(field, "")
        missing, _ = is_missing(raw_value)
        if missing:
            continue
        coded_text = str(raw_value).upper().replace("*", " ").replace("/", " ")
        part = "I" if field in PART_I_FIELDS else "II"
        position = field[-1] if field in PART_I_FIELDS else "II"

        if BILIARY_RE.search(coded_text):
            raw_biliary_fields.add(field)
        for match in ICD_TOKEN_RE.finditer(coded_text):
            code = match.group(1).upper()
            pair_key = (field, code)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pair = {"field": field, "code": code}
            all_pairs.append(pair)
            positioned_pairs.append(
                {"part": part, "position": position, "field": field, "code": code}
            )
            if BILIARY_RE.search(code):
                biliary_pairs.append(pair)
                biliary_components.add(code[:3])

        # Component presence follows the exact frozen boundary expression even
        # if a malformed-but-matching token cannot be represented by the
        # general ICD token extractor. Such a string still affects the binary
        # any-common definition; the audit records the discrepancy.
        for match in BILIARY_RE.finditer(coded_text):
            biliary_components.add(match.group(0)[:3])

    return {
        "line_codes": all_pairs,
        "line_biliary_codes": biliary_pairs,
        "position_codes": positioned_pairs,
        "line_biliary_components": biliary_components,
        "raw_biliary_fields": raw_biliary_fields,
        "has_line_biliary": bool(raw_biliary_fields),
    }


def source_path_for_year(raw_dir: Path, year: int) -> Path:
    if 2012 <= year <= 2021:
        return raw_dir / f"Mortalidade_Geral_{year}_csv.zip"
    if year == 2022:
        return raw_dir / "DO22OPEN.csv"
    if year == 2023:
        return raw_dir / "DO23OPEN.csv"
    if year == 2024:
        return raw_dir / "DO24OPEN_csv.zip"
    raise ExtractionError(f"Year outside supported Brazil SIM window: {year}")


def parse_years(value: str) -> list[int]:
    years: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bits = part.split("-")
            if len(bits) != 2:
                raise argparse.ArgumentTypeError(f"Invalid year range: {part}")
            try:
                first, last = map(int, bits)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid year range: {part}") from exc
            if first > last:
                raise argparse.ArgumentTypeError(f"Reversed year range: {part}")
            years.extend(range(first, last + 1))
        else:
            try:
                years.append(int(part))
            except ValueError as exc:
                raise argparse.ArgumentTypeError(f"Invalid year: {part}") from exc
    if not years or any(year < 2012 or year > 2024 for year in years):
        raise argparse.ArgumentTypeError("Years must be selected from 2012 through 2024")
    if len(set(years)) != len(years):
        raise argparse.ArgumentTypeError("Duplicate years are not allowed")
    return sorted(years)


def output_stem(years: list[int]) -> str:
    return f"BR_{years[0]}" if len(years) == 1 else f"BR_{years[0]}-{years[-1]}"


@contextmanager
def open_source_text(
    path: Path, status: dict[str, Any]
) -> Iterator[tuple[TextIO, str | None]]:
    """Yield decoded text directly from a CSV or ZIP member without extraction."""
    if path.suffix.lower() != ".zip":
        with path.open("r", encoding="latin-1", newline="") as text:
            yield text, None
        return

    try:
        with zipfile.ZipFile(path, "r") as archive:
            regular_files = [entry for entry in archive.infolist() if not entry.is_dir()]
            csv_entries = [entry for entry in regular_files if entry.filename.lower().endswith(".csv")]
            if len(csv_entries) != 1:
                raise ExtractionError(
                    f"Expected exactly one CSV member in {path.name}; found {len(csv_entries)}"
                )
            csv_entry = csv_entries[0]
            extra_entries = [entry for entry in regular_files if entry is not csv_entry]
            with archive.open(csv_entry, "r") as binary:
                with io.TextIOWrapper(binary, encoding="latin-1", newline="") as text:
                    yield text, csv_entry.filename
            if not status.get("csv_eof_reached"):
                raise ExtractionError(f"CSV stream did not reach EOF: {path.name}")
            # Validate CRCs for any ancillary members without writing them to
            # disk. The principal CSV CRC is checked by ZipExtFile at EOF.
            for entry in extra_entries:
                with archive.open(entry, "r") as extra:
                    while extra.read(1024 * 1024):
                        pass
            status["zip_crc_verified"] = True
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        if isinstance(exc, ExtractionError):
            raise
        raise ExtractionError(f"ZIP read/CRC failure for {path.name}: {exc}") from exc


def read_headers(reader: csv.DictReader) -> list[str]:
    if reader.fieldnames is None:
        raise ExtractionError("Source has no CSV header")
    headers = [str(name).strip().lstrip("\ufeff").upper() for name in reader.fieldnames]
    if len(set(headers)) != len(headers):
        duplicates = sorted({header for header in headers if headers.count(header) > 1})
        raise ExtractionError(f"Duplicate source header(s): {duplicates}")
    reader.fieldnames = headers
    missing = sorted(REQUIRED_HEADERS - set(headers))
    if missing:
        raise ExtractionError(f"Missing required source header(s): {', '.join(missing)}")
    return headers


def new_year_audit(year: int) -> dict[str, Any]:
    return {
        "year": year,
        "csv_data_records": 0,
        "base_eligible_records": 0,
        "analysis_eligible_records": 0,
        "candidate_records": 0,
        "tipobito_status": Counter(),
        "codmunres_status": Counter(),
        "age_all_status": Counter(),
        "age_after_base_filter_status": Counter(),
        "sex_all_status": Counter(),
        "sex_after_base_filter_status": Counter(),
        "dto_year_all_status": Counter(),
        "dto_year_after_base_filter_status": Counter(),
        "dto_observed_years": Counter(),
        "ucd_status_analysis_eligible": Counter(),
        "line_biliary_without_token_pairs": 0,
        "headers": {},
        "source_member": None,
        "csv_eof_reached": False,
        "zip_crc_verified": False,
    }


def simplify_audit(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(sorted(value.items()))
    if isinstance(value, dict):
        return {key: simplify_audit(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [simplify_audit(item) for item in value]
    return value


def parse_dto_year(raw_value: Any, source_year: int) -> tuple[str, str | None]:
    missing, missing_status = is_missing(raw_value)
    if missing:
        return missing_status, None
    text = str(raw_value).strip()
    if len(text) < 8:
        return "invalid_length", None
    observed = text[4:8]
    if len(observed) != 4 or not observed.isdigit():
        return "invalid_year_substring", None
    if int(observed) != source_year:
        return "mismatches_source_year", observed
    return "matches_source_year", observed


def process_source_year(
    year: int,
    source_path: Path,
    candidate_writer: csv.DictWriter,
    strata_counts: dict[tuple[int, str, str], dict[str, int]],
    component_counts: dict[tuple[int, str, str, str], dict[str, int]],
) -> dict[str, Any]:
    audit = new_year_audit(year)
    status: dict[str, Any] = {"csv_eof_reached": False, "zip_crc_verified": False}
    with open_source_text(source_path, status) as (text, source_member):
        audit["source_member"] = source_member
        reader = csv.DictReader(text, delimiter=";", strict=True)
        headers = read_headers(reader)
        audit["headers"] = {
            "required_present": {header: header in headers for header in sorted(REQUIRED_HEADERS)},
            "optional_audit_present": {
                header: header in headers for header in OPTIONAL_AUDIT_HEADERS
            },
            "missing_required": [],
        }
        for source_row, row in enumerate(reader, start=1):
            audit["csv_data_records"] += 1
            if None in row:
                raise ExtractionError(
                    f"Extra source field(s) at data record {source_row} in {source_path.name}"
                )
            malformed_required = [
                field for field in REQUIRED_HEADERS if row.get(field) is None
            ]
            if malformed_required:
                raise ExtractionError(
                    f"Truncated required field(s) at data record {source_row} in "
                    f"{source_path.name}: {', '.join(sorted(malformed_required))}"
                )

            tipobito_missing, tipobito_missing_status = is_missing(row.get("TIPOBITO"))
            tipobito_text = "" if tipobito_missing else str(row.get("TIPOBITO")).strip()
            if tipobito_missing:
                tipobito_status = tipobito_missing_status
            elif tipobito_text == "1":
                tipobito_status = "fetal_1_excluded"
            else:
                tipobito_status = "retained_nonfetal"
            audit["tipobito_status"][tipobito_status] += 1

            residence_missing, residence_missing_status = is_missing(row.get("CODMUNRES"))
            residence_status = (
                residence_missing_status if residence_missing else "nonempty_unvalidated"
            )
            audit["codmunres_status"][residence_status] += 1

            age, age_status = parse_age(row.get("IDADE"))
            sex, sex_status = parse_sex(row.get("SEXO"))
            audit["age_all_status"][age_status] += 1
            audit["sex_all_status"][sex_status] += 1
            dto_status, dto_year = parse_dto_year(row.get("DTOBITO"), year)
            audit["dto_year_all_status"][dto_status] += 1
            if dto_year is not None:
                audit["dto_observed_years"][dto_year] += 1

            base_eligible = tipobito_status == "retained_nonfetal" and not residence_missing
            if not base_eligible:
                continue
            audit["base_eligible_records"] += 1
            audit["age_after_base_filter_status"][age_status] += 1
            audit["sex_after_base_filter_status"][sex_status] += 1
            audit["dto_year_after_base_filter_status"][dto_status] += 1

            if age is None or sex is None:
                continue
            audit["analysis_eligible_records"] += 1
            age_group = age_group_for_age(age)
            ucd_code = normalize_ucd(row.get("CAUSABAS"))
            ucd_missing, ucd_missing_status = is_missing(row.get("CAUSABAS"))
            if ucd_missing:
                ucd_status = ucd_missing_status
            elif len(ucd_code) < 3:
                ucd_status = "too_short_after_normalization"
            elif ucd_code[:3] in COMPONENTS:
                ucd_status = "biliary_prefix"
            else:
                ucd_status = "non_biliary_prefix"
            audit["ucd_status_analysis_eligible"][ucd_status] += 1
            ucd_biliary = len(ucd_code) >= 3 and ucd_code[:3] in COMPONENTS

            line_data = extract_line_codes(row)
            has_line_biliary = line_data["has_line_biliary"]
            if has_line_biliary and not line_data["line_biliary_codes"]:
                audit["line_biliary_without_token_pairs"] += 1
            any_common = ucd_biliary or has_line_biliary
            cell_key = (year, age_group, sex)
            strata_counts[cell_key]["ucd"] += int(ucd_biliary)
            strata_counts[cell_key]["any_common"] += int(any_common)
            strata_counts[cell_key]["alternative"] += int(any_common)

            record_components = set(line_data["line_biliary_components"])
            if ucd_biliary:
                record_components.add(ucd_code[:3])
            for component in COMPONENTS:
                component_key = (year, age_group, sex, component)
                component_counts[component_key]["ucd"] += int(ucd_biliary and ucd_code[:3] == component)
                component_counts[component_key]["any_common"] += int(component in record_components)

            if not any_common:
                continue
            audit["candidate_records"] += 1
            candidate_writer.writerow(
                {
                    "country": "BR",
                    "year": year,
                    "source_row": source_row,
                    "age_group": age_group,
                    "sex": sex,
                    "ucd_code": ucd_code,
                    "ucd_biliary": int(ucd_biliary),
                    "any_common": 1,
                    "alternative": 1,
                    "part_i_biliary": int(
                        any(field in line_data["raw_biliary_fields"] for field in PART_I_FIELDS)
                    ),
                    "part_ii_biliary": int("LINHAII" in line_data["raw_biliary_fields"]),
                    "line_codes": json.dumps(
                        line_data["line_codes"], ensure_ascii=True, separators=(",", ":")
                    ),
                    "line_biliary_codes": json.dumps(
                        line_data["line_biliary_codes"], ensure_ascii=True, separators=(",", ":")
                    ),
                    "position_codes": json.dumps(
                        line_data["position_codes"], ensure_ascii=True, separators=(",", ":")
                    ),
                }
            )

        # DictReader completes only after the underlying iterator reaches EOF.
        status["csv_eof_reached"] = True

    audit["csv_eof_reached"] = bool(status.get("csv_eof_reached"))
    audit["zip_crc_verified"] = bool(status.get("zip_crc_verified"))
    if source_path.suffix.lower() == ".zip" and not audit["zip_crc_verified"]:
        raise ExtractionError(f"ZIP CRC was not confirmed after EOF: {source_path.name}")
    return simplify_audit(audit)


def initialize_counts(years: Iterable[int]) -> tuple[
    dict[tuple[int, str, str], dict[str, int]],
    dict[tuple[int, str, str, str], dict[str, int]],
]:
    strata = {
        (year, age_group, sex): {"ucd": 0, "any_common": 0, "alternative": 0}
        for year in years
        for age_group in AGE_GROUPS
        for sex in SEXES
    }
    components = {
        (year, age_group, sex, component): {"ucd": 0, "any_common": 0}
        for year in years
        for age_group in AGE_GROUPS
        for sex in SEXES
        for component in COMPONENTS
    }
    return strata, components


def load_ledger(path: Path, years: list[int]) -> dict[int, dict[str, str]]:
    wanted = set(years)
    found: dict[int, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"country", "year", "raw_sha256", "eligible_records", "record_rows"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ExtractionError(f"Raw scan ledger lacks required columns: {sorted(required)}")
        for row in reader:
            if row.get("country", "").strip().upper() != "BR":
                continue
            try:
                year = int(row.get("year", ""))
            except ValueError:
                continue
            if year not in wanted:
                continue
            if year in found:
                raise ExtractionError(f"Duplicate Brazil ledger row for {year}")
            found[year] = {key: (row.get(key) or "").strip() for key in required}
    return found


def load_frozen_table(
    path: Path, years: list[int], kind: str
) -> tuple[dict[tuple[Any, ...], dict[str, int]], list[dict[str, Any]], int]:
    expected_headers = (
        {"country", "year", "age_group", "sex", "ucd", "any_common", "alternative"}
        if kind == "strata"
        else {"country", "year", "age_group", "sex", "subcode", "ucd", "any_common"}
    )
    wanted_years = set(years)
    values: dict[tuple[Any, ...], dict[str, int]] = {}
    duplicates: list[dict[str, Any]] = []
    selected_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not expected_headers.issubset(reader.fieldnames):
            raise ExtractionError(
                f"Frozen {kind} input lacks required columns: {sorted(expected_headers)}"
            )
        for row in reader:
            if (row.get("country") or "").strip().upper() != "BR":
                continue
            try:
                year = int(row.get("year", ""))
            except ValueError:
                continue
            if year not in wanted_years:
                continue
            selected_rows += 1
            age_group = (row.get("age_group") or "").strip()
            sex = (row.get("sex") or "").strip().upper()
            if kind == "strata":
                key: tuple[Any, ...] = (year, age_group, sex)
                fields = ("ucd", "any_common", "alternative")
            else:
                subcode = (row.get("subcode") or "").strip().upper()
                key = (year, age_group, sex, subcode)
                fields = ("ucd", "any_common")
            try:
                parsed = {field: int(row.get(field, "")) for field in fields}
            except ValueError as exc:
                raise ExtractionError(f"Non-integer frozen {kind} count for key {key}") from exc
            if key in values:
                duplicates.append({"kind": kind, "key": list(key)})
            else:
                values[key] = parsed
    return values, duplicates, selected_rows


def mismatch_row(kind: str, key: Any, field: str, expected: Any, observed: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": json.dumps(key, ensure_ascii=True, separators=(",", ":")),
        "field": field,
        "expected": expected,
        "observed": observed,
    }


def compare_frozen(
    years: list[int],
    strata_counts: dict[tuple[int, str, str], dict[str, int]],
    component_counts: dict[tuple[int, str, str, str], dict[str, int]],
    frozen_strata: dict[tuple[Any, ...], dict[str, int]],
    frozen_components: dict[tuple[Any, ...], dict[str, int]],
    strata_duplicates: list[dict[str, Any]],
    component_duplicates: list[dict[str, Any]],
    frozen_strata_rows: int,
    frozen_component_rows: int,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    mismatches.extend(
        mismatch_row("duplicate_frozen_key", item.get("key"), item["kind"], "unique", "duplicate")
        for item in strata_duplicates + component_duplicates
    )
    expected_strata_keys = {
        (year, age_group, sex) for year in years for age_group in AGE_GROUPS for sex in SEXES
    }
    expected_component_keys = {
        (year, age_group, sex, component)
        for year in years
        for age_group in AGE_GROUPS
        for sex in SEXES
        for component in COMPONENTS
    }
    for year in years:
        expected_strata_rows = len(AGE_GROUPS) * len(SEXES)
        expected_component_rows = expected_strata_rows * len(COMPONENTS)
        observed_strata_rows = sum(1 for key in frozen_strata if key[0] == year)
        observed_component_rows = sum(1 for key in frozen_components if key[0] == year)
        if observed_strata_rows != expected_strata_rows:
            mismatches.append(
                mismatch_row(
                    "frozen_strata_row_count",
                    [year],
                    "row_count",
                    expected_strata_rows,
                    observed_strata_rows,
                )
            )
        if observed_component_rows != expected_component_rows:
            mismatches.append(
                mismatch_row(
                    "frozen_component_row_count",
                    [year],
                    "row_count",
                    expected_component_rows,
                    observed_component_rows,
                )
            )
    for key in sorted(expected_strata_keys):
        if key not in frozen_strata:
            mismatches.append(mismatch_row("frozen_missing_strata_key", key, "key", "present", "missing"))
            continue
        observed = strata_counts[key]
        expected = frozen_strata[key]
        for field in ("ucd", "any_common", "alternative"):
            if observed[field] != expected[field]:
                mismatches.append(
                    mismatch_row("frozen_strata_value", key, field, expected[field], observed[field])
                )
    for key in sorted(expected_component_keys):
        if key not in frozen_components:
            mismatches.append(
                mismatch_row("frozen_missing_component_key", key, "key", "present", "missing")
            )
            continue
        observed = component_counts[key]
        expected = frozen_components[key]
        for field in ("ucd", "any_common"):
            if observed[field] != expected[field]:
                mismatches.append(
                    mismatch_row("frozen_component_value", key, field, expected[field], observed[field])
                )
    extra_strata = sorted(set(frozen_strata) - expected_strata_keys)
    extra_components = sorted(set(frozen_components) - expected_component_keys)
    for key in extra_strata:
        mismatches.append(mismatch_row("frozen_unexpected_strata_key", key, "key", "absent", "present"))
    for key in extra_components:
        mismatches.append(mismatch_row("frozen_unexpected_component_key", key, "key", "absent", "present"))
    if frozen_strata_rows != len(expected_strata_keys):
        mismatches.append(
            mismatch_row("frozen_strata_selected_rows", years, "row_count", len(expected_strata_keys), frozen_strata_rows)
        )
    if frozen_component_rows != len(expected_component_keys):
        mismatches.append(
            mismatch_row(
                "frozen_component_selected_rows",
                years,
                "row_count",
                len(expected_component_keys),
                frozen_component_rows,
            )
        )
    return mismatches


def write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def output_rows(
    years: list[int],
    strata_counts: dict[tuple[int, str, str], dict[str, int]],
    component_counts: dict[tuple[int, str, str, str], dict[str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    strata_rows = [
        {
            "country": "BR",
            "year": year,
            "age_group": age_group,
            "sex": sex,
            **strata_counts[(year, age_group, sex)],
        }
        for year in years
        for age_group in AGE_GROUPS
        for sex in SEXES
    ]
    component_rows = [
        {
            "country": "BR",
            "year": year,
            "age_group": age_group,
            "sex": sex,
            "subcode": component,
            **component_counts[(year, age_group, sex, component)],
        }
        for year in years
        for age_group in AGE_GROUPS
        for sex in SEXES
        for component in COMPONENTS
    ]
    return strata_rows, component_rows


def current_run_paths(output_dir: Path, stem: str) -> dict[str, Path]:
    return {
        "candidate": output_dir / f"candidate_records_{stem}.csv.gz",
        "strata": output_dir / f"strata_counts_{stem}.csv",
        "components": output_dir / f"component_counts_{stem}.csv",
        "mismatches": output_dir / f"mismatches_{stem}.csv",
        "summary": output_dir / f"summary_{stem}.json",
        "complete": output_dir / f"COMPLETE_{stem}.txt",
    }


def can_reuse(paths: dict[str, Path], run_key: str) -> bool:
    try:
        if not paths["complete"].is_file() or not paths["summary"].is_file():
            return False
        summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
        if summary.get("run_key") != run_key or summary.get("gate_passed") is not True:
            return False
        marker = paths["complete"].read_text(encoding="utf-8")
        expected_summary_hash = file_sha256(paths["summary"])
        if f"summary_sha256={expected_summary_hash}" not in marker:
            return False
        if f"run_key={run_key}" not in marker or "gate=PASS" not in marker:
            return False
        artifacts = summary.get("artifact_sha256", {})
        for name in ("candidate", "strata", "components", "mismatches"):
            artifact = paths[name]
            if not artifact.is_file() or artifacts.get(artifact.name) != file_sha256(artifact):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def hash_inputs(
    years: list[int], raw_dir: Path, frozen_dir: Path, ledger_path: Path
) -> dict[str, Any]:
    source_paths = {year: source_path_for_year(raw_dir, year) for year in years}
    for source_path in source_paths.values():
        if not source_path.is_file():
            raise ExtractionError(f"Missing raw source: {source_path}")
    analysis_path = frozen_dir / "data" / "derived" / ANALYSIS_INPUT_NAME
    subcode_path = frozen_dir / "data" / "derived" / SUBCODE_INPUT_NAME
    required_paths = (analysis_path, subcode_path, ledger_path)
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise ExtractionError(f"Missing frozen reference input(s): {missing}")
    return {
        "source_paths": {str(year): str(source_paths[year]) for year in years},
        "source_sha256_by_year": {
            str(year): file_sha256(source_paths[year]) for year in years
        },
        "frozen_analysis_sha256": file_sha256(analysis_path),
        "frozen_subcode_sha256": file_sha256(subcode_path),
        "ledger_sha256": file_sha256(ledger_path),
        "source_paths_by_year_obj": source_paths,
        "analysis_path": analysis_path,
        "subcode_path": subcode_path,
    }


def write_gzip_csv(path: Path) -> tuple[TextIO, csv.DictWriter, Any]:
    raw = path.open("wb")
    compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    text = io.TextIOWrapper(compressed, encoding="utf-8", newline="")
    writer = csv.DictWriter(text, fieldnames=CANDIDATE_FIELDS, lineterminator="\n")
    writer.writeheader()
    return text, writer, raw


def run_extraction(
    years: list[int],
    raw_dir: Path,
    frozen_dir: Path,
    ledger_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    years = sorted(years)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem(years)
    final_paths = current_run_paths(output_dir, stem)
    parser_hash = file_sha256(Path(__file__).resolve())
    inputs = hash_inputs(years, raw_dir, frozen_dir, ledger_path)
    identity = {
        "years": years,
        "source_sha256_by_year": inputs["source_sha256_by_year"],
        "frozen_analysis_sha256": inputs["frozen_analysis_sha256"],
        "frozen_subcode_sha256": inputs["frozen_subcode_sha256"],
        "ledger_sha256": inputs["ledger_sha256"],
        "parser_sha256": parser_hash,
    }
    run_key = stable_sha256(identity)
    if can_reuse(final_paths, run_key):
        with final_paths["summary"].open("r", encoding="utf-8") as source:
            reused = json.load(source)
        reused["status"] = "reused_validated_output"
        reused["output_paths"] = {
            name: str(path) for name, path in final_paths.items() if path.exists()
        }
        return reused

    # An invalid/stale prior run must never leave a completion marker or tables
    # that appear to belong to the current attempt.
    for path in final_paths.values():
        if path.exists():
            path.unlink()

    ledger = load_ledger(ledger_path, years)
    frozen_strata, strata_duplicates, frozen_strata_rows = load_frozen_table(
        inputs["analysis_path"], years, "strata"
    )
    frozen_components, component_duplicates, frozen_component_rows = load_frozen_table(
        inputs["subcode_path"], years, "components"
    )

    strata_counts, component_counts = initialize_counts(years)
    source_audits: dict[str, Any] = {}
    raw_sha_mismatches: list[dict[str, Any]] = []
    ledger_count_audit: dict[str, Any] = {}
    for year in years:
        ledger_row = ledger.get(year)
        observed_hash = inputs["source_sha256_by_year"][str(year)].lower()
        expected_hash = (ledger_row or {}).get("raw_sha256", "").lower()
        if not ledger_row or not expected_hash:
            raw_sha_mismatches.append(
                mismatch_row("ledger_raw_sha256_missing", ["BR", year], "raw_sha256", "present", "missing")
            )
        elif observed_hash != expected_hash:
            raw_sha_mismatches.append(
                mismatch_row("ledger_raw_sha256", ["BR", year], "raw_sha256", expected_hash, observed_hash)
            )
        ledger_count_audit[str(year)] = {
            "ledger_row_present": bool(ledger_row),
            "raw_sha256_expected": expected_hash or None,
            "raw_sha256_observed": observed_hash,
            "raw_sha256_match": bool(expected_hash and observed_hash == expected_hash),
            "eligible_records_ledger_reference": (ledger_row or {}).get("eligible_records") or None,
            "record_rows_ledger_reference": (ledger_row or {}).get("record_rows") or None,
        }

    processing_error: str | None = None
    with tempfile.TemporaryDirectory(prefix=f".br-{stem}-", dir=output_dir) as temp_name:
        temp_dir = Path(temp_name)
        temp_paths = current_run_paths(temp_dir, stem)
        candidate_text: TextIO | None = None
        candidate_raw: Any = None
        try:
            candidate_text, candidate_writer, candidate_raw = write_gzip_csv(temp_paths["candidate"])
            for year in years:
                source_path = inputs["source_paths_by_year_obj"][year]
                source_audits[str(year)] = process_source_year(
                    year,
                    source_path,
                    candidate_writer,
                    strata_counts,
                    component_counts,
                )
            candidate_text.flush()
        except Exception as exc:  # persist a concise diagnostic; never mark a failed run complete
            processing_error = f"{type(exc).__name__}: {exc}"
        finally:
            if candidate_text is not None:
                candidate_text.close()
            if candidate_raw is not None:
                candidate_raw.close()

        mismatches = list(raw_sha_mismatches)
        if processing_error:
            mismatches.append(
                mismatch_row("processing_error", years, "error", "successful EOF parse", processing_error)
            )
            if temp_paths["candidate"].exists():
                temp_paths["candidate"].unlink()
        else:
            mismatches.extend(
                compare_frozen(
                    years,
                    strata_counts,
                    component_counts,
                    frozen_strata,
                    frozen_components,
                    strata_duplicates,
                    component_duplicates,
                    frozen_strata_rows,
                    frozen_component_rows,
                )
            )
            strata_rows, component_rows = output_rows(years, strata_counts, component_counts)
            write_csv(
                temp_paths["strata"],
                ("country", "year", "age_group", "sex", "ucd", "any_common", "alternative"),
                strata_rows,
            )
            write_csv(
                temp_paths["components"],
                ("country", "year", "age_group", "sex", "subcode", "ucd", "any_common"),
                component_rows,
            )
        write_csv(
            temp_paths["mismatches"],
            ("kind", "key", "field", "expected", "observed"),
            mismatches,
        )

        gate_passed = processing_error is None and not mismatches
        artifact_keys = ["mismatches"]
        if not processing_error:
            artifact_keys = ["candidate", "strata", "components", "mismatches"]
        artifact_sha256 = {
            temp_paths[name].name: file_sha256(temp_paths[name]) for name in artifact_keys
        }
        ledger_count_comparison: dict[str, Any] = {}
        for year in years:
            audit = source_audits.get(str(year), {})
            expected_eligible = ledger_count_audit[str(year)]["eligible_records_ledger_reference"]
            observed_eligible = audit.get("analysis_eligible_records")
            ledger_count_comparison[str(year)] = {
                "eligible_records_ledger_reference": expected_eligible,
                "analysis_eligible_records_rebuilt": observed_eligible,
                "match": (
                    str(expected_eligible) == str(observed_eligible)
                    if expected_eligible is not None and observed_eligible is not None
                    else None
                ),
                "use_for_gate": False,
                "note": "Diagnostic only; no counts were altered to fit the prior ledger.",
            }
        output_names = ["mismatches", "summary"]
        if not processing_error:
            output_names = ["candidate", "strata", "components", "mismatches", "summary"]
        if gate_passed:
            output_names.append("complete")
        summary = {
            "status": "complete" if gate_passed else "failed_gate",
            "country": "BR",
            "years": years,
            "run_key": run_key,
            "gate_passed": gate_passed,
            "gate_fields": {
                "analysis_input_936.csv": ["ucd", "any_common", "alternative"],
                "subcode_analysis_input_3744.csv": ["ucd", "any_common"],
                "denominators": "Not rebuilt from death records; frozen population values are outside this extraction gate.",
            },
            "source_identity": {
                "raw_paths": inputs["source_paths"],
                "raw_sha256_by_year": inputs["source_sha256_by_year"],
                "frozen_analysis_path": str(inputs["analysis_path"]),
                "frozen_analysis_sha256": inputs["frozen_analysis_sha256"],
                "frozen_subcode_path": str(inputs["subcode_path"]),
                "frozen_subcode_sha256": inputs["frozen_subcode_sha256"],
                "ledger_path": str(ledger_path),
                "ledger_sha256": inputs["ledger_sha256"],
                "raw_ledger_audit": ledger_count_audit,
                "ledger_eligible_count_comparison_audit_only": ledger_count_comparison,
            },
            "parser_sha256": parser_hash,
            "input_identity_sha256": stable_sha256(identity),
            "parsing_contract": {
                "encoding": "latin-1",
                "delimiter": ";",
                "missing_tokens": sorted(MISSING_TOKENS),
                "tipobito": "Exclude missing/NA-like values and exact value 1; retain other nonmissing values.",
                "codmunres": "Require only nonmissing/nonempty; no code or geography validation.",
                "age": "IDADE must be exactly three digits; units 1/2/3 -> 0, 4 -> value, 5 -> 100+value; all other values remain unknown.",
                "sex": "1/M -> M; 2/F -> F; other values remain unknown.",
                "dto_year": "Audit DTOBITO characters 5-8 (Python slice [4:8]) against source year; never overwrite source year.",
                "ucd": "CAUSABAS uppercase alphanumeric normalization; minimum three characters; K80-K83 prefix.",
                "line_biliary_regex": BILIARY_RE.pattern,
                "alternative": "Equal to any_common for Brazil.",
                "candidate_boolean_encoding": "0/1",
                "identifier_policy": "No identifiers, full source lines, or ATESTADO free text are emitted.",
            },
            "source_audit_by_year": source_audits,
            "processing_error": processing_error,
            "frozen_rows_selected": {
                "analysis_input_936": frozen_strata_rows,
                "subcode_analysis_input_3744": frozen_component_rows,
            },
            "expected_output_rows": {
                "strata": len(years) * len(AGE_GROUPS) * len(SEXES),
                "components": len(years) * len(AGE_GROUPS) * len(SEXES) * len(COMPONENTS),
            },
            "mismatch_count": len(mismatches),
            "mismatch_kinds": dict(sorted(Counter(row["kind"] for row in mismatches).items())),
            "artifact_sha256": artifact_sha256,
            "output_paths": {name: str(final_paths[name]) for name in output_names},
        }
        temp_paths["summary"].write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if gate_passed:
            summary_hash = file_sha256(temp_paths["summary"])
            temp_paths["complete"].write_text(
                f"gate=PASS\nrun_key={run_key}\nsummary_sha256={summary_hash}\n",
                encoding="utf-8",
            )

        for name in ("candidate", "strata", "components", "mismatches", "summary", "complete"):
            temp_path = temp_paths[name]
            if temp_path.exists():
                os.replace(temp_path, final_paths[name])

    summary["output_paths"] = {
        name: str(path) for name, path in final_paths.items() if path.exists()
    }
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        type=parse_years,
        default=[2019],
        help="Selected years, e.g. 2019 or 2012-2024 (default: 2019).",
    )
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--frozen-dir", type=Path, default=DEFAULT_FROZEN_DIR)
    parser.add_argument("--ledger-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    ledger_path = args.ledger_path or args.frozen_dir / LEDGER_RELATIVE_PATH
    try:
        summary = run_extraction(
            args.years,
            args.raw_dir,
            args.frozen_dir,
            ledger_path,
            args.output_dir,
        )
    except Exception as exc:
        print(f"BRAZIL extraction failed before artifact generation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": summary.get("status"),
                "gate_passed": summary.get("gate_passed"),
                "years": summary.get("years"),
                "mismatch_count": summary.get("mismatch_count"),
                "output_paths": summary.get("output_paths"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
