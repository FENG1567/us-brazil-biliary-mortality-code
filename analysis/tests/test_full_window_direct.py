from __future__ import annotations

import gc
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

import numpy as np

ANALYSIS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ANALYSIS_DIR))

import compute_full_window as compute
import prepare_full_window as prepare


class SyntheticBridge:
    @staticmethod
    def norm_code(value):
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    @staticmethod
    def matches_group(code, group):
        code = SyntheticBridge.norm_code(code)
        if group == "K80-83_total":
            return code[:3] in {"K80", "K81", "K82", "K83"}
        if group in {"K80", "K81", "K82", "K83"}:
            return code.startswith(group)
        if group == "K80-81":
            return code[:3] in {"K80", "K81"}
        if group == "K83.0":
            return code.startswith("K830")
        if group == "K83.1":
            return code.startswith("K831")
        if group == "K82.9":
            return code.startswith("K829")
        raise AssertionError(group)


def synthetic_patterns() -> tuple[list[dict], dict, dict]:
    bridge = SyntheticBridge()

    def m(codes):
        return prepare.mask_for_codes(codes, bridge)

    u830 = m(["K83.0"])
    u831 = m(["K83.1"])
    patterns = []
    population = {}
    for country in ("US", "BR"):
        for year in range(2012, 2025):
            cell = (country, year, "0-4", "F")
            population[cell] = 10_000
            if country == "US":
                rows = [
                    (u830, u830, u830, 10),
                    (u831, u831, u831, 5),
                    (0, u830, 0, 10),
                    (0, u831, 0, 5),
                ]
            else:
                rows = [
                    (u830, u830, None, 20),
                    (u831, u831, None, 10),
                    (0, u830, None, 40),
                    (0, u831, None, 5),
                ]
            for u_mask, a_mask, r_mask, count in rows:
                patterns.append({
                    "country": country, "year": year, "age_group": "0-4", "sex": "F",
                    "U_mask": u_mask, "A_mask": a_mask, "R_mask": r_mask,
                    "malignant_UCD": 0, "malignant_A": 0, "malignant_R": 0 if country == "US" else None,
                    "N_pattern": count,
                })
    return patterns, population, {"0-4": 1.0}


