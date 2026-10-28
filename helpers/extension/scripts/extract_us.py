#!/usr/bin/env python3
"""Rebuild US multiple-cause counts and biliary record features from CDC WONDER ZIPs.

The source member is streamed from the ZIP without extraction. Only de-identified
fields needed for the requested aggregate and composition analyses are retained.
"""

from __future__ import annotations

import argparse
import binascii
import csv
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(r"D:\数据库集合\cdcwonder数据库\raw")
FROZEN_ROOT = Path(r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build\data")
FROZEN_936 = FROZEN_ROOT / "derived" / "analysis_input_936.csv"
FROZEN_3744 = FROZEN_ROOT / "derived" / "subcode_analysis_input_3744.csv"
LEDGER_PATH = FROZEN_ROOT / "source_data" / "raw_scan_cache_ledger.csv"
OUTPUT_DIR = ROOT / "outputs" / "us"
SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")

AGE_GROUPS = (
    "0-4", "5-9", "10-14", "15-19", "20-24", "25-29", "30-34",
    "35-39", "40-44", "45-49", "50-54", "55-59", "60-64", "65-69",
    "70-74", "75-79", "80-84", "85+",
)
SEXES = ("F", "M")
SUBCODES = ("K80", "K81", "K82", "K83")

# Offsets in the source specification are one-based and inclusive.
MIN_RECORD_WIDTH = 443
RESIDENT_SLICE = slice(19, 20)
SEX_SLICE = slice(68, 69)
AGE_SLICE = slice(69, 73)
YEAR_SLICE = slice(101, 105)
UCD_SLICE = slice(145, 149)
ENTITY_SLICE = slice(164, 304)
RECORD_SLICE = slice(343, 443)
ENTITY_SLOT_WIDTH = 7
RECORD_SLOT_WIDTH = 5
PYTHON_ZIP_METHODS = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
}

