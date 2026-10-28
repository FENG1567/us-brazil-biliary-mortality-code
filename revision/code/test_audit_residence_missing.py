import importlib.util
import csv
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


AUDIT_PATH = Path(__file__).with_name("audit_residence_missing.py")
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import revision_adapter
# Relocate only parser-directory constants before the original module executes.
# Scientific test cases below are unchanged and use synthetic data only.
AUDIT = revision_adapter._module_from_source(
    AUDIT_PATH, "audit_residence_missing_under_test",
    {"EXT_SCRIPTS": "_portable_repo / 'helpers/extension/scripts'"},
    {"_portable_repo": REPO_ROOT},
)


class ContractMetricTests(unittest.TestCase):
    def test_residence_eligible_totals_equal_age_sex_partitions(self):
        result = AUDIT.empty_scan("BR", 2012)
        AUDIT.add_contract_record(
            result,
            "current_IBGE_municipality_match",
            "1100015",
            None,
            "invalid_value",
            "M",
            "valid_male",
            True,
            False,
        )
        AUDIT.add_contract_record(
            result,
            "current_IBGE_municipality_match",
            "1100015",
            "0-4",
            "valid_age",
            "F",
            "valid_female",
            False,
            True,
        )

        metrics = result["metrics"]
        self.assertEqual(metrics["contract_ucd"], 1)
        self.assertEqual(metrics["contract_extra_only"], 1)
        self.assertEqual(metrics["contract_any"], 2)
        self.assertEqual(
            metrics["contract_ucd"],
            sum(metrics[f"contract_{group}_ucd"] for group in AUDIT.MISSING_GROUPS)
            + metrics["contract_valid_ucd"],
        )
        self.assertEqual(
            metrics["contract_any"],
            sum(metrics[f"contract_{group}_any"] for group in AUDIT.MISSING_GROUPS)
            + metrics["contract_valid_any"],
        )

    def test_residence_classes_do_not_infer_foreign_brazilian_codes(self):
        prefixes = {"110001"}
        self.assertEqual(
            AUDIT.residence_class_br("1100015", prefixes)[0],
            "current_IBGE_municipality_match",
        )
        self.assertEqual(
            AUDIT.residence_class_br("1199999", prefixes)[0],
            "valid_UF_prefix_unmatched_current_IBGE",
        )
        self.assertEqual(
            AUDIT.residence_class_br("9999999", prefixes)[0],
            "special_or_unknown_no_official_foreign_mapping",
        )

    def test_unknown_age_has_zero_feasible_minimum_for_under75_bounds(self):
        populations = {
            (country, year, age, sex): 100_000
            for country in ("US", "BR")
            for year in AUDIT.YEARS
            for age in AUDIT.AGE_GROUPS
            for sex in AUDIT.SEXES
        }
        weights = {age: 1 / len(AUDIT.AGE_GROUPS) for age in AUDIT.AGE_GROUPS}
        valid_strata = {
            (country, year, age, sex): Counter({"disease_ucd": 1, "disease_extra_only": 1})
            for country in ("US", "BR")
            for year in AUDIT.YEARS
            for age in AUDIT.AGE_GROUPS
            for sex in AUDIT.SEXES
        }
        missing = {
            ("US", year, ("age_only_invalid", "invalid_value", "valid_male", "<unknown>", "M")): Counter(
                {"disease_ucd": 1, "disease_extra_only": 0}
            )
            for year in AUDIT.YEARS
        }

        direct_bounds = AUDIT.missing_rate_bounds(
            "US", (2012, 2013, 2014), True, missing, populations, weights
        )
        self.assertEqual(direct_bounds["ucd"]["min"], 0.0)
        self.assertGreater(direct_bounds["ucd"]["max"], 0.0)

        rate_rows, contrast_rows, bounds_meta = AUDIT.rate_bound_rows(
            valid_strata, missing, populations, weights, {}
        )
        early_ucd = next(
            row for row in rate_rows
            if row["period"] == "early_2012_2014" and row["country"] == "US" and row["endpoint"] == "ucd"
        )
        self.assertEqual(early_ucd["missing_allocation_min_increment"], 0.0)
        separation = bounds_meta["time_0_74_BR_vs_US_rate_separation_checks"]
        self.assertEqual(len(separation), 4)
        self.assertFalse(bounds_meta["time_0_74_all_BR_lower_exceeds_US_upper_for_UCD_and_any"])
        absolute_ucd_gap = next(
            row for row in contrast_rows
            if row["metric"] == "absolute_gap_UCD_rate_BR_vs_US_per_100000"
            and row["period"] == "early_2012_2014_age_0_74"
        )
        self.assertEqual(absolute_ucd_gap["lower_bound"], 0.0)
        self.assertGreater(absolute_ucd_gap["upper_bound"], 0.0)

    def test_aggregate_cache_round_trips_counters_without_record_rows(self):
        scans = [
            AUDIT.empty_scan(country, year)
            for country in ("BR", "US") for year in AUDIT.YEARS
        ]
        sample = next(scan for scan in scans if scan["country"] == "BR" and scan["year"] == 2012)
        sample["metrics"]["raw_records"] = 12
        sample["source"] = {"source_rows": 12, "crc32_verified": True}
        AUDIT.add_contract_record(
            sample, "current_IBGE_municipality_match", "1100015",
            None, "invalid_value", "M", "valid_male", True, False,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "scan_cache.json"
            AUDIT.write_aggregate_scan_cache(cache_path, scans, True, {"source": "test"}, b"[]")
            raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            restored, meta, payload = AUDIT.load_aggregate_scan_cache(cache_path)

        restored_sample = next(scan for scan in restored if scan["country"] == "BR" and scan["year"] == 2012)
        self.assertFalse(raw_cache["record_level_rows_included"])
        self.assertEqual(meta, {"source": "test"})
        self.assertEqual(payload, b"[]")
        self.assertEqual(restored_sample["metrics"]["raw_records"], 12)
        self.assertEqual(restored_sample["metrics"]["contract_ucd"], 1)
        self.assertEqual(
            restored_sample["residence_codes"][("current_IBGE_municipality_match", "1100015")]["contract_ucd"],
            1,
        )

    def test_aggregate_csv_reconstruction_restores_all_build_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_work_dir = AUDIT.WORK_DIR
            AUDIT.WORK_DIR = Path(temp_dir)
            try:
                country_fields = [
                    "country", "year", "raw_records", "residence_contract_records", "analytic_age_sex_records",
                    "age_only_invalid_allcause", "sex_only_invalid_allcause", "both_invalid_allcause",
                    "disease_ucd_residence_eligible", "disease_extra_only_residence_eligible",
                    "disease_any_residence_eligible", "disease_ucd_analytic",
                    "disease_extra_only_analytic", "disease_any_analytic",
                ]
                for group in AUDIT.MISSING_GROUPS:
                    for endpoint in ("ucd", "extra_only", "any"):
                        country_fields.append(f"{group}_{endpoint}")
                code_fields = [
                    "country", "year", "residence_class", "residence_raw_code_aggregate_only",
                    "source_records", "contract_records", "contract_ucd", "contract_extra_only", "contract_any",
                    "contract_valid_age_sex_records", "contract_valid_ucd", "contract_valid_extra_only", "contract_valid_any",
                ]
                strata_fields = [
                    "country", "year", "residence_class", "age_group", "sex", "age_sex_eligible_allcause",
                    "UCD_K80_K83", "available_mention_extra_only_K80_K83", "any_available_K80_K83",
                ]
                missing_fields = [
                    "country", "year", "missing_group", "age_parse_status", "sex_parse_status",
                    "known_age_group", "known_sex", "UCD_K80_K83",
                    "available_mention_extra_only_K80_K83", "any_available_K80_K83",
                ]
                country_rows, code_rows, strata_rows, missing_rows = [], [], [], []
                for country in ("BR", "US"):
                    for year in AUDIT.YEARS:
                        country_row = {
                            "country": country, "year": year, "raw_records": 10,
                            "residence_contract_records": 3, "analytic_age_sex_records": 1,
                            "age_only_invalid_allcause": 2, "sex_only_invalid_allcause": 0,
                            "both_invalid_allcause": 0, "disease_ucd_residence_eligible": 2,
                            "disease_extra_only_residence_eligible": 1, "disease_any_residence_eligible": 3,
                            "disease_ucd_analytic": 1, "disease_extra_only_analytic": 0,
                            "disease_any_analytic": 1,
                        }
                        for group in AUDIT.MISSING_GROUPS:
                            for endpoint in ("ucd", "extra_only", "any"):
                                country_row[f"{group}_{endpoint}"] = (
                                    {"ucd": 1, "extra_only": 1, "any": 2}[endpoint]
                                    if group == "age_only_invalid" else 0
                                )
                        country_rows.append(country_row)
                        code_rows.append({
                            "country": country, "year": year,
                            "residence_class": "US_resident_status_1_3" if country == "US" else "current_IBGE_municipality_match",
                            "residence_raw_code_aggregate_only": "1" if country == "US" else "1100015",
                            "source_records": 10, "contract_records": 3, "contract_ucd": 2,
                            "contract_extra_only": 1, "contract_any": 3,
                            "contract_valid_age_sex_records": 1, "contract_valid_ucd": 1,
                            "contract_valid_extra_only": 0, "contract_valid_any": 1,
                        })
                        strata_rows.append({
                            "country": country, "year": year,
                            "residence_class": "US_resident_status_1_3" if country == "US" else "current_IBGE_municipality_match",
                            "age_group": "0-4", "sex": "F", "age_sex_eligible_allcause": 1,
                            "UCD_K80_K83": 1, "available_mention_extra_only_K80_K83": 0,
                            "any_available_K80_K83": 1,
                        })
                        missing_rows.append({
                            "country": country, "year": year, "missing_group": "age_only_invalid",
                            "age_parse_status": "invalid_value", "sex_parse_status": "valid_female",
                            "known_age_group": "<unknown>", "known_sex": "F", "UCD_K80_K83": 1,
                            "available_mention_extra_only_K80_K83": 1, "any_available_K80_K83": 2,
                        })

                for filename, fields, data in (
                    ("residence_missing_country_year.csv", country_fields, country_rows),
                    ("residence_code_year_aggregate.csv", code_fields, code_rows),
                    ("residence_class_age_sex_eligible_strata.csv", strata_fields, strata_rows),
                    ("age_sex_invalid_disease_constraints.csv", missing_fields, missing_rows),
                ):
                    with (AUDIT.WORK_DIR / filename).open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
                        writer.writeheader()
                        writer.writerows(data)

                scans = AUDIT.reconstruct_scans_from_aggregate_outputs()
                sample = next(scan for scan in scans if scan["country"] == "BR" and scan["year"] == 2012)
                self.assertEqual(sample["metrics"]["contract_ucd"], 2)
                self.assertEqual(sample["metrics"]["contract_valid_ucd"], 1)
                self.assertEqual(sample["metrics"]["contract_age_only_invalid_ucd"], 1)
                self.assertEqual(sample["missing"][
                    ("age_only_invalid", "invalid_value", "valid_female", "<unknown>", "F")
                ]["disease_any"], 2)
                self.assertEqual(sample["valid_strata"][("0-4", "F")]["disease_ucd"], 1)
            finally:
                AUDIT.WORK_DIR = original_work_dir


if __name__ == "__main__":
    unittest.main()