class DirectInferenceUnitTests(unittest.TestCase):
    def test_group_masks_are_bounded_and_overlapping(self):
        bridge = SyntheticBridge()
        k80 = prepare.mask_for_codes(["K80.20"], bridge)
        self.assertEqual(k80 & prepare.GROUP_BITS["K80-83_total"], prepare.GROUP_BITS["K80-83_total"])
        self.assertEqual(k80 & prepare.GROUP_BITS["K80"], prepare.GROUP_BITS["K80"])
        self.assertEqual(k80 & prepare.GROUP_BITS["K80-81"], prepare.GROUP_BITS["K80-81"])
        self.assertEqual(prepare.mask_for_codes(["K83.0"], bridge), prepare.mask_for_codes(["K83.0", "K83.0"], bridge))
        self.assertEqual(prepare.mask_for_codes(["XK80.2"], bridge), 0)

    def test_parser_flags_are_not_silently_coerced(self):
        self.assertTrue(prepare.parse_parser_flag("1", "any_common"))
        self.assertFalse(prepare.parse_parser_flag("0", "any_common"))
        with self.assertRaises(prepare.PreparationError):
            prepare.parse_parser_flag("maybe", "any_common")

    def test_invalid_thread_budget_is_rejected_before_numpy_import(self):
        compute_path = ANALYSIS_DIR / "compute_full_window.py"
        env = os.environ.copy()
        env["USBR_NUM_THREADS"] = "9"
        code = (
            "import runpy,sys\n"
            "sys.argv=['compute_full_window.py','--help']\n"
            f"p={str(compute_path)!r}\n"
            "try:\n"
            "    runpy.run_path(p)\n"
            "except SystemExit as e:\n"
            "    print(str(e))\n"
            "    print('numpy_loaded=' + str('numpy' in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USBR_NUM_THREADS must be an integer from 1 to 8", result.stdout)
        self.assertIn("numpy_loaded=False", result.stdout)

    def test_default_thread_budget_is_eight_and_sets_native_environment(self):
        env = os.environ.copy()
        env.pop("USBR_NUM_THREADS", None)
        code = (
            "import json,os,sys\n"
            f"sys.path.insert(0,{str(ANALYSIS_DIR)!r})\n"
            "import compute_full_window as c\n"
            "print(json.dumps({'budget':c.NATIVE_THREAD_BUDGET,'env':{k:os.environ.get(k) for k in c.NATIVE_THREAD_ENV_VARS}}))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = __import__("json").loads(result.stdout)
        self.assertEqual(observed["budget"], 8)
        self.assertTrue(all(value == "8" for value in observed["env"].values()))

    def test_aggregate_poisson_count_matches_sum_of_death_weights(self):
        # The construction uses Pois(N) for N identical records. By Poisson
        # additivity this has the same law as summing N independent Pois(1)s.
        reps, multiplicity = 30_000, 4
        grouped_rng = np.random.default_rng(93)
        individual_rng = np.random.default_rng(94)
        grouped = grouped_rng.poisson(lam=multiplicity, size=reps)
        individual = individual_rng.poisson(lam=1.0, size=(reps, multiplicity)).sum(axis=1)
        self.assertLess(abs(float(grouped.mean()) - multiplicity), 0.07)
        self.assertLess(abs(float(individual.mean()) - multiplicity), 0.07)
        self.assertLess(abs(float(grouped.var()) - multiplicity), 0.20)
        self.assertLess(abs(float(individual.var()) - multiplicity), 0.20)
        self.assertLess(abs(float(grouped.mean()) - float(individual.mean())), 0.10)

    def test_point_estimands_identity_and_k83_heterogeneity(self):
        patterns, population, weights = synthetic_patterns()
        design = compute.build_rate_design(patterns, population, weights)
        point_rows, values = compute.point_estimate_rows(design)
        row = next(
            x for x in point_rows
            if x["window"] == "2012-2023" and x["code_group"] == "K83.0" and x["axis_variant"] == "entity_any"
        )
        self.assertAlmostEqual(row["RRR"], row["E_BR"] / row["E_US"], places=12)
        self.assertAlmostEqual(values[("RRR", "2012-2023", "K83.0", "entity_any")], 1.5, places=12)
        self.assertAlmostEqual(values[("RRR", "2012-2023", "K83.1", "entity_any")], 0.75, places=12)
        h = values[("H_logRRR_K83.0_minus_K83.1", "2012-2023", "K83.0_vs_K83.1", "entity_any")]
        self.assertAlmostEqual(h, math.log(2.0), places=12)

    def test_shared_paired_draws_give_rrr_one_and_dd_zero_when_a_equals_u(self):
        bridge = SyntheticBridge()
        mask = prepare.mask_for_codes(["K80.2"], bridge)
        patterns = []
        population = {}
        weights = {"0-4": 1.0}
        for country, count in (("US", 11), ("BR", 29)):
            for year in range(2012, 2025):
                population[(country, year, "0-4", "F")] = 20_000
                patterns.append({
                    "country": country, "year": year, "age_group": "0-4", "sex": "F",
                    "U_mask": mask, "A_mask": mask, "R_mask": mask if country == "US" else None,
                    "malignant_UCD": 0, "malignant_A": 0, "malignant_R": 0 if country == "US" else None,
                    "N_pattern": count,
                })
        design = compute.build_rate_design(patterns, population, weights)
        keys, draws, meta = compute.run_bootstrap(design, reps=64, seed=20261025, memory_limit_mib=32)
        by_key = {key: draws[:, i] for i, key in enumerate(keys)}
        self.assertTrue(np.allclose(by_key[("RRR", "2012-2023", "K80", "entity_any")], 1.0))
        self.assertTrue(np.allclose(by_key[("DD", "2012-2023", "K80", "entity_any")], 0.0))
        self.assertEqual(meta["resampling"].split(";")[0], "independent Poisson(N_pattern) by stratum/joint-pattern")
        self.assertLessEqual(meta["chunk_size"], 200)

    def test_multiplicity_families_and_percentile_policy_are_separate(self):
        self.assertEqual(
            compute.adjusted_family(("RRR", "2012-2023", "K80-83_total", "entity_any")),
            (compute.PRIMARY_FAMILY_ID, 2, 0.975),
        )
        family = compute.adjusted_family(("RRR", "2012-2023", "K80", "entity_any"))
        self.assertEqual(family[0:2], (compute.SECONDARY_FAMILY_ID, 9))
        self.assertAlmostEqual(family[2], 0.9944444444444445)
        self.assertIsNone(compute.adjusted_family(("RRR", "2012-2024", "K80", "entity_any")))
        keys = [
            ("RRR", "2012-2023", "K80-83_total", "entity_any"),
            ("RRR", "2012-2023", "K80", "entity_any"),
        ]
        values = np.asarray([[0.8, 1.1], [0.9, 1.2], [1.0, 1.3], [1.1, 1.4]], dtype=float)
        points = {keys[0]: 0.95, keys[1]: 1.25}
        rows = compute.interval_rows(keys, values, points, seed=20261025, phase="formal")
        self.assertEqual(rows[0]["family_id"], compute.PRIMARY_FAMILY_ID)
        self.assertEqual(rows[0]["adjusted_level"], 0.975)
        self.assertEqual(rows[0]["evidence_role"], "posthoc_primary_adjusted_family")
        self.assertEqual(rows[1]["family_id"], compute.SECONDARY_FAMILY_ID)
        self.assertEqual(rows[1]["evidence_role"], "posthoc_secondary_adjusted_family")
        expected = np.quantile(values[:, 1], [0.025, 0.975], method="linear")
        self.assertAlmostEqual(rows[1]["nominal_lower"], float(expected[0]))
        self.assertAlmostEqual(rows[1]["nominal_upper"], float(expected[1]))

    def test_seed_draw_arrays_are_released_before_next_seed(self):
        patterns, population, weights = synthetic_patterns()
        design = compute.build_rate_design(patterns, population, weights)
        _, point_values = compute.point_estimate_rows(design)
        original = compute.run_bootstrap
        keys = compute.metric_keys()
        previous_draws = []
        call_count = 0

        def fake_bootstrap(_design, *, reps, seed, memory_limit_mib):
            nonlocal call_count
            gc.collect()
            if call_count:
                self.assertIsNone(previous_draws[0](), "previous seed draw array remained live")
            values = np.ones((reps, len(keys)), dtype=np.float64)
            previous_draws[:] = [weakref.ref(values)]
            call_count += 1
            return keys, values, {"seed": seed, "memory_limit_mib": memory_limit_mib}

        compute.run_bootstrap = fake_bootstrap
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp)
                for seed in compute.SEEDS:
                    intervals, meta = compute.run_seed_outputs(
                        design, point_values, seed=seed, phase="diagnostic", reps=2,
                        memory_limit_mib=32, output_dir=output_dir,
                    )
                    self.assertTrue(intervals)
                    self.assertEqual(meta["seed"], seed)
                    del intervals, meta
        finally:
            compute.run_bootstrap = original

    def test_inference_acceptance_is_not_execution_status(self):
        rows = []
        for seed in compute.SEEDS:
            for family_id, size in (
                (compute.PRIMARY_FAMILY_ID, 2),
                (compute.SECONDARY_FAMILY_ID, 9),
            ):
                for index in range(size):
                    rows.append({
                        "seed": seed, "family_id": family_id,
                        "metric_id": f"{family_id}|{seed}|{index}",
                        "adjusted_lower": 0.9, "adjusted_upper": 1.1,
                        "status": "PASS",
                    })
        stability = [{"metric_id": f"m{i}", "stability_pass": True} for i in range(11)]
        accepted = compute.assess_inferential_acceptance("formal", rows, stability)
        self.assertEqual(accepted["status"], "PASS")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(compute.assess_inferential_acceptance("diagnostic", rows, stability)["status"], "DIAGNOSTIC_ONLY")

        suppressed_rows = [dict(row) for row in rows]
        suppressed_rows[0]["adjusted_lower"] = ""
        suppressed = compute.assess_inferential_acceptance("formal", suppressed_rows, stability)
        self.assertEqual(suppressed["status"], "NOT_ACCEPTED")
        self.assertFalse(suppressed["accepted"])

        unstable_rows = [dict(row) for row in stability]
        unstable_rows[0]["stability_pass"] = False
        unstable = compute.assess_inferential_acceptance("formal", rows, unstable_rows)
        self.assertEqual(unstable["status"], "NOT_ACCEPTED")
        self.assertIn("m0", unstable["failed_seed_stability_metric_ids"])


if __name__ == "__main__":
    unittest.main()
