from collections import Counter
import unittest

import run_retained_m1_m3 as pilot


class RetainedM1M3Tests(unittest.TestCase):
    def test_ucd_groups_and_fixed_cancer_subgroups(self):
        self.assertEqual(pilot.ucd_group("C22.1"), "malignant_C00_C97")
        self.assertEqual(pilot.ucd_group("D48.9"), "other_neoplasm_D00_D48")
        self.assertEqual(pilot.ucd_group("D50.0"), "nonneoplasm")
        self.assertEqual(pilot.ucd_group("C98.0"), "unknown_invalid")
        self.assertEqual(pilot.ucd_detail("C22.1"), "C22_1")
        self.assertEqual(pilot.ucd_detail("C22.9"), "other_C22")
        self.assertEqual(pilot.ucd_detail("C23.0"), "C23")
        self.assertEqual(pilot.ucd_detail("C24.0"), "C24")
        self.assertEqual(pilot.ucd_detail("C25.9"), "C25")

    def test_source_any_uses_comparable_axes_and_record_union_is_separate(self):
        us = {
            "ucd_code": "C80.1",
            "entity_codes": '["K83.1", "C18.9"]',
            "record_codes": '["C23.0"]',
        }
        self.assertEqual(pilot.source_any_codes(us, "US"), {"C801", "K831", "C189"})
        self.assertEqual(pilot.us_record_union_codes(us), {"C801", "K831", "C189", "C230"})
        br = {
            "ucd_code": "K80.2",
            "line_codes": '[{"field":"I","code":"C23.0"},{"field":"II","code":"K82.9"}]',
        }
        self.assertEqual(pilot.source_any_codes(br, "BR"), {"K802", "C230", "K829"})

    def test_m2_definition_filter_effects_are_nested_and_distinct(self):
        records = (
            {"ucd": "C23.0", "codes": {"C230", "K831"}},
            {"ucd": "K83.1", "codes": {"K831", "C189"}},
            {"ucd": "K83.0", "codes": {"K830"}},
        )
        included = {
            definition: [
                record for record in records
                if pilot.include_m2_case_definition(definition, record["ucd"], record["codes"])
            ]
            for definition in pilot.M2_DEFINITIONS
        }
        counts = {definition: len(rows) for definition, rows in included.items()}
        self.assertEqual(counts["full"], 3)
        self.assertEqual(counts["exclude_malignant_UCD"], 2)
        self.assertEqual(counts["no_malignancy_source_any"], 1)
        self.assertLessEqual(counts["no_malignancy_source_any"], counts["exclude_malignant_UCD"])
        self.assertLessEqual(counts["exclude_malignant_UCD"], counts["full"])
        self.assertLess(counts["no_malignancy_source_any"], counts["exclude_malignant_UCD"])
        self.assertLess(counts["exclude_malignant_UCD"], counts["full"])

        malignant_ucd, malignancy_mentioned_nonmalignant_ucd, clean_biliary = records
        self.assertFalse(pilot.include_m2_case_definition("exclude_malignant_UCD", malignant_ucd["ucd"], malignant_ucd["codes"]))
        self.assertFalse(pilot.include_m2_case_definition("no_malignancy_source_any", malignant_ucd["ucd"], malignant_ucd["codes"]))
        self.assertTrue(pilot.include_m2_case_definition("exclude_malignant_UCD", malignancy_mentioned_nonmalignant_ucd["ucd"], malignancy_mentioned_nonmalignant_ucd["codes"]))
        self.assertFalse(pilot.include_m2_case_definition("no_malignancy_source_any", malignancy_mentioned_nonmalignant_ucd["ucd"], malignancy_mentioned_nonmalignant_ucd["codes"]))
        self.assertTrue(pilot.include_m2_case_definition("no_malignancy_source_any", clean_biliary["ucd"], clean_biliary["codes"]))

    def test_biliary_positions_and_family_set_are_ordered(self):
        self.assertEqual(pilot.source_position({"part_i_biliary": '["K83.1"]', "part_ii_biliary": "[]"}, "US"), "only_part_i")
        self.assertEqual(pilot.source_position({"part_i_biliary": "0", "part_ii_biliary": "1"}, "BR"), "only_part_ii")
        self.assertEqual(pilot.source_position({"part_i_biliary": "1", "part_ii_biliary": "1"}, "BR"), "both_part_i_ii")
        self.assertEqual(pilot.source_position({"part_i_biliary": "0", "part_ii_biliary": "0"}, "BR"), "neither_captured")
        self.assertEqual(pilot.family_set({"K83.1", "K80.2", "K82.9"}), "K80|K82|K83")

    def test_group_matching_supports_per_death_union_endpoints(self):
        codes = {"K802", "K810", "K829", "K831"}
        self.assertTrue(any(pilot.matches_group(code, "K80-81") for code in codes))
        self.assertTrue(any(pilot.matches_group(code, "K82-83_context") for code in codes))
        self.assertTrue(any(pilot.matches_group(code, "K80-83_total") for code in codes))
        self.assertFalse(pilot.matches_group("K831", "K83.0"))
        self.assertTrue(pilot.matches_group("K8301", "K83.0"))

    def test_under_75_standardization_renormalizes_age_weights(self):
        weights = {age: 1.0 / len(pilot.AGES) for age in pilot.AGES}
        population = {
            ("US", year, age, sex): {"population": 1000, "ucd": 0, "any_common": 0}
            for year in (2012, 2013) for age in pilot.AGES for sex in pilot.SEXES
        }
        counts = Counter({("US", 2012, "0-4", "F", "endpoint"): 10})
        got = pilot.standard_rate(
            counts, "US", ("endpoint",), population, weights,
            years=(2012, 2013), age_scope="0_74",
            key_builder=lambda c, y, a, s, category: (c, y, a, s, category[0]),
        )
        renormalized_age_weight = weights["0-4"] / sum(weights[age] for age in pilot.AGES_0_74)
        expected = 0.5 * 0.5 * renormalized_age_weight * (10 / 1000) * 100_000
        self.assertAlmostEqual(got, expected, places=12)

    def test_m1_joint_partition_closes_with_zero_filled_categories(self):
        cell = ("US", 2012, "0-4", "F")
        base = Counter({cell + ("extra",): 1})
        joint = Counter({cell + ("K83", "only_part_i", "malignant_C00_C97"): 1})
        result = pilot.verify_m1_closure(base, joint, [cell])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["zero_filled_joint_combinations"], 240)

    def test_m2_checks_union_counts_once_per_death(self):
        cell = ("US", 2012, "0-4", "F")
        base = Counter({cell + ("candidate",): 3})
        base[cell + ("m2_definition", "full")] = 3
        base[cell + ("m2_definition", "exclude_malignant_UCD")] = 2
        base[cell + ("m2_definition", "no_malignancy_source_any")] = 1
        counts = Counter()
        for definition in pilot.M2_DEFINITIONS:
            for group in pilot.M2_COUNT_GROUPS + ("K80-83_total",):
                counts[cell + (definition, group, "UCD")] = 0
                counts[cell + (definition, group, "any_mention")] = 0
            count = base[cell + ("m2_definition", definition)]
            for group in ("K83", "K82-83_context", "K80-83_total"):
                counts[cell + (definition, group, "any_mention")] = count
        result = pilot.verify_m2_dedup(counts, base, [cell])
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