RETAINED_FIELDS = (
    "country", "year", "source_row", "age_group", "sex", "ucd_code",
    "ucd_biliary", "any_common", "alternative", "part_i_biliary",
    "part_ii_biliary", "entity_codes", "record_codes",
    "entity_biliary_codes", "record_biliary_codes", "position_codes",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(field: bytes) -> str:
    # The selected CDC WONDER fields are ASCII; replacement keeps auditing
    # resilient to an unexpected byte without ever retaining the full line.
    return field.decode("ascii", errors="replace")


def _age_group_from_years(age: int) -> str:
    if age >= 85:
        return "85+"
    lower = (age // 5) * 5
    return f"{lower}-{lower + 4}"


def parse_age(age_field: str) -> tuple[str | None, str]:
    """Return WHO18 age group and an auditable source-unit classification."""
    value = (age_field or "").strip()
    if not value:
        return None, "blank"
    if len(value) != 4:
        return None, "invalid_width"

    unit, magnitude = value[0], value[1:]
    if unit == "1":
        if magnitude == "999":
            return None, "unit_1_unknown_999"
        if not magnitude.isdigit():
            return None, "unit_1_invalid_value"
        return _age_group_from_years(int(magnitude)), "unit_1_years"
    if unit in {"2", "4", "5", "6"}:
        return "0-4", f"unit_{unit}_mapped_zero"
    if unit == "3":
        return None, "unexpected_unit_3"
    return None, f"unexpected_unit_{unit}"


def parse_sex(sex_field: str) -> str | None:
    value = (sex_field or "").strip().upper()
    if value in {"1", "M"}:
        return "M"
    if value in {"2", "F"}:
        return "F"
    return None


def parse_entity_mentions(entity_field: bytes) -> list[dict[str, Any]]:
    """Parse the 20 seven-character entity slots, preserving each position."""
    mentions: list[dict[str, Any]] = []
    for zero_slot in range(20):
        start = zero_slot * ENTITY_SLOT_WIDTH
        slot = entity_field[start : start + ENTITY_SLOT_WIDTH]
        if len(slot) < ENTITY_SLOT_WIDTH:
            slot = slot.ljust(ENTITY_SLOT_WIDTH, b" ")
        part = _decode(slot[0:1]).strip()
        sequence = _decode(slot[1:2]).strip()
        code = _decode(slot[2:6]).strip().upper()
        if code:
            mentions.append({
                "axis": "entity",
                "slot": zero_slot + 1,
                "part": part,
                "sequence": sequence,
                "certificate_part": (
                    "I" if part in {"1", "2", "3", "4", "5"}
                    else "II" if part == "6"
                    else ""
                ),
                "certificate_line": part if part in {"1", "2", "3", "4", "5"} else "",
                "code": code,
            })
    return mentions


def parse_record_mentions(record_field: bytes) -> list[dict[str, Any]]:
    """Parse the 20 five-character record-axis slots, preserving each slot."""
    mentions: list[dict[str, Any]] = []
    for zero_slot in range(20):
        start = zero_slot * RECORD_SLOT_WIDTH
        slot = record_field[start : start + RECORD_SLOT_WIDTH]
        if len(slot) < RECORD_SLOT_WIDTH:
            slot = slot.ljust(RECORD_SLOT_WIDTH, b" ")
        code = _decode(slot[:4]).strip().upper()
        if code:
            mentions.append({
                "axis": "record",
                "slot": zero_slot + 1,
                "part": None,
                "sequence": None,
                "certificate_part": "",
                "certificate_line": "",
                "code": code,
            })
    return mentions


def is_biliary_code(code: str) -> bool:
    return len(code) >= 3 and code[:3] in SUBCODES


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_us_line(raw_line: bytes, year: int, source_row: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Parse only contract fields from one raw row; return feature row and audit."""
    if raw_line.endswith(b"\r\n"):
        body = raw_line[:-2]
    elif raw_line.endswith(b"\n"):
        body = raw_line[:-1]
    elif raw_line.endswith(b"\r"):
        body = raw_line[:-1]
    else:
        body = raw_line

    audit: dict[str, Any] = {"line_width": len(body), "eligible": False}
    if len(body) < MIN_RECORD_WIDTH:
        audit.update({"parse_status": "short_record", "age_status": "not_parsed"})
        return None, audit

    resident = _decode(body[RESIDENT_SLICE]).strip()
    sex_raw = _decode(body[SEX_SLICE]).strip().upper()
    age_raw = _decode(body[AGE_SLICE])
    year_raw = _decode(body[YEAR_SLICE]).strip()
    ucd_code = _decode(body[UCD_SLICE]).strip().upper()
    resident_valid = resident in {"1", "2", "3"}
    age_group, age_status = parse_age(age_raw)
    sex = parse_sex(sex_raw)

    audit.update({
        "parse_status": "parsed",
        "resident_raw": resident or "<blank>",
        "resident_valid": resident_valid,
        "sex_raw": sex_raw or "<blank>",
        "sex_valid": sex is not None,
        "age_status": age_status,
        "year_raw": year_raw or "<blank>",
        "year_matches_source": year_raw == str(year),
        "year_field_numeric": len(year_raw) == 4 and year_raw.isdigit(),
        "eligible": resident_valid and age_group is not None and sex is not None,
    })
    if not audit["eligible"]:
        return None, audit

    entity_mentions = parse_entity_mentions(body[ENTITY_SLICE])
    record_mentions = parse_record_mentions(body[RECORD_SLICE])
    entity_codes = _ordered_unique(item["code"] for item in entity_mentions)
    record_codes = _ordered_unique(item["code"] for item in record_mentions)
    entity_biliary_codes = [code for code in entity_codes if is_biliary_code(code)]
    record_biliary_codes = [code for code in record_codes if is_biliary_code(code)]
    part_i_biliary = _ordered_unique(
        item["code"] for item in entity_mentions
        if item["part"] in {"1", "2", "3", "4", "5"} and is_biliary_code(item["code"])
    )
    part_ii_biliary = _ordered_unique(
        item["code"] for item in entity_mentions
        if item["part"] == "6" and is_biliary_code(item["code"])
    )
    ucd_biliary = is_biliary_code(ucd_code)
    any_common = ucd_biliary or bool(entity_biliary_codes)
    alternative = ucd_biliary or bool(record_biliary_codes)

    all_mentions = entity_mentions + record_mentions
    entity_prefixes = {code[:3] for code in entity_biliary_codes}
    record_prefixes = {code[:3] for code in record_biliary_codes}
    ucd_prefix = ucd_code[:3] if ucd_biliary else None

    row = {
        "country": "US",
        "year": year,
        "source_row": source_row,
        "age_group": age_group,
        "sex": sex,
        "ucd_code": ucd_code,
        "ucd_biliary": int(ucd_biliary),
        "any_common": int(any_common),
        "alternative": int(alternative),
        "part_i_biliary": _json_cell(part_i_biliary),
        "part_ii_biliary": _json_cell(part_ii_biliary),
        "entity_codes": _json_cell(entity_codes),
        "record_codes": _json_cell(record_codes),
        "entity_biliary_codes": _json_cell(entity_biliary_codes),
        "record_biliary_codes": _json_cell(record_biliary_codes),
        "position_codes": _json_cell(all_mentions),
        "_ucd_prefix": ucd_prefix,
        "_entity_prefixes": entity_prefixes,
        "_record_prefixes": record_prefixes,
        "_part_i_present": bool(part_i_biliary),
        "_part_ii_present": bool(part_ii_biliary),
    }
    audit["eligible"] = True
    audit["entity_invalid_part_mentions"] = sum(
        1 for item in entity_mentions if item["part"] not in {"1", "2", "3", "4", "5", "6"}
    )
    return row, audit


def _iter_year_tokens(values: list[str]) -> list[int]:
    years: list[int] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            match = re.fullmatch(r"(\d{4})-(\d{4})", token)
            if match:
                start, end = (int(part) for part in match.groups())
                if end < start:
                    raise ValueError(f"descending year range: {token}")
                years.extend(range(start, end + 1))
            elif re.fullmatch(r"\d{4}", token):
                years.append(int(token))
            else:
                raise ValueError(f"invalid year token: {token}")
    unique_years = list(dict.fromkeys(years))
    if not unique_years:
        raise ValueError("at least one year is required")
    invalid = [year for year in unique_years if year < 2012 or year > 2024]
    if invalid:
        raise ValueError(f"supported source years are 2012-2024; invalid: {invalid}")
    return unique_years


def load_ledger_row(year: int) -> dict[str, str]:
    with LEDGER_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("country") == "US" and row.get("year") == str(year)
        ]
    if len(rows) != 1:
        raise ValueError(f"old raw-scan ledger must contain exactly one US/{year} row; found {len(rows)}")
    return rows[0]


def load_frozen_rows(year: int) -> tuple[dict[tuple[str, str], dict[str, str]], dict[tuple[str, str, str], dict[str, str]]]:
    main: dict[tuple[str, str], dict[str, str]] = {}
    sub: dict[tuple[str, str, str], dict[str, str]] = {}
    with FROZEN_936.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("country") == "US" and row.get("year") == str(year):
                key = (row["age_group"], row["sex"])
                if key in main:
                    raise ValueError(f"duplicate frozen 936 stratum for US/{year}: {key}")
                main[key] = row
    with FROZEN_3744.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("country") == "US" and row.get("year") == str(year):
                key = (row["age_group"], row["sex"], row["subcode"])
                if key in sub:
                    raise ValueError(f"duplicate frozen 3744 stratum for US/{year}: {key}")
                sub[key] = row
    if len(main) != len(AGE_GROUPS) * len(SEXES):
        raise ValueError(f"expected 36 frozen US/{year} strata, found {len(main)}")
    if len(sub) != len(AGE_GROUPS) * len(SEXES) * len(SUBCODES):
        raise ValueError(f"expected 144 frozen US/{year} subcode strata, found {len(sub)}")
    return main, sub


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_input_manifest(year: int, source_sha256: str) -> dict[str, Any]:
    return {
        "country": "US",
        "year": year,
        "source_archive_sha256": source_sha256,
        "ledger_file_sha256": sha256_file(LEDGER_PATH),
        "frozen_936_sha256": sha256_file(FROZEN_936),
        "frozen_3744_sha256": sha256_file(FROZEN_3744),
    }


def _open_gzip_csv(path: Path) -> tuple[io.TextIOWrapper, Any]:
    raw = path.open("wb")
    zipped = gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=6)
    text = io.TextIOWrapper(zipped, encoding="utf-8", newline="")
    return text, raw


@contextmanager
def open_member_stream(
    archive_path: Path,
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    force_7zip: bool = False,
):
    """Yield one member stream, using 7-Zip only for unsupported methods.

    The 7-Zip path writes decompressed bytes to stdout and never extracts a
    member to disk. Its process must finish with exit status zero.
    """
    if not force_7zip and member.compress_type in PYTHON_ZIP_METHODS:
        with archive.open(member, "r") as source:
            yield source, "python_zipfile"
        return

    if not SEVEN_ZIP.is_file():
        raise FileNotFoundError(f"7-Zip fallback unavailable at {SEVEN_ZIP}")
    command = [str(SEVEN_ZIP), "e", "-so", str(archive_path), member.filename]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024)
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("7-Zip did not provide a member output stream")
    try:
        yield process.stdout, "7zip_stream"
    finally:
        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"7-Zip member streaming failed with exit code {return_code}; exit code 0 is required")


def scan_archive(archive_path: Path, year: int, retained_path: Path) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, int]], dict[tuple[str, str, str], dict[str, int]]]:
    """Stream one ZIP member through EOF, letting zipfile verify its CRC."""
    main_counts: dict[tuple[str, str], dict[str, int]] = {
        (age, sex): {"ucd": 0, "any_common": 0, "alternative": 0}
        for age in AGE_GROUPS for sex in SEXES
    }
    sub_counts: dict[tuple[str, str, str], dict[str, int]] = {
        (age, sex, subcode): {"ucd": 0, "any_common": 0}
        for age in AGE_GROUPS for sex in SEXES for subcode in SUBCODES
    }
    audit_counts: dict[str, Counter[str]] = {
        "age_status": Counter(), "sex_raw": Counter(), "resident_raw": Counter(),
        "year_field": Counter(), "line_width": Counter(),
    }
    numeric_audit = Counter()
    position_summary = Counter()
    source_year_mismatches = 0
    eligible_records = 0
    retained_records = 0
    entity_record_flag_disagreements = 0
    entity_record_codeset_disagreements = 0
    entity_record_symmetric_difference_codes = 0
    invalid_entity_parts = 0
    short_records = 0
    observed_bytes = 0
    observed_crc32 = 0

    text_out, raw_out = _open_gzip_csv(retained_path)
    writer = csv.DictWriter(text_out, fieldnames=RETAINED_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise ValueError(f"expected exactly one non-directory ZIP member; found {len(members)}")
            member = members[0]
            crc32 = f"{member.CRC:08x}"
            member_metadata = {
                "name": member.filename,
                "file_size": member.file_size,
                "compressed_size": member.compress_size,
                "crc32_expected": crc32,
                "compression_method": member.compress_type,
                "date_time": list(member.date_time),
            }
            eof_reached = False
            with open_member_stream(archive_path, archive, member) as (source, backend):
                for source_row, raw_line in enumerate(source, start=1):
                    observed_bytes += len(raw_line)
                    observed_crc32 = binascii.crc32(raw_line, observed_crc32)
                    body = raw_line[:-2] if raw_line.endswith(b"\r\n") else raw_line[:-1] if raw_line.endswith((b"\n", b"\r")) else raw_line
                    audit_counts["line_width"][str(len(body))] += 1
                    parsed, audit = parse_us_line(raw_line, year, source_row)
                    if audit["parse_status"] == "short_record":
                        short_records += 1
                        numeric_audit["short_records"] += 1
                        continue
                    audit_counts["resident_raw"][str(audit["resident_raw"])] += 1
                    audit_counts["sex_raw"][str(audit["sex_raw"])] += 1
                    audit_counts["age_status"][str(audit["age_status"])] += 1
                    audit_counts["year_field"][str(audit["year_raw"])] += 1
                    if not audit["year_matches_source"]:
                        source_year_mismatches += 1
                        numeric_audit["source_year_mismatches"] += 1
                    if not audit["year_field_numeric"]:
                        numeric_audit["source_year_non_numeric_or_blank"] += 1
                    if not audit["resident_valid"]:
                        numeric_audit["resident_invalid"] += 1
                    if not audit["sex_valid"]:
                        numeric_audit["sex_invalid"] += 1
                    if audit["age_status"] == "unexpected_unit_3":
                        numeric_audit["age_unit_3_unexpected"] += 1
                    elif audit["age_status"].startswith("unexpected_unit_"):
                        numeric_audit["age_unit_other_unexpected"] += 1
                    if not audit["eligible"]:
                        continue

                    eligible_records += 1
                    invalid_entity_parts += int(audit.get("entity_invalid_part_mentions", 0))
                    numeric_audit["eligible_records"] += 1
                    age_group = parsed["age_group"]
                    sex = parsed["sex"]
                    key = (age_group, sex)
                    if parsed["ucd_biliary"]:
                        main_counts[key]["ucd"] += 1
                    if parsed["any_common"]:
                        main_counts[key]["any_common"] += 1
                    if parsed["alternative"]:
                        main_counts[key]["alternative"] += 1

                    ucd_prefix = parsed["_ucd_prefix"]
                    entity_prefixes = parsed["_entity_prefixes"]
                    for subcode in SUBCODES:
                        sub_key = (age_group, sex, subcode)
                        if ucd_prefix == subcode:
                            sub_counts[sub_key]["ucd"] += 1
                        if ucd_prefix == subcode or subcode in entity_prefixes:
                            sub_counts[sub_key]["any_common"] += 1

                    entity_present = bool(entity_prefixes)
                    record_prefixes = parsed["_record_prefixes"]
                    record_present = bool(record_prefixes)
                    if entity_present != record_present:
                        entity_record_flag_disagreements += 1
                    if entity_prefixes != record_prefixes:
                        entity_record_codeset_disagreements += 1
                        entity_record_symmetric_difference_codes += len(entity_prefixes.symmetric_difference(record_prefixes))

                    if parsed["any_common"] or parsed["alternative"] or parsed["ucd_biliary"]:
                        if parsed["_part_i_present"] and parsed["_part_ii_present"]:
                            position_summary["entity_part_i_and_ii"] += 1
                        elif parsed["_part_i_present"]:
                            position_summary["entity_part_i_only"] += 1
                        elif parsed["_part_ii_present"]:
                            position_summary["entity_part_ii_only"] += 1
                        else:
                            position_summary["no_entity_part_i_or_ii_biliary"] += 1
                        if parsed["ucd_biliary"] and not entity_present and not record_present:
                            position_summary["ucd_only_not_captured_on_either_axis"] += 1
                        if parsed["ucd_biliary"]:
                            position_summary["ucd_biliary"] += 1
                        else:
                            position_summary["non_biliary_ucd"] += 1
                        if parsed["any_common"] and not parsed["alternative"]:
                            position_summary["any_common_only"] += 1
                        elif parsed["alternative"] and not parsed["any_common"]:
                            position_summary["alternative_only"] += 1
                        elif parsed["any_common"] and parsed["alternative"]:
                            position_summary["both_any_and_alternative"] += 1
                        output_row = {field: parsed[field] for field in RETAINED_FIELDS}
                        writer.writerow(output_row)
                        retained_records += 1

                # Exhausting ZipExtFile through iteration checks member CRC.
                eof_reached = True
            observed_crc32 &= 0xFFFFFFFF
            if observed_bytes != member.file_size:
                raise zipfile.BadZipFile(
                    f"member byte count mismatch: observed {observed_bytes}, expected {member.file_size}"
                )
            if observed_crc32 != member.CRC:
                raise zipfile.BadZipFile(
                    f"member CRC32 mismatch: observed {observed_crc32:08x}, expected {member.CRC:08x}"
                )
            if not eof_reached:
                raise zipfile.BadZipFile("ZIP member was not read through EOF")
            member_metadata["crc32_verified"] = True
            member_metadata["crc32_observed"] = f"{observed_crc32:08x}"
            member_metadata["decompression_backend"] = backend
    finally:
        text_out.close()
        raw_out.close()

    scan_metadata = {
        "member": member_metadata,
        "zip_crc_verified": True,
        "member_eof_reached": True,
        "member_uncompressed_bytes_read": observed_bytes,
        "record_rows_scanned": sum(audit_counts["line_width"].values()),
        "eligible_records": eligible_records,
        "retained_biliary_records": retained_records,
        "short_records": short_records,
        "source_year_mismatches": source_year_mismatches,
        "age_parse_status_counts": dict(sorted(audit_counts["age_status"].items())),
        "sex_raw_counts": dict(sorted(audit_counts["sex_raw"].items())),
        "resident_raw_counts": dict(sorted(audit_counts["resident_raw"].items())),
        "source_year_field_counts": dict(sorted(audit_counts["year_field"].items())),
        "record_width_counts": dict(sorted(audit_counts["line_width"].items(), key=lambda item: int(item[0]))),
        "audit_counts": dict(sorted(numeric_audit.items())),
        "entity_invalid_part_mentions": invalid_entity_parts,
        "entity_record_flag_disagreement_records": entity_record_flag_disagreements,
        "entity_record_codeset_disagreement_records": entity_record_codeset_disagreements,
        "entity_record_symmetric_difference_codes": entity_record_symmetric_difference_codes,
        "retained_position_class_counts": dict(sorted(position_summary.items())),
    }
    return scan_metadata, main_counts, sub_counts


def make_comparisons(
    year: int,
    observed_main: dict[tuple[str, str], dict[str, int]],
    observed_sub: dict[tuple[str, str, str], dict[str, int]],
    expected_main: dict[tuple[str, str], dict[str, str]],
    expected_sub: dict[tuple[str, str, str], dict[str, str]],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for age_group in AGE_GROUPS:
        for sex in SEXES:
            key = (age_group, sex)
            for measure in ("ucd", "any_common", "alternative"):
                expected = int(expected_main[key][measure])
                observed = int(observed_main[key][measure])
                if observed != expected:
                    differences.append({
                        "comparison_type": "main_36_strata",
                        "country": "US", "year": year, "age_group": age_group,
                        "sex": sex, "subcode": "", "measure": measure,
                        "expected_count": expected, "observed_count": observed,
                        "difference": observed - expected,
                    })
            for subcode in SUBCODES:
                sub_key = (age_group, sex, subcode)
                for measure in ("ucd", "any_common"):
                    expected = int(expected_sub[sub_key][measure])
                    observed = int(observed_sub[sub_key][measure])
                    if observed != expected:
                        differences.append({
                            "comparison_type": "subcode_144_strata",
                            "country": "US", "year": year, "age_group": age_group,
                            "sex": sex, "subcode": subcode, "measure": measure,
                            "expected_count": expected, "observed_count": observed,
                            "difference": observed - expected,
                        })
    return differences


def write_mismatch_csv(path: Path, differences: list[dict[str, Any]]) -> None:
    fields = (
        "comparison_type", "country", "year", "age_group", "sex", "subcode",
        "measure", "expected_count", "observed_count", "difference",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(differences)


def write_count_csvs(
    main_path: Path,
    sub_path: Path,
    year: int,
    main_counts: dict[tuple[str, str], dict[str, int]],
    sub_counts: dict[tuple[str, str, str], dict[str, int]],
    expected_main: dict[tuple[str, str], dict[str, str]],
    expected_sub: dict[tuple[str, str, str], dict[str, str]],
) -> None:
    main_fields = ("country", "year", "age_group", "sex", "population", "ucd", "any_common", "alternative")
    with main_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=main_fields, lineterminator="\n")
        writer.writeheader()
        for age_group in AGE_GROUPS:
            for sex in SEXES:
                key = (age_group, sex)
                writer.writerow({
                    "country": "US", "year": year, "age_group": age_group, "sex": sex,
                    "population": expected_main[key]["population"],
                    "ucd": main_counts[key]["ucd"],
                    "any_common": main_counts[key]["any_common"],
                    "alternative": main_counts[key]["alternative"],
                })

    sub_fields = ("country", "year", "age_group", "sex", "subcode", "ucd", "any_common", "population")
    with sub_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sub_fields, lineterminator="\n")
        writer.writeheader()
        for age_group in AGE_GROUPS:
            for sex in SEXES:
                for subcode in SUBCODES:
                    key = (age_group, sex, subcode)
                    writer.writerow({
                        "country": "US", "year": year, "age_group": age_group, "sex": sex,
                        "subcode": subcode,
                        "ucd": sub_counts[key]["ucd"],
                        "any_common": sub_counts[key]["any_common"],
                        "population": expected_sub[key]["population"],
                    })


def _try_validated_reuse(summary_path: Path, expected_input_sha: str, parser_sha: str) -> dict[str, Any] | None:
    if not summary_path.exists():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") != "complete" or summary.get("exact_frozen_gate_pass") is not True:
            return None
        if summary.get("input_sha256") != expected_input_sha or summary.get("parser_sha256") != parser_sha:
            return None
        if summary.get("zip_crc_verified") is not True:
            return None
        outputs = summary.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            return None
        for name, expected_hash in outputs.items():
            path = summary_path.parent / name
            if not path.is_file() or sha256_file(path) != expected_hash:
                return None
        return summary
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def run_year(year: int, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = RAW_DIR / f"mort{year}us.zip"
    if not archive_path.is_file():
        raise FileNotFoundError(f"missing source archive: {archive_path}")

    ledger = load_ledger_row(year)
    source_sha256 = sha256_file(archive_path)
    ledger_source_sha256 = ledger.get("raw_sha256", "").strip().lower()
    if source_sha256.lower() != ledger_source_sha256:
        raise ValueError(
            f"source SHA256 does not match old ledger for US/{year}; "
            f"expected {ledger_source_sha256}, observed {source_sha256}; scan not started"
        )

    expected_main, expected_sub = load_frozen_rows(year)
    input_manifest = build_input_manifest(year, source_sha256)
    input_sha256 = _json_sha256(input_manifest)
    parser_sha256 = sha256_file(Path(__file__).resolve())
    stem = f"us_{year}_{source_sha256[:12]}_{parser_sha256[:12]}"
    count_name = f"{stem}_counts.csv"
    sub_name = f"{stem}_subcode_counts.csv"
    retained_name = f"{stem}_retained.csv.gz"
    mismatch_name = f"{stem}_mismatch.csv"
    summary_name = f"{stem}_summary.json"
    count_path = output_dir / count_name
    sub_path = output_dir / sub_name
    retained_path = output_dir / retained_name
    mismatch_path = output_dir / mismatch_name
    summary_path = output_dir / summary_name

    reusable = _try_validated_reuse(summary_path, input_sha256, parser_sha256)
    if reusable is not None:
        return {"status": "reused", "year": year, "summary": str(summary_path), "summary_data": reusable}

    with tempfile.TemporaryDirectory(prefix=f".us_{year}_", dir=output_dir) as temp_name:
        temp_dir = Path(temp_name)
        staged_retained = temp_dir / retained_name
        scan_metadata, main_counts, sub_counts = scan_archive(archive_path, year, staged_retained)
        differences = make_comparisons(year, main_counts, sub_counts, expected_main, expected_sub)
        staged_mismatch = temp_dir / mismatch_name
        write_mismatch_csv(staged_mismatch, differences)
        gate_pass = not differences

        if not gate_pass:
            # Preserve only aggregate cell differences for diagnosis. No record
            # examples, identifiers, or source rows are emitted on a failed gate.
            os.replace(staged_mismatch, mismatch_path)
            return {
                "status": "gate_failed",
                "year": year,
                "source_sha256": source_sha256,
                "parser_sha256": parser_sha256,
                "input_sha256": input_sha256,
                "eligible_records": scan_metadata["eligible_records"],
                "old_ledger_eligible_records": int(ledger["eligible_records"]),
                "record_rows_scanned": scan_metadata["record_rows_scanned"],
                "mismatch_count": len(differences),
                "mismatch_csv": str(mismatch_path),
                "mismatch_preview": differences[:10],
                "zip_crc_verified": scan_metadata["zip_crc_verified"],
            }

        staged_counts = temp_dir / count_name
        staged_sub = temp_dir / sub_name
        write_count_csvs(staged_counts, staged_sub, year, main_counts, sub_counts, expected_main, expected_sub)
        output_hashes = {
            count_name: sha256_file(staged_counts),
            sub_name: sha256_file(staged_sub),
            retained_name: sha256_file(staged_retained),
            mismatch_name: sha256_file(staged_mismatch),
        }
        summary = {
            "status": "complete",
            "country": "US",
            "year": year,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_archive": archive_path.name,
            "source_sha256": source_sha256,
            "ledger_source_sha256": ledger_source_sha256,
            "ledger_parser_sha256": ledger.get("parser_sha256"),
            "old_ledger_eligible_records": int(ledger["eligible_records"]),
            "parser_sha256": parser_sha256,
            "input_sha256": input_sha256,
            "input_manifest": input_manifest,
            "zip_crc_verified": scan_metadata["zip_crc_verified"],
            "member_eof_reached": scan_metadata["member_eof_reached"],
            "member": scan_metadata["member"],
            "record_rows_scanned": scan_metadata["record_rows_scanned"],
            "eligible_records": scan_metadata["eligible_records"],
            "eligible_record_difference_from_ledger": (
                scan_metadata["eligible_records"] - int(ledger["eligible_records"])
            ),
            "retained_biliary_records": scan_metadata["retained_biliary_records"],
            "source_year_field_audit": {
                "mismatched_rows": scan_metadata["source_year_mismatches"],
                "field_value_counts": scan_metadata["source_year_field_counts"],
                "note": "The source year field is audited as stored; it is never used to reassign a row to another year.",
            },
            "age_parse_status_counts": scan_metadata["age_parse_status_counts"],
            "sex_raw_counts": scan_metadata["sex_raw_counts"],
            "resident_raw_counts": scan_metadata["resident_raw_counts"],
            "record_width_counts": scan_metadata["record_width_counts"],
            "audit_counts": scan_metadata["audit_counts"],
            "entity_invalid_part_mentions": scan_metadata["entity_invalid_part_mentions"],
            "entity_record_flag_disagreement_records": scan_metadata["entity_record_flag_disagreement_records"],
            "entity_record_codeset_disagreement_records": scan_metadata["entity_record_codeset_disagreement_records"],
            "entity_record_symmetric_difference_codes": scan_metadata["entity_record_symmetric_difference_codes"],
            "retained_position_class_counts": scan_metadata["retained_position_class_counts"],
            "strata_rows": 36,
            "subcode_rows": 144,
            "frozen_comparisons": 396,
            "frozen_mismatches": 0,
            "exact_frozen_gate_pass": True,
            "outputs": output_hashes,
        }
        staged_summary = temp_dir / summary_name
        _write_json_atomic(staged_summary, summary)

        # The summary is installed last, so a complete marker is never visible
        # before all of its hashed output files are in place.
        os.replace(staged_counts, count_path)
        os.replace(staged_sub, sub_path)
        os.replace(staged_retained, retained_path)
        os.replace(staged_mismatch, mismatch_path)
        os.replace(staged_summary, summary_path)

    return {
        "status": "complete",
        "year": year,
        "summary": str(summary_path),
        "source_sha256": source_sha256,
        "parser_sha256": parser_sha256,
        "input_sha256": input_sha256,
        "eligible_records": summary["eligible_records"],
        "old_ledger_eligible_records": summary["old_ledger_eligible_records"],
        "record_rows_scanned": summary["record_rows_scanned"],
        "retained_biliary_records": summary["retained_biliary_records"],
        "mismatch_count": 0,
        "zip_crc_verified": True,
        "outputs": list(summary["outputs"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years", nargs="+", default=["2019"],
        help="US source years (space/comma separated or ranges such as 2019 or 2012-2024)",
    )
    args = parser.parse_args(argv)
    try:
        years = _iter_year_tokens(args.years)
        for year in years:
            result = run_year(year)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if result["status"] == "gate_failed":
                return 2
        return 0
    except Exception as error:  # CLI boundary: give concise failure without dumping source data.
        print(f"US extraction failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
