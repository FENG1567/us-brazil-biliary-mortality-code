#!/usr/bin/env python3
"""Aggregate-only raw audit of age/sex missingness and residence status.

The scanners deliberately parse K80-K83 UCD/available mentions before applying
the frozen age/sex gate. No row identifiers or record-level data are written.
The source parser modules are imported read-only for their exact age, sex,
cause and streaming rules; this audit adds the missing-demographic branch.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


for _thread_var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_thread_var] = "1"

WORK_DIR = Path(__file__).resolve().parent
EXT_ROOT = Path(r"D:\MCOD_Research\USBR_MCOD_extension_20261021")
EXT_SCRIPTS = EXT_ROOT / "scripts"
BR_RAW_DIR = Path(r"D:\数据库集合\巴西 SIM 数据库\raw")
US_RAW_DIR = Path(r"D:\数据库集合\cdcwonder数据库\raw")
FROZEN_ROOT = Path(r"D:\MCOD_Archive_20260919\2026-08-24\x20\github_repo_build\data")
FROZEN_ANALYSIS = FROZEN_ROOT / "derived" / "analysis_input_936.csv"
FROZEN_WEIGHTS = FROZEN_ROOT / "metadata" / "who_2000_2025_18_group_weights_correct.csv"
FROZEN_LEDGER = FROZEN_ROOT / "source_data" / "raw_scan_cache_ledger.csv"
FROZEN_SOURCE_IDENTITY = EXT_ROOT / "outputs" / "identity" / "source_identity.csv"
PRIOR_TABNET = FROZEN_ROOT / "source_data" / "br_datasus_tabnet_validation_2012_2024.csv"
PRIOR_TABNET_SHA256 = "84fe968df4cce154343d7b3100033863584615adaa3e359964275f5826aa18bf"
PILOT_IBGE_REFERENCE = Path(r"D:\MCOD_Research\USBR_MCOD_upgrade_pilot_20261021\outputs\raw\ibge_municipality_codes_current_20261021.response")
PILOT_IBGE_MANIFEST = Path(r"D:\MCOD_Research\USBR_MCOD_upgrade_pilot_20261021\outputs\raw\m0_m4_raw_pilot_manifest.json")
IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?orderBy=id"
YEARS = tuple(range(2012, 2024))
AGE_GROUPS = tuple(["0-4"] + [f"{lower}-{lower + 4}" for lower in range(5, 85, 5)] + ["85+"])
UNDER75 = AGE_GROUPS[:15]
SEXES = ("F", "M")
MISSING_GROUPS = ("age_only_invalid", "sex_only_invalid", "both_invalid")
UF_PREFIXES = frozenset(
    "11 12 13 14 15 16 17 21 22 23 24 25 26 27 28 29 31 32 33 35 41 42 43 50 51 52 53".split()
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load parser module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BR = load_module("residence_missing_extract_br", EXT_SCRIPTS / "extract_br.py")
US = load_module("residence_missing_extract_us", EXT_SCRIPTS / "extract_us.py")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_utc_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def age_sex_group(age_group: str | None, sex: str | None) -> str:
    if age_group is None and sex is None:
        return "both_invalid"
    if age_group is None:
        return "age_only_invalid"
    if sex is None:
        return "sex_only_invalid"
    return "valid"


def residence_class_br(raw_value: Any, ibge_prefixes: set[str]) -> tuple[str, str]:
    missing, _ = BR.is_missing(raw_value)
    value = "" if raw_value is None else str(raw_value).strip()
    if missing:
        return "empty_or_missing", "<missing>"
    if len(value) in {6, 7} and value.isdigit():
        prefix6 = value[:6]
        if prefix6 in ibge_prefixes:
            return "current_IBGE_municipality_match", value
        if value[:2] in UF_PREFIXES:
            return "valid_UF_prefix_unmatched_current_IBGE", value
    return "special_or_unknown_no_official_foreign_mapping", value


def residence_class_us(raw_value: str) -> tuple[str, str]:
    value = (raw_value or "").strip()
    if value in {"1", "2", "3"}:
        return "US_resident_status_1_3", value
    if value == "4":
        return "foreign_resident_status_4", value
    if not value:
        return "blank_residence_code_unknown", "<blank>"
    return "other_residence_code_unknown", value


def empty_scan(country: str, year: int) -> dict[str, Any]:
    return {
        "country": country,
        "year": year,
        "metrics": Counter(),
        "missing": defaultdict(Counter),
        "residence_codes": defaultdict(Counter),
        "residence_strata": defaultdict(Counter),
        "valid_strata": defaultdict(Counter),
        "source": {},
    }


def add_disease(counter: Counter, group: str, ucd: bool, extra: bool) -> None:
    if ucd:
        counter[f"{group}_ucd"] += 1
    if extra:
        counter[f"{group}_extra_only"] += 1
    if ucd or extra:
        counter[f"{group}_any"] += 1


def add_contract_record(
    result: dict[str, Any],
    residence_class: str,
    residence_code: str,
    age_group: str | None,
    age_status: str,
    sex: str | None,
    sex_status: str,
    ucd: bool,
    extra: bool,
) -> None:
    metrics: Counter = result["metrics"]
    missing: dict = result["missing"]
    residence_codes: dict = result["residence_codes"]
    residence_strata: dict = result["residence_strata"]
    valid_strata: dict = result["valid_strata"]

    group = age_sex_group(age_group, sex)
    metrics["residence_contract_records"] += 1
    metrics[f"contract_allcause_{group}"] += 1
    code_counter = residence_codes[(residence_class, residence_code)]
    code_counter["contract_records"] += 1
    code_counter[f"contract_allcause_{group}"] += 1
    # Maintain the residence-eligible disease totals alongside their age/sex
    # partitions; downstream denominators and TabNet replay use these totals.
    add_disease(metrics, "contract", ucd, extra)
    add_disease(metrics, f"contract_{group}", ucd, extra)
    add_disease(code_counter, "contract", ucd, extra)

    if group != "valid":
        missing_key = (group, age_status, sex_status, age_group or "<unknown>", sex or "<unknown>")
        add_disease(missing[missing_key], "disease", ucd, extra)
    else:
        metrics["analytic_age_sex_records"] += 1
        code_counter["contract_valid_age_sex_records"] += 1
        add_disease(code_counter, "contract_valid", ucd, extra)
        cell = (age_group, sex)
        valid_strata[cell]["allcause"] += 1
        add_disease(valid_strata[cell], "disease", ucd, extra)
        rcell = (residence_class, age_group, sex)
        residence_strata[rcell]["allcause"] += 1
        add_disease(residence_strata[rcell], "disease", ucd, extra)


def scan_us_year(year: int, prior_identity: dict[str, Any]) -> dict[str, Any]:
    result = empty_scan("US", year)
    archive_path = US_RAW_DIR / f"mort{year}us.zip"
    before = archive_path.stat()
    prior_bytes = int(prior_identity["bytes"])
    if before.st_size != prior_bytes:
        raise RuntimeError(f"US/{year} current archive size differs from prior identity record")
    source_info: dict[str, Any] = {
        "path": str(archive_path),
        "current_size_bytes": before.st_size,
        "current_mtime_utc": iso_utc_ns(before.st_mtime_ns),
        "prior_verified_sha256": prior_identity["actual_sha256"],
        "prior_identity_check_date": "2026-10-21 source_identity.csv",
        "streamed_once_for_this_audit": True,
    }

    raw_rows = observed_bytes = observed_crc32 = 0
    short_records = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise RuntimeError(f"US/{year} expected one data member; found {len(members)}")
        member = members[0]
        source_info["member"] = {
            "name": member.filename,
            "file_size": member.file_size,
            "compressed_size": member.compress_size,
            "crc32_expected": f"{member.CRC:08x}",
            "compression_method": member.compress_type,
        }
        with US.open_member_stream(archive_path, archive, member) as (stream, backend):
            source_info["stream_backend"] = backend
            for raw_line in stream:
                raw_rows += 1
                observed_bytes += len(raw_line)
                observed_crc32 = binascii.crc32(raw_line, observed_crc32)
                metrics: Counter = result["metrics"]
                metrics["raw_records"] += 1
                body = raw_line.rstrip(b"\r\n")
                if len(body) < US.MIN_RECORD_WIDTH:
                    short_records += 1
                    metrics["short_records"] += 1
                    continue

                resident_raw = US._decode(body[US.RESIDENT_SLICE]).strip()
                res_class, res_code = residence_class_us(resident_raw)
                age_group, age_status = US.parse_age(US._decode(body[US.AGE_SLICE]))
                sex_raw = US._decode(body[US.SEX_SLICE]).strip().upper()
                sex = US.parse_sex(sex_raw)
                sex_status = (
                    "valid_male" if sex == "M" else "valid_female" if sex == "F"
                    else "blank" if not sex_raw else "invalid_value"
                )
                ucd_code = US._decode(body[US.UCD_SLICE]).strip().upper()
                ucd = US.is_biliary_code(ucd_code)
                # Exact frozen primary available-mention axis: UCD plus entity-axis.
                entity_mentions = US.parse_entity_mentions(body[US.ENTITY_SLICE])
                entity_biliary = any(US.is_biliary_code(item["code"]) for item in entity_mentions)
                extra = bool(entity_biliary and not ucd)
                base = res_class == "US_resident_status_1_3"

                code_counter = result["residence_codes"][(res_class, res_code)]
                code_counter["source_records"] += 1
                if age_group is not None and sex is not None:
                    code_counter["all_record_valid_age_sex_records"] += 1
                    add_disease(code_counter, "all_record_valid", ucd, extra)
                add_disease(code_counter, "all_record", ucd, extra)
                if base:
                    add_contract_record(
                        result, res_class, res_code, age_group, age_status,
                        sex, sex_status, ucd, extra,
                    )

    observed_crc32 &= 0xFFFFFFFF
    if observed_bytes != member.file_size:
        raise RuntimeError(f"US/{year} uncompressed member byte count mismatch")
    if observed_crc32 != member.CRC:
        raise RuntimeError(f"US/{year} CRC32 mismatch: {observed_crc32:08x} != {member.CRC:08x}")
    after = archive_path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"US/{year} archive changed during scan")
    source_info.update({
        "source_rows": raw_rows,
        "member_bytes_observed": observed_bytes,
        "crc32_observed": f"{observed_crc32:08x}",
        "crc32_verified": True,
        "member_eof_reached": True,
        "short_records": short_records,
    })
    result["source"] = source_info
    print(f"DONE US/{year}: rows={raw_rows:,} crc={observed_crc32:08x}", flush=True)
    return result


def scan_br_year(year: int, prior_identity: dict[str, Any], ibge_prefixes: set[str]) -> dict[str, Any]:
    result = empty_scan("BR", year)
    source_path = BR.source_path_for_year(BR_RAW_DIR, year)
    before = source_path.stat()
    if before.st_size != int(prior_identity["bytes"]):
        raise RuntimeError(f"BR/{year} current source size differs from prior identity record")
    source_info: dict[str, Any] = {
        "path": str(source_path),
        "current_size_bytes": before.st_size,
        "current_mtime_utc": iso_utc_ns(before.st_mtime_ns),
        "prior_verified_sha256": prior_identity["actual_sha256"],
        "prior_identity_check_date": "2026-10-21 source_identity.csv",
        "streamed_once_for_this_audit": True,
    }
    status: dict[str, Any] = {"csv_eof_reached": False, "zip_crc_verified": False}
    raw_rows = 0

    with BR.open_source_text(source_path, status) as (text, source_member):
        reader = csv.DictReader(text, delimiter=";", strict=True)
        headers = BR.read_headers(reader)
        source_info["member"] = source_member
        source_info["headers_required_present"] = all(field in headers for field in BR.REQUIRED_HEADERS)
        for source_row, row in enumerate(reader, start=1):
            raw_rows += 1
            result["metrics"]["raw_records"] += 1
            if None in row:
                raise RuntimeError(f"BR/{year} extra CSV fields at data row {source_row}")
            malformed = [field for field in BR.REQUIRED_HEADERS if row.get(field) is None]
            if malformed:
                raise RuntimeError(f"BR/{year} truncated required fields at data row {source_row}: {sorted(malformed)}")

            residence_missing, _ = BR.is_missing(row.get("CODMUNRES"))
            res_class, res_code = residence_class_br(row.get("CODMUNRES"), ibge_prefixes)
            tipobito_missing, _ = BR.is_missing(row.get("TIPOBITO"))
            tipobito_text = "" if tipobito_missing else str(row.get("TIPOBITO")).strip()
            nonfetal = not tipobito_missing and tipobito_text != "1"
            age, age_status = BR.parse_age(row.get("IDADE"))
            age_group = None if age is None else BR.age_group_for_age(age)
            sex, sex_status = BR.parse_sex(row.get("SEXO"))
            ucd_code = BR.normalize_ucd(row.get("CAUSABAS"))
            ucd = len(ucd_code) >= 3 and ucd_code[:3] in BR.COMPONENTS
            line_data = BR.extract_line_codes(row)
            line_biliary = bool(line_data["has_line_biliary"])
            extra = bool(line_biliary and not ucd)
            base = nonfetal and not residence_missing

            code_counter = result["residence_codes"][(res_class, res_code)]
            code_counter["source_records"] += 1
            if nonfetal:
                code_counter["nonfetal_records"] += 1
                add_disease(code_counter, "nonfetal", ucd, extra)
                if age_group is not None and sex is not None:
                    code_counter["nonfetal_valid_age_sex_records"] += 1
                    add_disease(code_counter, "nonfetal_valid", ucd, extra)
            if base:
                add_contract_record(
                    result, res_class, res_code, age_group, age_status,
                    sex, sex_status, ucd, extra,
                )

        # The ZIP helper checks the member CRC only after this caller confirms EOF.
        status["csv_eof_reached"] = True

    after = source_path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"BR/{year} source changed during scan")
    if source_path.suffix.lower() == ".zip" and not status.get("zip_crc_verified"):
        raise RuntimeError(f"BR/{year} ZIP CRC was not verified")
    source_info.update({
        "source_rows": raw_rows,
        "csv_eof_reached": bool(status.get("csv_eof_reached")),
        "zip_crc_verified": bool(status.get("zip_crc_verified")),
    })
    result["source"] = source_info
    print(f"DONE BR/{year}: rows={raw_rows:,} zip_crc={source_info['zip_crc_verified']}", flush=True)
    return result


def fetch_ibge_reference() -> tuple[set[str], dict[str, Any], bytes]:
    request = urllib.request.Request(
        IBGE_URL,
        headers={"User-Agent": "MCOD aggregate audit/1.0", "Accept": "application/json", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            transport = response.read()
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            payload = gzip.decompress(transport) if encoding == "gzip" else transport
            rows = json.loads(payload.decode("utf-8"))
            codes = {
                str(item.get("id", "")).zfill(7)
                for item in rows
                if str(item.get("id", "")).isdigit() and len(str(item.get("id", ""))) <= 7
            }
            codes = {code for code in codes if len(code) == 7}
            if len(codes) < 5000:
                raise ValueError(f"IBGE API returned only {len(codes)} 7-digit municipality codes")
            meta = {
                "source_url": IBGE_URL,
                "retrieval": "live_official_api",
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "http_status": getattr(response, "status", None),
                "content_encoding": encoding,
                "transport_sha256": sha256_bytes(transport),
                "decoded_json_sha256": sha256_bytes(payload),
                "official_7_digit_municipality_codes": len(codes),
                "unique_6_digit_prefixes": len({code[:6] for code in codes}),
                "classification_rule": "match 6- or 7-digit CODMUNRES by its first six digits; historical unmatched values remain unresolved, never foreign by inference",
            }
            return {code[:6] for code in codes}, meta, payload
    except Exception as live_error:
        if not PILOT_IBGE_REFERENCE.is_file() or not PILOT_IBGE_MANIFEST.is_file():
            raise RuntimeError(f"Official IBGE live lookup failed and no prior official cache is available: {live_error}") from live_error
        pilot_manifest = json.loads(PILOT_IBGE_MANIFEST.read_text(encoding="utf-8"))
        reference = pilot_manifest["ibge_current_municipality_reference"]
        transport = PILOT_IBGE_REFERENCE.read_bytes()
        if sha256_bytes(transport) != reference["response_sha256"]:
            raise RuntimeError("Saved IBGE reference response hash does not match prior manifest") from live_error
        payload = gzip.decompress(transport) if reference.get("response_content_encoding") == "gzip" else transport
        if sha256_bytes(payload) != reference["decoded_json_sha256"]:
            raise RuntimeError("Saved IBGE decoded JSON hash does not match prior manifest") from live_error
        rows = json.loads(payload.decode("utf-8"))
        codes = {
            str(item.get("id", "")).zfill(7)
            for item in rows
            if str(item.get("id", "")).isdigit() and len(str(item.get("id", ""))) <= 7
        }
        codes = {code for code in codes if len(code) == 7}
        meta = {
            "source_url": IBGE_URL,
            "retrieval": "cached_official_response_fallback",
            "prior_retrieval_date": "2026-10-21",
            "live_lookup_error": f"{type(live_error).__name__}: {live_error}",
            "transport_sha256": reference["response_sha256"],
            "decoded_json_sha256": reference["decoded_json_sha256"],
            "official_7_digit_municipality_codes": len(codes),
            "unique_6_digit_prefixes": len({code[:6] for code in codes}),
            "classification_rule": "match 6- or 7-digit CODMUNRES by its first six digits; historical unmatched values remain unresolved, never foreign by inference",
        }
        return {code[:6] for code in codes}, meta, payload


def load_prior_identity() -> tuple[dict[tuple[str, int], dict[str, str]], str]:
    result: dict[tuple[str, int], dict[str, str]] = {}
    with FROZEN_SOURCE_IDENTITY.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["country"].strip().upper(), int(row["year"]))
            result[key] = row
    wanted = {(country, year) for country in ("US", "BR") for year in YEARS}
    if not wanted.issubset(result):
        raise RuntimeError("Prior source identity table lacks one or more 2012-2023 inputs")
    mismatches = [key for key in wanted if result[key]["status"] != "MATCH"]
    if mismatches:
        raise RuntimeError(f"Prior source identity table contains non-MATCH entries: {mismatches}")
    return result, sha256_file(FROZEN_SOURCE_IDENTITY)


def load_tabnet_validation() -> tuple[dict[int, dict[str, Any]], str, dict[str, Any]]:
    observed_hash = sha256_file(PRIOR_TABNET)
    if observed_hash != PRIOR_TABNET_SHA256:
        raise RuntimeError(f"Prior TabNet evidence hash mismatch: {observed_hash}")
    rows: dict[int, dict[str, Any]] = {}
    with PRIOR_TABNET.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            year = int(row["year"])
            rows[year] = {
                "official_ucd": int(row["official_ucd"]),
                "official_all": int(row["official_all"]),
                "saved_ucd_exact": str(row["ucd_exact"]).strip().lower() == "true",
                "saved_all_exact": str(row["all_exact"]).strip().lower() == "true",
            }
    if set(rows) != set(range(2012, 2025)):
        raise RuntimeError("Prior TabNet evidence is not the expected complete 2012-2024 annual table")
    annual_13 = {
        "years": 13,
        "ucd_exact_years": sum(item["saved_ucd_exact"] for item in rows.values()),
        "allcause_exact_years": sum(item["saved_all_exact"] for item in rows.values()),
        "saved_validation_csv_sha256": observed_hash,
    }
    return rows, observed_hash, annual_13


def load_reference_grid() -> tuple[dict[tuple[str, int, str, str], int], dict[str, float], dict[str, Any]]:
    populations: dict[tuple[str, int, str, str], int] = {}
    with FROZEN_ANALYSIS.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            country = row["country"].strip()
            year = int(row["year"])
            if country not in {"BR", "US"} or year not in YEARS:
                continue
            key = (country, year, row["age_group"], row["sex"])
            if key in populations:
                raise RuntimeError(f"Duplicate population denominator cell: {key}")
            population = int(row["population"])
            if population <= 0:
                raise RuntimeError(f"Nonpositive population denominator: {key}")
            populations[key] = population
    expected = {
        (country, year, age, sex)
        for country in ("BR", "US") for year in YEARS
        for age in AGE_GROUPS for sex in SEXES
    }
    if set(populations) != expected:
        raise RuntimeError(f"Frozen population grid mismatch: expected {len(expected)}, got {len(populations)}")

    weights: dict[str, float] = {}
    with FROZEN_WEIGHTS.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            weights[row["age_group"]] = float(row["weight"])
    if set(weights) != set(AGE_GROUPS) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise RuntimeError("WHO18 age weights are incomplete or do not sum to one")
    meta = {
        "population_source": str(FROZEN_ANALYSIS),
        "population_sha256": sha256_file(FROZEN_ANALYSIS),
        "who18_source": str(FROZEN_WEIGHTS),
        "who18_sha256": sha256_file(FROZEN_WEIGHTS),
        "population_cells": len(populations),
        "who18_weights_sum": sum(weights.values()),
    }
    return populations, weights, meta


def load_frozen_analytic_counts() -> dict[tuple[str, int, str, str], dict[str, int]]:
    counts: dict[tuple[str, int, str, str], dict[str, int]] = {}
    with FROZEN_ANALYSIS.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            country = row["country"].strip()
            year = int(row["year"])
            if country not in {"BR", "US"} or year not in YEARS:
                continue
            key = (country, year, row["age_group"], row["sex"])
            counts[key] = {"ucd": int(row["ucd"]), "any": int(row["any_common"])}
    return counts


def load_ledger_eligible() -> dict[tuple[str, int], int]:
    values: dict[tuple[str, int], int] = {}
    with FROZEN_LEDGER.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            country = row["country"].strip().upper()
            year = int(row["year"])
            if country in {"BR", "US"} and year in YEARS:
                values[(country, year)] = int(row["eligible_records"])
    return values


def flatten_scan_results(scans: list[dict[str, Any]]):
    summary: dict[tuple[str, int], Counter] = {}
    missing: dict[tuple[str, int, tuple], Counter] = {}
    residence_codes: dict[tuple[str, int, tuple], Counter] = {}
    residence_strata: dict[tuple[str, int, tuple], Counter] = {}
    valid_strata: dict[tuple[str, int, str, str], Counter] = defaultdict(Counter)
    source_info: dict[str, Any] = {}
    for scan in scans:
        cy = (scan["country"], scan["year"])
        summary[cy] = scan["metrics"]
        source_info[f"{scan['country']}_{scan['year']}"] = scan["source"]
        for key, value in scan["missing"].items():
            missing[(scan["country"], scan["year"], key)] = value
        for key, value in scan["residence_codes"].items():
            residence_codes[(scan["country"], scan["year"], key)] = value
        for key, value in scan["residence_strata"].items():
            residence_strata[(scan["country"], scan["year"], key)] = value
        for (age, sex), value in scan["valid_strata"].items():
            valid_strata[(scan["country"], scan["year"], age, sex)].update(value)
    return summary, missing, residence_codes, residence_strata, valid_strata, source_info


def serialize_scan_result(scan: dict[str, Any]) -> dict[str, Any]:
    """Return only aggregate counters and source-integrity metadata for caching."""
    return {
        "country": scan["country"],
        "year": int(scan["year"]),
        "metrics": {key: int(value) for key, value in scan["metrics"].items()},
        "missing": [
            {"key": list(key), "counts": {name: int(value) for name, value in counts.items()}}
            for key, counts in sorted(scan["missing"].items())
        ],
        "residence_codes": [
            {"key": list(key), "counts": {name: int(value) for name, value in counts.items()}}
            for key, counts in sorted(scan["residence_codes"].items())
        ],
        "residence_strata": [
            {"key": list(key), "counts": {name: int(value) for name, value in counts.items()}}
            for key, counts in sorted(scan["residence_strata"].items())
        ],
        "valid_strata": [
            {"key": list(key), "counts": {name: int(value) for name, value in counts.items()}}
            for key, counts in sorted(scan["valid_strata"].items())
        ],
        "source": scan["source"],
    }


def deserialize_scan_result(value: dict[str, Any]) -> dict[str, Any]:
    """Restore the internal counter shape from an aggregate-only cache entry."""
    result = empty_scan(str(value["country"]), int(value["year"]))
    result["metrics"].update({key: int(count) for key, count in value["metrics"].items()})
    for field, key_width in (
        ("missing", 5), ("residence_codes", 2), ("residence_strata", 3), ("valid_strata", 2)
    ):
        for item in value[field]:
            key = tuple(item["key"])
            if len(key) != key_width:
                raise RuntimeError(f"Invalid {field} key in aggregate cache for {value['country']}/{value['year']}")
            result[field][key].update({name: int(count) for name, count in item["counts"].items()})
    result["source"] = value["source"]
    return result


def write_aggregate_scan_cache(
    path: Path,
    scans: list[dict[str, Any]],
    complete: bool,
    ibge_meta: dict[str, Any],
    ibge_payload: bytes,
) -> dict[str, Any]:
    """Atomically save replayable per-year counters, never source rows."""
    payload = {
        "schema_version": 1,
        "audit_name": "residence-missing aggregate-only raw audit",
        "cache_written_utc": datetime.now(timezone.utc).isoformat(),
        "complete": bool(complete),
        "completed_source_year_scans": len(scans),
        "expected_source_year_scans": len(YEARS) * 2,
        "record_level_rows_included": False,
        "contains_only_aggregate_counters_and_source_metadata": True,
        "source_integrity_metadata_complete": all(
            "member_eof_reached" in scan["source"] or "csv_eof_reached" in scan["source"]
            for scan in scans
        ),
        "ibge_meta": ibge_meta,
        "ibge_payload_base64": base64.b64encode(ibge_payload).decode("ascii"),
        "scans": [serialize_scan_result(scan) for scan in sorted(scans, key=lambda item: (item["country"], item["year"]))],
    }
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary_path, path)
    return {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size, "completed_scans": len(scans), "complete": bool(complete)}


def load_aggregate_scan_cache(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], bytes]:
    cache = json.loads(path.read_text(encoding="utf-8"))
    if cache.get("schema_version") != 1 or cache.get("record_level_rows_included") is not False:
        raise RuntimeError("Aggregate scan cache schema or record-level-data safeguard is invalid")
    expected = {(country, year) for country in ("US", "BR") for year in YEARS}
    scans = [deserialize_scan_result(item) for item in cache.get("scans", [])]
    actual = {(item["country"], item["year"]) for item in scans}
    if not cache.get("complete") or actual != expected or len(scans) != len(expected):
        raise RuntimeError(f"Aggregate scan cache is incomplete: expected {len(expected)} country-years, got {len(actual)}")
    return scans, cache["ibge_meta"], base64.b64decode(cache["ibge_payload_base64"], validate=True)


def counter_rows_as_dicts(mapping: dict) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for key in sorted(mapping):
        value = mapping[key]
        output.append({"key": key, **dict(value)})
    return output


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def population_coefficient(
    populations: dict[tuple[str, int, str, str], int],
    weights: dict[str, float],
    country: str,
    year: int,
    age: str,
    sex: str,
    years: tuple[int, ...],
    under75: bool = False,
) -> float:
    ages = UNDER75 if under75 else AGE_GROUPS
    age_weight = weights[age]
    if under75:
        age_weight /= sum(weights[item] for item in UNDER75)
    return 100000.0 * age_weight * 0.5 / len(years) / populations[(country, year, age, sex)]


def support_for_missing(group: str, age_known: str, sex_known: str, under75: bool):
    candidate_ages = UNDER75 if under75 else AGE_GROUPS
    if group == "age_only_invalid":
        known_sexes = (sex_known,) if sex_known in SEXES else SEXES
        return [(age, sex) for age in candidate_ages for sex in known_sexes]
    if group == "sex_only_invalid":
        if age_known not in AGE_GROUPS or (under75 and age_known not in UNDER75):
            return []
        return [(age_known, sex) for sex in SEXES]
    if group == "both_invalid":
        return [(age, sex) for age in candidate_ages for sex in SEXES]
    raise ValueError(f"Unknown missingness group: {group}")


def missing_rate_bounds(
    country: str,
    years: tuple[int, ...],
    under75: bool,
    missing: dict[tuple[str, int, tuple], Counter],
    populations: dict[tuple[str, int, str, str], int],
    weights: dict[str, float],
) -> dict[str, dict[str, float]]:
    bounds = {
        "ucd": {"min": 0.0, "max": 0.0},
        "extra_only": {"min": 0.0, "max": 0.0},
    }
    for (c, year, key), counter in missing.items():
        if c != country or year not in years:
            continue
        group, _age_status, _sex_status, age_known, sex_known = key
        support = support_for_missing(group, age_known, sex_known, under75)
        if support:
            coefficients = [
                population_coefficient(populations, weights, country, year, age, sex, years, under75)
                for age, sex in support
            ]
            minimum, maximum = min(coefficients), max(coefficients)
        else:
            minimum = maximum = 0.0
        # An invalid/unknown age may lie outside the 0-74 target altogether.
        # Its feasible contribution minimum is therefore zero, even though
        # its maximum can be attained by assigning it to an in-range cell.
        if under75 and group in {"age_only_invalid", "both_invalid"}:
            minimum = 0.0
        for endpoint, field in (("ucd", "disease_ucd"), ("extra_only", "disease_extra_only")):
            count = int(counter[field])
            bounds[endpoint]["min"] += count * minimum
            bounds[endpoint]["max"] += count * maximum
    return bounds


def base_rates_from_strata(
    country: str,
    years: tuple[int, ...],
    valid_strata: dict[tuple[str, int, str, str], Counter],
    populations: dict[tuple[str, int, str, str], int],
    weights: dict[str, float],
    under75: bool = False,
    residence_strata: dict[tuple[str, int, tuple], Counter] | None = None,
    allowed_classes: set[str] | None = None,
) -> dict[str, float]:
    result = {"ucd": 0.0, "extra_only": 0.0, "any": 0.0}
    if residence_strata is None or allowed_classes is None:
        items = (
            (year, age, sex, counter)
            for (c, year, age, sex), counter in valid_strata.items()
            if c == country and year in years
        )
    else:
        items = (
            (year, key[1], key[2], counter)
            for (c, year, key), counter in residence_strata.items()
            if c == country and year in years and key[0] in allowed_classes
        )
    allowed_ages = set(UNDER75 if under75 else AGE_GROUPS)
    for year, age, sex, counter in items:
        if age not in allowed_ages:
            continue
        coefficient = population_coefficient(populations, weights, country, year, age, sex, years, under75)
        ucd = int(counter["disease_ucd"])
        extra = int(counter["disease_extra_only"])
        result["ucd"] += coefficient * ucd
        result["extra_only"] += coefficient * extra
        result["any"] += coefficient * (ucd + extra)
    return result


def rate_bound_rows(
    valid_strata,
    missing,
    populations,
    weights,
    residence_strata,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    years = YEARS
    rate_rows: list[dict[str, Any]] = []
    base_by_country: dict[str, dict[str, float]] = {}
    bounds_by_country: dict[str, dict[str, dict[str, float]]] = {}
    for country in ("US", "BR"):
        base = base_rates_from_strata(country, years, valid_strata, populations, weights)
        missing_bounds = missing_rate_bounds(country, years, False, missing, populations, weights)
        base_by_country[country] = base
        bounds_by_country[country] = missing_bounds
        for endpoint in ("ucd", "extra_only", "any"):
            base_value = base[endpoint]
            if endpoint == "ucd":
                miss_min = missing_bounds["ucd"]["min"]
                miss_max = missing_bounds["ucd"]["max"]
            elif endpoint == "extra_only":
                miss_min = missing_bounds["extra_only"]["min"]
                miss_max = missing_bounds["extra_only"]["max"]
            else:
                miss_min = missing_bounds["ucd"]["min"] + missing_bounds["extra_only"]["min"]
                miss_max = missing_bounds["ucd"]["max"] + missing_bounds["extra_only"]["max"]
            rate_rows.append({
                "period": "2012-2023_all_age",
                "country": country,
                "endpoint": endpoint,
                "complete_case_rate_per_100000": base_value,
                "missing_allocation_min_increment": miss_min,
                "missing_allocation_max_increment": miss_max,
                "rate_lower": base_value + miss_min,
                "rate_upper": base_value + miss_max,
                "bound_type": "deterministic feasible allocation bound, not a confidence interval",
            })

    def expansion(country: str, side: str) -> float:
        base = base_by_country[country]
        mb = bounds_by_country[country]
        if side == "min":
            extra = mb["extra_only"]["min"]
            ucd = mb["ucd"]["max"]
        else:
            extra = mb["extra_only"]["max"]
            ucd = mb["ucd"]["min"]
        return (base["ucd"] + ucd + base["extra_only"] + extra) / (base["ucd"] + ucd)

    us_base, br_base = base_by_country["US"], base_by_country["BR"]
    base_rrr = (br_base["any"] / us_base["any"]) / (br_base["ucd"] / us_base["ucd"])
    base_dd = br_base["extra_only"] - us_base["extra_only"]
    all_rrr_lower = expansion("BR", "min") / expansion("US", "max")
    all_rrr_upper = expansion("BR", "max") / expansion("US", "min")
    br_missing = bounds_by_country["BR"]["extra_only"]
    us_missing = bounds_by_country["US"]["extra_only"]
    all_dd_lower = base_dd + br_missing["min"] - us_missing["max"]
    all_dd_upper = base_dd + br_missing["max"] - us_missing["min"]
    contrast_rows = [
        {
            "period": "2012-2023_all_age",
            "metric": "RRR_any_vs_UCD",
            "complete_case_estimate": base_rrr,
            "lower_bound": all_rrr_lower,
            "upper_bound": all_rrr_upper,
            "allocation_rule": "same UCD allocation increments both UCD and any; extra-only increments any only; age/sex support preserved",
            "bound_type": "deterministic feasible allocation bound, not a confidence interval",
        },
        {
            "period": "2012-2023_all_age",
            "metric": "DD_any_minus_UCD_per_100000",
            "complete_case_estimate": base_dd,
            "lower_bound": all_dd_lower,
            "upper_bound": all_dd_upper,
            "allocation_rule": "UCD missing increments cancel exactly in any-minus-UCD; extra-only allocations vary by country",
            "bound_type": "deterministic feasible allocation bound, not a confidence interval",
        },
    ]

    time_rows: list[dict[str, Any]] = []
    periods = {"early_2012_2014": (2012, 2013, 2014), "late_2021_2023": (2021, 2022, 2023)}
    time_base: dict[str, dict[str, dict[str, float]]] = {}
    time_miss: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for period_name, period_years in periods.items():
        time_base[period_name] = {}
        time_miss[period_name] = {}
        for country in ("US", "BR"):
            base = base_rates_from_strata(country, period_years, valid_strata, populations, weights, under75=True)
            mb = missing_rate_bounds(country, period_years, True, missing, populations, weights)
            time_base[period_name][country] = base
            time_miss[period_name][country] = mb
            for endpoint in ("ucd", "extra_only", "any"):
                if endpoint == "ucd":
                    miss_min, miss_max = mb["ucd"]["min"], mb["ucd"]["max"]
                elif endpoint == "extra_only":
                    miss_min, miss_max = mb["extra_only"]["min"], mb["extra_only"]["max"]
                else:
                    miss_min = mb["ucd"]["min"] + mb["extra_only"]["min"]
                    miss_max = mb["ucd"]["max"] + mb["extra_only"]["max"]
                time_rows.append({
                    "period": period_name,
                    "country": country,
                    "endpoint": endpoint,
                    "complete_case_rate_per_100000": base[endpoint],
                    "missing_allocation_min_increment": miss_min,
                    "missing_allocation_max_increment": miss_max,
                    "rate_lower": base[endpoint] + miss_min,
                    "rate_upper": base[endpoint] + miss_max,
                    "bound_type": "deterministic feasible allocation bound, not a confidence interval",
                })

    def time_expansion(period_name: str, country: str, side: str) -> float:
        base = time_base[period_name][country]
        mb = time_miss[period_name][country]
        if side == "min":
            extra, ucd = mb["extra_only"]["min"], mb["ucd"]["max"]
        else:
            extra, ucd = mb["extra_only"]["max"], mb["ucd"]["min"]
        return (base["ucd"] + ucd + base["extra_only"] + extra) / (base["ucd"] + ucd)

    e, l = "early_2012_2014", "late_2021_2023"
    rr_early = (time_base[e]["BR"]["any"] / time_base[e]["US"]["any"]) / (time_base[e]["BR"]["ucd"] / time_base[e]["US"]["ucd"])
    rr_late = (time_base[l]["BR"]["any"] / time_base[l]["US"]["any"]) / (time_base[l]["BR"]["ucd"] / time_base[l]["US"]["ucd"])
    i_r_base = __import__("math").log(rr_late / rr_early)
    i_r_lower = (
        __import__("math").log(time_expansion(l, "BR", "min") / time_expansion(l, "US", "max"))
        - __import__("math").log(time_expansion(e, "BR", "max") / time_expansion(e, "US", "min"))
    )
    i_r_upper = (
        __import__("math").log(time_expansion(l, "BR", "max") / time_expansion(l, "US", "min"))
        - __import__("math").log(time_expansion(e, "BR", "min") / time_expansion(e, "US", "max"))
    )
    i_a_base = (
        time_base[l]["BR"]["extra_only"] - time_base[l]["US"]["extra_only"]
        - time_base[e]["BR"]["extra_only"] + time_base[e]["US"]["extra_only"]
    )
    ebr_l = time_miss[l]["BR"]["extra_only"]
    eus_l = time_miss[l]["US"]["extra_only"]
    ebr_e = time_miss[e]["BR"]["extra_only"]
    eus_e = time_miss[e]["US"]["extra_only"]
    i_a_lower = i_a_base + ebr_l["min"] - eus_l["max"] - ebr_e["max"] + eus_e["min"]
    i_a_upper = i_a_base + ebr_l["max"] - eus_l["min"] - ebr_e["min"] + eus_e["max"]
    contrast_rows.extend([
        {
            "period": "late_2021_2023_vs_early_2012_2014_age_0_74",
            "metric": "interaction_log_RRR_change",
            "complete_case_estimate": i_r_base,
            "lower_bound": i_r_lower,
            "upper_bound": i_r_upper,
            "allocation_rule": "paired UCD increments jointly alter UCD and any expansion factors; extra-only is disjoint; year periods are independently allocated",
            "bound_type": "deterministic feasible allocation bound, not a confidence interval",
        },
        {
            "period": "late_2021_2023_vs_early_2012_2014_age_0_74",
            "metric": "interaction_RD_change_per_100000",
            "complete_case_estimate": i_a_base,
            "lower_bound": i_a_lower,
            "upper_bound": i_a_upper,
            "allocation_rule": "UCD missing increments cancel; missing age may be outside 0-74 (zero contribution), but known age >=75 cannot enter",
            "bound_type": "deterministic feasible allocation bound, not a confidence interval",
        },
    ])
    separation_rows: list[dict[str, Any]] = []
    for period_name in (e, l):
        for endpoint in ("ucd", "any"):
            br_row = next(
                row for row in time_rows
                if row["period"] == period_name and row["country"] == "BR" and row["endpoint"] == endpoint
            )
            us_row = next(
                row for row in time_rows
                if row["period"] == period_name and row["country"] == "US" and row["endpoint"] == endpoint
            )
            signed_lower = br_row["rate_lower"] - us_row["rate_upper"]
            signed_upper = br_row["rate_upper"] - us_row["rate_lower"]
            if signed_lower > 0:
                absolute_lower, absolute_upper = signed_lower, signed_upper
            elif signed_upper < 0:
                absolute_lower, absolute_upper = -signed_upper, -signed_lower
            else:
                absolute_lower, absolute_upper = 0.0, max(abs(signed_lower), abs(signed_upper))
            separation_rows.append({
                "period": period_name,
                "endpoint": endpoint,
                "BR_rate_lower": br_row["rate_lower"],
                "BR_rate_upper": br_row["rate_upper"],
                "US_rate_lower": us_row["rate_lower"],
                "US_rate_upper": us_row["rate_upper"],
                "signed_BR_minus_US_lower": signed_lower,
                "signed_BR_minus_US_upper": signed_upper,
                "BR_lower_strictly_exceeds_US_upper": signed_lower > 0,
                "absolute_gap_lower": absolute_lower,
                "absolute_gap_upper": absolute_upper,
            })
            contrast_rows.append({
                "period": f"{period_name}_age_0_74",
                "metric": f"absolute_gap_{endpoint.upper()}_rate_BR_vs_US_per_100000",
                "complete_case_estimate": abs(
                    time_base[period_name]["BR"][endpoint] - time_base[period_name]["US"][endpoint]
                ),
                "lower_bound": absolute_lower,
                "upper_bound": absolute_upper,
                "allocation_rule": "derive the signed BR-minus-US interval from independent country bounds, then take exact absolute-value extrema",
                "bound_type": "deterministic feasible allocation bound, not a confidence interval",
            })
    return rate_rows + time_rows, contrast_rows, {
        "all_age_complete_case": base_by_country,
        "all_age_missing_rate_increments": bounds_by_country,
        "time_0_74_complete_case": time_base,
        "time_0_74_missing_rate_increments": time_miss,
        "all_age_RRR_lower": all_rrr_lower,
        "all_age_RRR_upper": all_rrr_upper,
        "all_age_DD_lower": all_dd_lower,
        "all_age_DD_upper": all_dd_upper,
        "time_log_RRR_interaction_lower": i_r_lower,
        "time_log_RRR_interaction_upper": i_r_upper,
        "time_RD_interaction_lower": i_a_lower,
        "time_RD_interaction_upper": i_a_upper,
        "time_0_74_BR_vs_US_rate_separation_checks": separation_rows,
        "time_0_74_all_BR_lower_exceeds_US_upper_for_UCD_and_any": all(
            row["BR_lower_strictly_exceeds_US_upper"] for row in separation_rows
        ),
    }


def calculate_residence_scope_sensitivity(
    valid_strata,
    residence_strata,
    populations,
    weights,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scope, br_classes in (
        ("operational_nonempty_CODMUNRES", None),
        ("current_IBGE_code_match_only_conservative_sensitivity", {"current_IBGE_municipality_match"}),
    ):
        by_country: dict[str, dict[str, float]] = {}
        for country in ("US", "BR"):
            if country == "US" or scope == "operational_nonempty_CODMUNRES":
                by_country[country] = base_rates_from_strata(country, YEARS, valid_strata, populations, weights)
            else:
                by_country[country] = base_rates_from_strata(
                    country, YEARS, valid_strata, populations, weights,
                    residence_strata=residence_strata, allowed_classes=br_classes,
                )
        rrr = (by_country["BR"]["any"] / by_country["US"]["any"]) / (by_country["BR"]["ucd"] / by_country["US"]["ucd"])
        dd = by_country["BR"]["extra_only"] - by_country["US"]["extra_only"]
        rows.extend([
            {"scope": scope, "period": "2012-2023_all_age", "metric": "RRR_any_vs_UCD", "estimate": rrr},
            {"scope": scope, "period": "2012-2023_all_age", "metric": "DD_any_minus_UCD_per_100000", "estimate": dd},
        ])
        period_rrrs: dict[str, float] = {}
        period_dds: dict[str, float] = {}
        for p_name, years in (("early_2012_2014", (2012, 2013, 2014)), ("late_2021_2023", (2021, 2022, 2023))):
            time_by_country: dict[str, dict[str, float]] = {}
            for country in ("US", "BR"):
                if country == "US" or scope == "operational_nonempty_CODMUNRES":
                    time_by_country[country] = base_rates_from_strata(country, years, valid_strata, populations, weights, under75=True)
                else:
                    time_by_country[country] = base_rates_from_strata(
                        country, years, valid_strata, populations, weights, under75=True,
                        residence_strata=residence_strata, allowed_classes=br_classes,
                    )
            period_rrrs[p_name] = (
                time_by_country["BR"]["any"] / time_by_country["US"]["any"]
            ) / (
                time_by_country["BR"]["ucd"] / time_by_country["US"]["ucd"]
            )
            period_dds[p_name] = time_by_country["BR"]["extra_only"] - time_by_country["US"]["extra_only"]
        import math
        rows.append({
            "scope": scope, "period": "late_2021_2023_vs_early_2012_2014_age_0_74",
            "metric": "interaction_log_RRR_change",
            "estimate": math.log(period_rrrs["late_2021_2023"] / period_rrrs["early_2012_2014"]),
        })
        rows.append({
            "scope": scope, "period": "late_2021_2023_vs_early_2012_2014_age_0_74",
            "metric": "interaction_RD_change_per_100000",
            "estimate": period_dds["late_2021_2023"] - period_dds["early_2012_2014"],
        })
    return rows


def build_outputs(
    scans: list[dict[str, Any]],
    ibge_meta: dict[str, Any],
    ibge_payload: bytes,
    prior_identity_hash: str,
    tabnet_rows: dict[int, dict[str, Any]],
    tabnet_hash: str,
    tabnet_13_summary: dict[str, Any],
    aggregate_cache_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary, missing, residence_codes, residence_strata, valid_strata, source_info = flatten_scan_results(scans)
    populations, weights, denominator_meta = load_reference_grid()
    frozen_counts = load_frozen_analytic_counts()
    ledger_eligible = load_ledger_eligible()

    country_year_rows: list[dict[str, Any]] = []
    tabnet_compare_rows: list[dict[str, Any]] = []
    verification_rows: list[dict[str, Any]] = []
    tabnet_primary_ucd_match = tabnet_primary_all_match = 0
    frozen_grid_mismatch_cells = 0
    eligible_ledger_mismatch_cells = 0
    for country in ("BR", "US"):
        for year in YEARS:
            m = summary[(country, year)]
            row: dict[str, Any] = {
                "country": country,
                "year": year,
                "raw_records": int(m["raw_records"]),
                "residence_contract_records": int(m["residence_contract_records"]),
                "analytic_age_sex_records": int(m["analytic_age_sex_records"]),
                "age_only_invalid_allcause": int(m["contract_allcause_age_only_invalid"]),
                "sex_only_invalid_allcause": int(m["contract_allcause_sex_only_invalid"]),
                "both_invalid_allcause": int(m["contract_allcause_both_invalid"]),
                "disease_ucd_residence_eligible": int(m["contract_ucd"]),
                "disease_extra_only_residence_eligible": int(m["contract_extra_only"]),
                "disease_any_residence_eligible": int(m["contract_any"]),
                "disease_ucd_analytic": int(m["contract_valid_ucd"]),
                "disease_extra_only_analytic": int(m["contract_valid_extra_only"]),
                "disease_any_analytic": int(m["contract_valid_any"]),
            }
            for group in MISSING_GROUPS:
                for outcome in ("ucd", "extra_only", "any"):
                    count = int(m[f"contract_{group}_{outcome}"])
                    denominator = int(m[f"contract_{outcome}"])
                    row[f"{group}_{outcome}"] = count
                    row[f"{group}_{outcome}_pct_of_residence_eligible_disease"] = (
                        100.0 * count / denominator if denominator else 0.0
                    )
            country_year_rows.append(row)

            expected_analytic = ledger_eligible.get((country, year))
            ledger_match = expected_analytic == int(m["analytic_age_sex_records"])
            eligible_ledger_mismatch_cells += int(not ledger_match)
            cell_mismatches = []
            for age in AGE_GROUPS:
                for sex in SEXES:
                    key = (country, year, age, sex)
                    observed = valid_strata.get(key, Counter())
                    frozen = frozen_counts.get(key)
                    if frozen is None or int(observed["disease_ucd"]) != frozen["ucd"] or int(observed["disease_any"]) != frozen["any"]:
                        cell_mismatches.append(f"{age}/{sex}")
            frozen_grid_mismatch_cells += len(cell_mismatches)
            verification_rows.append({
                "country": country,
                "year": year,
                "analytic_age_sex_records_scanned": int(m["analytic_age_sex_records"]),
                "prior_raw_ledger_eligible_records": expected_analytic,
                "prior_eligible_count_match": ledger_match,
                "frozen_UCD_any_grid_mismatched_cells": len(cell_mismatches),
                "status": "PASS" if ledger_match and not cell_mismatches else "FAIL",
            })

            if country == "BR":
                official = tabnet_rows[year]
                all_match = int(m["residence_contract_records"]) == official["official_all"]
                ucd_match = int(m["contract_ucd"]) == official["official_ucd"]
                tabnet_primary_all_match += int(all_match)
                tabnet_primary_ucd_match += int(ucd_match)
                tabnet_compare_rows.append({
                    "year": year,
                    "raw_source_contract_allcause_before_age_sex_filter": int(m["residence_contract_records"]),
                    "TabNet_allcause_residence_reference": official["official_all"],
                    "allcause_exact": all_match,
                    "raw_source_contract_UCD_K80_K83_before_age_sex_filter": int(m["contract_ucd"]),
                    "TabNet_UCD_K80_K83_reference": official["official_ucd"],
                    "ucd_exact": ucd_match,
                    "comparison_stage": "non-fetal + nonempty CODMUNRES, before age/sex exclusion",
                })

    missing_detail_rows = []
    for (country, year, key), counts in sorted(missing.items()):
        group, age_status, sex_status, known_age, known_sex = key
        if not any(int(counts[field]) for field in ("disease_ucd", "disease_extra_only")):
            continue
        missing_detail_rows.append({
            "country": country,
            "year": year,
            "missing_group": group,
            "age_parse_status": age_status,
            "sex_parse_status": sex_status,
            "known_age_group": known_age,
            "known_sex": known_sex,
            "UCD_K80_K83": int(counts["disease_ucd"]),
            "available_mention_extra_only_K80_K83": int(counts["disease_extra_only"]),
            "any_available_K80_K83": int(counts["disease_any"]),
        })

    residence_code_rows = []
    for (country, year, key), counts in sorted(residence_codes.items()):
        klass, raw_code = key
        residence_code_rows.append({
            "country": country,
            "year": year,
            "residence_class": klass,
            "residence_raw_code_aggregate_only": raw_code,
            **{field: int(counts[field]) for field in sorted(counts)},
        })

    residence_strata_rows = []
    for (country, year, key), counts in sorted(residence_strata.items()):
        klass, age, sex = key
        residence_strata_rows.append({
            "country": country,
            "year": year,
            "residence_class": klass,
            "age_group": age,
            "sex": sex,
            "age_sex_eligible_allcause": int(counts["allcause"]),
            "UCD_K80_K83": int(counts["disease_ucd"]),
            "available_mention_extra_only_K80_K83": int(counts["disease_extra_only"]),
            "any_available_K80_K83": int(counts["disease_any"]),
        })

    county_code_rows = []
    # Use the code-level table as the raw-value frequency evidence, not as a foreign classifier.
    for row in residence_code_rows:
        county_code_rows.append({
            "country": row["country"],
            "year": row["year"],
            "residence_class": row["residence_class"],
            "residence_raw_code_aggregate_only": row["residence_raw_code_aggregate_only"],
            "source_records": row.get("source_records", 0),
            "nonfetal_records": row.get("nonfetal_records", ""),
            "residence_contract_records": row.get("contract_records", 0),
            "valid_age_sex_contract_records": row.get("contract_valid_age_sex_records", 0),
            "contract_UCD_K80_K83": row.get("contract_ucd", 0),
            "contract_extra_only_K80_K83": row.get("contract_extra_only", 0),
            "contract_any_K80_K83": row.get("contract_any", 0),
            "all_record_valid_age_sex_UCD_K80_K83": row.get("all_record_valid_ucd", 0),
            "all_record_valid_age_sex_extra_only_K80_K83": row.get("all_record_valid_extra_only", 0),
            "all_record_valid_age_sex_any_K80_K83": row.get("all_record_valid_any", 0),
            "nonfetal_valid_age_sex_UCD_K80_K83": row.get("nonfetal_valid_ucd", 0),
            "nonfetal_valid_age_sex_extra_only_K80_K83": row.get("nonfetal_valid_extra_only", 0),
            "nonfetal_valid_age_sex_any_K80_K83": row.get("nonfetal_valid_any", 0),
        })

    rate_rows, contrast_rows, bounds_meta = rate_bound_rows(
        valid_strata, missing, populations, weights, residence_strata,
    )
    residence_scope_rows = calculate_residence_scope_sensitivity(
        valid_strata, residence_strata, populations, weights,
    )

    OUTPUTS = {
        "residence_missing_country_year.csv": (
            ("country", "year", "raw_records", "residence_contract_records", "analytic_age_sex_records",
             "age_only_invalid_allcause", "sex_only_invalid_allcause", "both_invalid_allcause",
             "disease_ucd_residence_eligible", "disease_extra_only_residence_eligible", "disease_any_residence_eligible",
             "disease_ucd_analytic", "disease_extra_only_analytic", "disease_any_analytic")
            + tuple(
                f"{group}_{endpoint}{suffix}"
                for group in MISSING_GROUPS for endpoint in ("ucd", "extra_only", "any")
                for suffix in ("", "_pct_of_residence_eligible_disease")
            ),
            country_year_rows,
        ),
        "age_sex_invalid_disease_constraints.csv": (
            ("country", "year", "missing_group", "age_parse_status", "sex_parse_status", "known_age_group", "known_sex",
             "UCD_K80_K83", "available_mention_extra_only_K80_K83", "any_available_K80_K83"),
            missing_detail_rows,
        ),
        "residence_code_year_aggregate.csv": (
            tuple(dict.fromkeys(["country", "year", "residence_class", "residence_raw_code_aggregate_only"] + sorted({key for row in residence_code_rows for key in row if key not in {"country", "year", "residence_class", "residence_raw_code_aggregate_only"}}))),
            residence_code_rows,
        ),
        "residence_class_age_sex_eligible_strata.csv": (
            ("country", "year", "residence_class", "age_group", "sex", "age_sex_eligible_allcause", "UCD_K80_K83",
             "available_mention_extra_only_K80_K83", "any_available_K80_K83"),
            residence_strata_rows,
        ),
        "br_tabnet_resident_contract_validation.csv": (
            ("year", "raw_source_contract_allcause_before_age_sex_filter", "TabNet_allcause_residence_reference", "allcause_exact",
             "raw_source_contract_UCD_K80_K83_before_age_sex_filter", "TabNet_UCD_K80_K83_reference", "ucd_exact", "comparison_stage"),
            tabnet_compare_rows,
        ),
        "all_age_and_0_74_bounds.csv": (
            ("period", "country", "endpoint", "complete_case_rate_per_100000", "missing_allocation_min_increment",
             "missing_allocation_max_increment", "rate_lower", "rate_upper", "bound_type"),
            rate_rows,
        ),
        "effect_bounds.csv": (
            ("period", "metric", "complete_case_estimate", "lower_bound", "upper_bound", "allocation_rule", "bound_type"),
            contrast_rows,
        ),
        "under75_BR_vs_US_rate_separation_checks.csv": (
            ("period", "endpoint", "BR_rate_lower", "BR_rate_upper", "US_rate_lower", "US_rate_upper",
             "signed_BR_minus_US_lower", "signed_BR_minus_US_upper", "BR_lower_strictly_exceeds_US_upper",
             "absolute_gap_lower", "absolute_gap_upper"),
            bounds_meta["time_0_74_BR_vs_US_rate_separation_checks"],
        ),
        "br_residence_scope_sensitivity.csv": (
            ("scope", "period", "metric", "estimate"),
            residence_scope_rows,
        ),
        "validation_country_year.csv": (
            ("country", "year", "analytic_age_sex_records_scanned", "prior_raw_ledger_eligible_records", "prior_eligible_count_match",
             "frozen_UCD_any_grid_mismatched_cells", "status"),
            verification_rows,
        ),
    }
    output_manifest = {}
    for name, (fields, rows) in OUTPUTS.items():
        path = WORK_DIR / name
        write_csv(path, fields, rows)
        output_manifest[name] = {"rows": len(rows), "sha256": sha256_file(path), "bytes": path.stat().st_size}

    date_tag = datetime.now().astimezone().strftime("%Y%m%d")
    ibge_path = WORK_DIR / f"ibge_municipality_reference_{date_tag}.json"
    ibge_path.write_bytes(ibge_payload)
    output_manifest[ibge_path.name] = {"bytes": ibge_path.stat().st_size, "sha256": sha256_file(ibge_path)}

    official_window = {
        **tabnet_13_summary,
        "primary_2012_2023_allcause_exact_years": tabnet_primary_all_match,
        "primary_2012_2023_ucd_exact_years": tabnet_primary_ucd_match,
        "primary_2012_2023_years": len(YEARS),
        "primary_window_current_scan_allcause_match": tabnet_primary_all_match == len(YEARS),
        "primary_window_current_scan_ucd_match": tabnet_primary_ucd_match == len(YEARS),
        "interpretation": "TabNet comparison is an aggregate validation against resident counts before age/sex exclusions; exact agreement does not prove saved-line meaning or that every nonempty historical municipality code is domestic.",
    }
    if tabnet_primary_all_match != len(YEARS) or tabnet_primary_ucd_match != len(YEARS):
        raise RuntimeError(f"Current BR 2012-2023 scan fails TabNet resident aggregates: {official_window}")
    if frozen_grid_mismatch_cells or eligible_ledger_mismatch_cells:
        raise RuntimeError(
            f"Raw scan failed replay checks: frozen_grid_mismatches={frozen_grid_mismatch_cells}, "
            f"eligible_ledger_mismatches={eligible_ledger_mismatch_cells}"
        )

    validation = {
        "audit_name": "residence-missing aggregate-only raw audit",
        "run_completed_utc": datetime.now(timezone.utc).isoformat(),
        "years_scanned": list(YEARS),
        "country_year_source_scans": len(scans),
        "parallel_workers": None,
        "source_rows": {
            f"{country}_{year}": source_info[f"{country}_{year}"].get("source_rows")
            for country in ("BR", "US") for year in YEARS
        },
        "tabnet_resident_aggregate_agreement": official_window,
        "prior_full_window_2012_2024_tabnet_validation": tabnet_13_summary,
        "analytic_replay": {
            "all_country_year_eligible_count_matches": eligible_ledger_mismatch_cells == 0,
            "eligible_count_mismatch_country_years": eligible_ledger_mismatch_cells,
            "UCD_any_frozen_grid_mismatched_cells": frozen_grid_mismatch_cells,
        },
        "source_identity_reference": {
            "prior_source_identity_csv": str(FROZEN_SOURCE_IDENTITY),
            "prior_source_identity_csv_sha256": prior_identity_hash,
            "note": "Prior verified SHA256 values are paired with scan-time source integrity metadata when available. Aggregate-cache replay does not reopen raw archives or repeat raw-file hashing.",
        },
        "aggregate_scan_cache": aggregate_cache_info,
        "parser_contracts": {
            "br_parser_path": str(EXT_SCRIPTS / "extract_br.py"),
            "br_parser_sha256": sha256_file(EXT_SCRIPTS / "extract_br.py"),
            "us_parser_path": str(EXT_SCRIPTS / "extract_us.py"),
            "us_parser_sha256": sha256_file(EXT_SCRIPTS / "extract_us.py"),
            "residence": {
                "US": "single-character status 1/2/3 included; 4 and all other codes excluded, per source parser",
                "BR": "non-fetal per parser rule and CODMUNRES nonmissing/nonempty per parser; no geography filter added to the frozen cohort",
            },
            "disease": {
                "UCD": "K80-K83 prefix in selected UCD field/CAUSABAS using the source parser normalization",
                "US_available": "UCD union entity-axis slots only; record axis excluded from primary definition",
                "BR_available": "UCD union saved LINHAA-LINHAD and LINHAII fields using the exact frozen boundary expression",
                "extra_only": "available mention present and UCD absent; mutually exclusive from UCD; any = UCD + extra-only",
            },
            "demographic_branch": "K80-K83 UCD/entity/line fields parsed before classifying age/sex validity, unlike the extraction gate which returns early for invalid demographics",
        },
        "geography_reference": {
            **ibge_meta,
            "official_sentinel_mapping_for_foreign": "none supplied by the reviewed SIM data dictionary; no raw CODMUNRES value was labeled foreign",
            "class_labels": {
                "current_IBGE_municipality_match": "6/7 digit code whose first six digits match current official IBGE code",
                "valid_UF_prefix_unmatched_current_IBGE": "numeric 6/7 digit code with a valid two-digit UF prefix but no current municipality match; not foreign",
                "special_or_unknown_no_official_foreign_mapping": "nonempty value not matched by the above; not foreign",
                "empty_or_missing": "parser missing tokens; excluded by frozen BR residence contract",
            },
        },
        "allcause_flow_totals_scanned": {
            country: {
                "raw_records": sum(int(summary[(country, year)]["raw_records"]) for year in YEARS),
                "residence_contract_records": sum(int(summary[(country, year)]["residence_contract_records"]) for year in YEARS),
                "analytic_age_sex_records": sum(int(summary[(country, year)]["analytic_age_sex_records"]) for year in YEARS),
                "demographic_excluded_records": sum(
                    int(summary[(country, year)]["contract_allcause_age_only_invalid"])
                    + int(summary[(country, year)]["contract_allcause_sex_only_invalid"])
                    + int(summary[(country, year)]["contract_allcause_both_invalid"])
                    for year in YEARS
                ),
            }
            for country in ("US", "BR")
        },
        "disease_totals_scanned": {
            country: {
                "UCD_K80_K83_residence_eligible": sum(int(summary[(country, year)]["contract_ucd"]) for year in YEARS),
                "extra_only_K80_K83_residence_eligible": sum(int(summary[(country, year)]["contract_extra_only"]) for year in YEARS),
                "any_K80_K83_residence_eligible": sum(int(summary[(country, year)]["contract_any"]) for year in YEARS),
                "UCD_K80_K83_analytic": sum(int(summary[(country, year)]["contract_valid_ucd"]) for year in YEARS),
                "any_K80_K83_analytic": sum(int(summary[(country, year)]["contract_valid_any"]) for year in YEARS),
            }
            for country in ("US", "BR")
        },
        "bounds": bounds_meta,
        "residence_scope_sensitivity": {
            "definition": "Brazil nonempty source-contract cohort compared with a conservative sensitivity retaining only age/sex eligible records whose six-digit municipality prefix matches the current IBGE municipality table; current-unmatched codes are excluded only in this sensitivity and are not called foreign.",
            "rows": residence_scope_rows,
        },
        "outputs": output_manifest,
        "limitations": [
            "This deterministic allocation bound is not a confidence interval and does not select a single imputation/allocation.",
            "The 0-74 bound allows unknown-age records to contribute zero by placing them outside the age target; known ages 75+ contribute zero by design.",
            "The current IBGE crosswalk is a contemporary match sensitivity, not a complete historical municipality concordance or foreign-residence classifier.",
            "TabNet agreement checks the aggregate source-contract totals, not SIM individual field semantics or Brazilian saved-line parsing.",
            "No broad model rerun, changes to frozen eligibility, or updates to manuscript files were performed.",
        ],
    }
    if aggregate_cache_info and aggregate_cache_info.get("reconstructed_from_aggregate_outputs"):
        validation["source_identity_reference"]["note"] = (
            "The initial source scan completed before this aggregate-only replay and used the scanner's per-member EOF/CRC checks. "
            "The earlier process did not persist detailed per-member integrity fields, so this reconstructed cache retains source-year row counts and aggregate counters, not the original per-member check values."
        )
        validation["limitations"].append(
            "Final aggregation was reconstructed from aggregate CSVs after the initial raw scan; source-year EOF/CRC checks ran in that scan, but their detailed values were not preserved by the earlier process."
        )
    validation["parallel_workers"] = getattr(build_outputs, "workers", None)
    validation_path = WORK_DIR / "verification.json"
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_manifest["verification.json"] = {"bytes": validation_path.stat().st_size, "sha256": sha256_file(validation_path)}
    print(json.dumps({
        "verification_json": str(validation_path),
        "primary_tabnet": official_window,
        "disease_totals": validation["disease_totals_scanned"],
        "effect_bounds": contrast_rows,
        "outputs": output_manifest,
    }, ensure_ascii=False, indent=2))
    return validation


def reconstruct_scans_from_aggregate_outputs() -> list[dict[str, Any]]:
    """Recreate build_outputs inputs from the preceding aggregate CSV outputs.

    This recovery path is intentionally limited to aggregates. It never opens
    or stores source records and is useful when a post-scan validation or
    sensitivity calculation needs to be regenerated.
    """
    paths = {
        "country_year": WORK_DIR / "residence_missing_country_year.csv",
        "codes": WORK_DIR / "residence_code_year_aggregate.csv",
        "residence_strata": WORK_DIR / "residence_class_age_sex_eligible_strata.csv",
        "missing": WORK_DIR / "age_sex_invalid_disease_constraints.csv",
    }
    absent = [str(path) for path in paths.values() if not path.is_file()]
    if absent:
        raise RuntimeError(f"Cannot rebuild from aggregate outputs; missing table(s): {absent}")

    def rows(path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    country_rows = rows(paths["country_year"])
    code_rows = rows(paths["codes"])
    strata_rows = rows(paths["residence_strata"])
    missing_rows = rows(paths["missing"])
    scans_by_key = {
        (country, year): empty_scan(country, year)
        for country in ("BR", "US") for year in YEARS
    }

    for row in country_rows:
        country, year = row["country"], int(row["year"])
        scan = scans_by_key[(country, year)]
        metrics: Counter = scan["metrics"]
        for field in ("raw_records", "residence_contract_records", "analytic_age_sex_records"):
            metrics[field] = int(row[field])
        metrics["contract_allcause_valid"] = int(row["analytic_age_sex_records"])
        for group, field in (
            ("age_only_invalid", "age_only_invalid_allcause"),
            ("sex_only_invalid", "sex_only_invalid_allcause"),
            ("both_invalid", "both_invalid_allcause"),
        ):
            metrics[f"contract_allcause_{group}"] = int(row[field])
        for outcome, field in (
            ("ucd", "disease_ucd_analytic"),
            ("extra_only", "disease_extra_only_analytic"),
            ("any", "disease_any_analytic"),
        ):
            metrics[f"contract_valid_{outcome}"] = int(row[field])
        for group in MISSING_GROUPS:
            for outcome in ("ucd", "extra_only", "any"):
                metrics[f"contract_{group}_{outcome}"] = int(row[f"{group}_{outcome}"])
        scan["source"] = {"source_rows": int(row["raw_records"])}

    for row in code_rows:
        country, year = row["country"], int(row["year"])
        key = (row["residence_class"], row["residence_raw_code_aggregate_only"])
        counts = scans_by_key[(country, year)]["residence_codes"][key]
        for field, value in row.items():
            if field in {"country", "year", "residence_class", "residence_raw_code_aggregate_only"} or value in (None, ""):
                continue
            counts[field] = int(value)
            if field in {"contract_ucd", "contract_extra_only", "contract_any"}:
                scans_by_key[(country, year)]["metrics"][field] += int(value)

    for row in strata_rows:
        country, year = row["country"], int(row["year"])
        age, sex = row["age_group"], row["sex"]
        klass = row["residence_class"]
        counts = scans_by_key[(country, year)]["residence_strata"][(klass, age, sex)]
        counts["allcause"] += int(row["age_sex_eligible_allcause"])
        counts["disease_ucd"] += int(row["UCD_K80_K83"])
        counts["disease_extra_only"] += int(row["available_mention_extra_only_K80_K83"])
        counts["disease_any"] += int(row["any_available_K80_K83"])
        valid = scans_by_key[(country, year)]["valid_strata"][(age, sex)]
        valid["allcause"] += int(row["age_sex_eligible_allcause"])
        valid["disease_ucd"] += int(row["UCD_K80_K83"])
        valid["disease_extra_only"] += int(row["available_mention_extra_only_K80_K83"])
        valid["disease_any"] += int(row["any_available_K80_K83"])

    for row in missing_rows:
        scan = scans_by_key[(row["country"], int(row["year"]))]
        key = (
            row["missing_group"], row["age_parse_status"], row["sex_parse_status"],
            row["known_age_group"], row["known_sex"],
        )
        counts = scan["missing"][key]
        counts["disease_ucd"] = int(row["UCD_K80_K83"])
        counts["disease_extra_only"] = int(row["available_mention_extra_only_K80_K83"])
        counts["disease_any"] = int(row["any_available_K80_K83"])

    scans = list(scans_by_key.values())
    # Ensure the derived contract totals reconcile to the age/sex partition.
    summary = {(scan["country"], scan["year"]): scan["metrics"] for scan in scans}
    for (country, year), metrics in summary.items():
        for outcome in ("ucd", "extra_only", "any"):
            partitioned = metrics[f"contract_valid_{outcome}"] + sum(
                metrics[f"contract_{group}_{outcome}"] for group in MISSING_GROUPS
            )
            if int(metrics[f"contract_{outcome}"]) != int(partitioned):
                raise RuntimeError(f"Aggregate cache disease partition mismatch for {country}/{year}/{outcome}")
    return sorted(scans, key=lambda item: (item["country"], item["year"]))


def load_saved_ibge_payload() -> tuple[dict[str, Any], bytes]:
    candidates = sorted(
        WORK_DIR.glob("ibge_municipality_reference_*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("No saved official IBGE municipality response is available for aggregate replay")
    path = candidates[0]
    payload = path.read_bytes()
    rows = json.loads(payload.decode("utf-8"))
    codes = {
        str(item.get("id", "")).zfill(7)
        for item in rows
        if str(item.get("id", "")).isdigit() and len(str(item.get("id", ""))) <= 7
    }
    codes = {code for code in codes if len(code) == 7}
    if len(codes) < 5000:
        raise RuntimeError(f"Saved IBGE response appears incomplete: {len(codes)} codes")
    meta = {
        "source_url": IBGE_URL,
        "retrieval": "saved_official_decoded_response_for_aggregate_replay",
        "saved_response_path": str(path),
        "decoded_json_sha256": sha256_bytes(payload),
        "official_7_digit_municipality_codes": len(codes),
        "unique_6_digit_prefixes": len({code[:6] for code in codes}),
        "classification_rule": "match 6- or 7-digit CODMUNRES by its first six digits; historical unmatched values remain unresolved, never foreign by inference",
        "metadata_limitation": "The original retrieval timestamp and HTTP headers were not retained in the aggregate CSV recovery path; the saved official response bytes are hash-identified.",
    }
    return meta, payload


def run_from_aggregate_cache(path: Path) -> dict[str, Any]:
    scans, ibge_meta, ibge_payload = load_aggregate_scan_cache(path)
    prior_identity, prior_identity_hash = load_prior_identity()
    tabnet_rows, tabnet_hash, tabnet_13_summary = load_tabnet_validation()
    cache_info = {
        "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size,
        "completed_scans": len(scans), "complete": True, "replayed_without_raw_source_scan": True,
    }
    return build_outputs(
        scans, ibge_meta, ibge_payload, prior_identity_hash,
        tabnet_rows, tabnet_hash, tabnet_13_summary, cache_info,
    )


def rebuild_from_existing_aggregate_outputs() -> dict[str, Any]:
    scans = reconstruct_scans_from_aggregate_outputs()
    ibge_meta, ibge_payload = load_saved_ibge_payload()
    cache_path = WORK_DIR / "residence_missing_reconstructed_aggregate_cache.json"
    cache_info = write_aggregate_scan_cache(cache_path, scans, True, ibge_meta, ibge_payload)
    cache_info["reconstructed_from_aggregate_outputs"] = True
    prior_identity, prior_identity_hash = load_prior_identity()
    tabnet_rows, tabnet_hash, tabnet_13_summary = load_tabnet_validation()
    cache_info["replayed_without_raw_source_scan"] = True
    return build_outputs(
        scans, ibge_meta, ibge_payload, prior_identity_hash,
        tabnet_rows, tabnet_hash, tabnet_13_summary, cache_info,
    )


def run(workers: int) -> dict[str, Any]:
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    ibge_prefixes, ibge_meta, ibge_payload = fetch_ibge_reference()
    prior_identity, prior_identity_hash = load_prior_identity()
    tabnet_rows, tabnet_hash, tabnet_13_summary = load_tabnet_validation()

    tasks = [("BR", year) for year in YEARS] + [("US", year) for year in YEARS]
    scans: list[dict[str, Any]] = []
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cache_path = WORK_DIR / f"residence_missing_scan_aggregate_cache_{run_tag}.json"
    print(f"Starting {len(tasks)} one-pass source scans with {workers} worker threads", flush=True)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="residence-audit") as executor:
        future_map = {}
        for country, year in tasks:
            if country == "BR":
                future = executor.submit(scan_br_year, year, prior_identity[(country, year)], ibge_prefixes)
            else:
                future = executor.submit(scan_us_year, year, prior_identity[(country, year)])
            future_map[future] = (country, year)
        for future in as_completed(future_map):
            country, year = future_map[future]
            try:
                scans.append(future.result())
                cache_info = write_aggregate_scan_cache(
                    cache_path, scans, len(scans) == len(tasks), ibge_meta, ibge_payload,
                )
            except Exception as exc:
                for pending in future_map:
                    pending.cancel()
                raise RuntimeError(f"Raw scan failed for {country}/{year}: {type(exc).__name__}: {exc}") from exc
    scans.sort(key=lambda item: (item["country"], item["year"]))
    build_outputs.workers = workers
    cache_info["replayed_without_raw_source_scan"] = False
    return build_outputs(
        scans, ibge_meta, ibge_payload, prior_identity_hash,
        tabnet_rows, tabnet_hash, tabnet_13_summary, cache_info,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, help="I/O workers (hard maximum 8; default 4)")
    parser.add_argument("--use-aggregate-cache", type=Path, help="rebuild outputs from a complete aggregate-only scan cache")
    parser.add_argument("--rebuild-from-aggregate-csvs", action="store_true", help="rebuild outputs from existing aggregate CSV tables without reopening raw archives")
    args = parser.parse_args()
    if args.use_aggregate_cache and args.rebuild_from_aggregate_csvs:
        parser.error("choose only one aggregate replay mode")
    try:
        if args.use_aggregate_cache:
            run_from_aggregate_cache(args.use_aggregate_cache)
        elif args.rebuild_from_aggregate_csvs:
            rebuild_from_existing_aggregate_outputs()
        else:
            run(args.workers)
    except Exception as exc:
        print(f"AUDIT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
