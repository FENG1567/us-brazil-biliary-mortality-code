import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_publication_tables as tables


class PublicationTableUnitTests(unittest.TestCase):
    def test_excluded_payload_manifest_requires_the_recorded_hash(self):
        expected_hash = "a" * 64
        with tempfile.TemporaryDirectory() as temporary_directory:
            analysis_dir = Path(temporary_directory)
            excluded_identity = analysis_dir / "payload_identity.json"
            excluded_identity.write_text(
                json.dumps({
                    "schema": "excluded_prepared_payload_identity_v1",
                    "payload_included": False,
                    "payload_manifest_bytes": 123,
                    "payload_manifest_sha256": expected_hash,
                }),
                encoding="utf-8",
            )

            observed = tables.resolve_payload_identity(
                analysis_dir / "prepared_payload" / "payload_manifest.json",
                excluded_identity,
                expected_hash,
            )

            self.assertEqual(observed["sha256"], expected_hash)
            self.assertEqual(observed["bytes"], 123)
            self.assertEqual(observed["path"], "EXCLUDED_FROM_PORTABLE_PACKAGE")
            self.assertIn("PREPARED_PAYLOAD_AND_MANIFEST_EXCLUDED", observed["status"])
            with self.assertRaises(tables.TableBuildError):
                tables.resolve_payload_identity(
                    analysis_dir / "prepared_payload" / "payload_manifest.json",
                    excluded_identity,
                    "b" * 64,
                )

    def test_relative_source_map_snapshots_resolve_from_package_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            snapshot = project_root / "02_analysis" / "reused" / "sample.csv"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text("key,value\na,1\n", encoding="utf-8")
            source_map_path = project_root / "01_inventory" / "SOURCE_MAP.json"
            source_map_path.parent.mkdir(parents=True)
            source_map_path.write_text(
                json.dumps({
                    "run_id": tables.RUN_ID,
                    "items": [{
                        "category": "test",
                        "source": "sample.csv",
                        "snapshot": "02_analysis/reused/sample.csv",
                        "sha256": tables.sha256_file(snapshot),
                        "bytes": snapshot.stat().st_size,
                        "status": "reused",
                    }],
                }),
                encoding="utf-8",
            )
            loaded = {
                "source_map": source_map_path,
                "m1_joint_contributions": snapshot,
                "m1_focus": snapshot,
                "m3_contrasts": snapshot,
                "m3_rates": snapshot,
                "time_annual_rates": snapshot,
                "time_monitoring_points": snapshot,
                "time_primary_intervals": snapshot,
            }

            _, identities = tables.validate_source_map(project_root, loaded)

            self.assertEqual(identities["m1_joint_contributions"]["sha256"], tables.sha256_file(snapshot))

    def test_m1_top_plus_remainder_closes_exact_signed_sum(self):
        families = [f"F{i:02d}" for i in range(15)]
        positions = ("only_part_i", "only_part_ii", "both_part_i_ii", "neither_captured")
        ucd_groups = ("malignant_C00_C97", "other_neoplasm_D00_D48", "nonneoplasm", "unknown")
        joint_rows = []
        index = 0
        for family in families:
            for position in positions:
                for ucd_group in ucd_groups:
                    contribution = float(index - 120 + (121 if index == 0 else 0))
                    joint_rows.append({
                        "period": tables.PRIMARY_WINDOW,
                        "family_set": family,
                        "position_group": position,
                        "ucd_group": ucd_group,
                        "BR_deaths": "1",
                        "US_deaths": "1",
                        "BR_std_rate_per_100k": "0.1",
                        "US_std_rate_per_100k": "0.1",
                        "DD_contribution_BR_minus_US_per_100k": repr(contribution),
                    })
                    index += 1
        focus_rows = [
            {
                "period": tables.PRIMARY_WINDOW,
                "focus_definition": focus_definition,
                "ucd_group": tables.M1_FOCUS_UCD,
                "BR_deaths": "1",
                "US_deaths": "1",
                "BR_std_rate_per_100k": "0.5",
                "US_std_rate_per_100k": "0.0",
                "DD_contribution_BR_minus_US_per_100k": "0.5",
            }
            for focus_definition in (
                tables.M1_FOCUS_DEFINITION,
                "K83_in_family_set__any_PartI",
                "K83_only_family_set__only_PartI",
            )
        ]
        full, compact, focus, summary = tables.build_m1_tables(
            joint_rows,
            focus_rows,
            {"DD_A_per_100k": "1.0", "RD_U_per_100k": "1.9"},
        )

        self.assertEqual(len(full), 240)
        self.assertEqual(len(compact), 21)
        self.assertEqual(summary["closure_status"], "PASS")
        self.assertAlmostEqual(summary["signed_contribution_sum_per_100k"], 1.0)
        self.assertAlmostEqual(summary["top_plus_remainder_sum_per_100k"], 1.0)
        self.assertAlmostEqual(summary["focus_share_of_primary_DD_pct"], 50.0)
        self.assertTrue(all(row.get("estimand_id") and row.get("contribution_unit") for row in compact))
        self.assertEqual(focus[0]["focus_definition"], tables.M1_FOCUS_DEFINITION)

    def test_metric_units_distinguish_log_ratio_from_rate_difference(self):
        self.assertEqual(tables.interval_unit("H_logRRR_K83.0_minus_K83.1"), "log ratio")
        self.assertEqual(tables.interval_unit("RRR"), "ratio")
        self.assertEqual(tables.interval_unit("DD"), "deaths per 100000 population")

    def test_primary_record_restrictions_use_exact_all_age_keys(self):
        source_rows = []
        for case_definition in tables.RECORD_RESTRICTION_CASES:
            for endpoint in tables.RECORD_RESTRICTION_ENDPOINTS:
                source_rows.append({
                    "period": tables.PRIMARY_WINDOW,
                    "age_scope": "all_age",
                    "case_definition": case_definition,
                    "code_group": tables.PRIMARY_GROUP,
                    "endpoint": endpoint,
                    "BR_rate_per_100k": "2.0",
                    "US_rate_per_100k": "1.0",
                    "BR_minus_US_rate_difference_per_100k": "0.5",
                    "BR_over_US_rate_ratio": "1.5",
                })
        source_rows.extend([
            {**source_rows[0], "age_scope": "0_74"},
            {**source_rows[0], "period": tables.SUPPORT_WINDOW},
            {**source_rows[0], "code_group": "K83"},
        ])

        output = tables.build_primary_record_restrictions(source_rows)

        self.assertEqual(len(output), 9)
        self.assertEqual({row["age_scope"] for row in output}, {"all_age"})
        self.assertEqual({row["period"] for row in output}, {tables.PRIMARY_WINDOW})
        self.assertEqual({row["endpoint_scale_label"] for row in output}, {"U", "A", "DD"})
        self.assertTrue(all(row["ci_status"] == "NOT_COMPUTED_IN_REUSED_M3_TABLE" for row in output))
        self.assertTrue(all(row["difference_estimand_id"] and row["ratio_estimand_id"] for row in output))


if __name__ == "__main__":
    unittest.main()
