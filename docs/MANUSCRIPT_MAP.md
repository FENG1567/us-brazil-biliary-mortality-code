> Export scope: this document describes the companion complete local candidate. The current code-only export does not include the research data it names.

# Manuscript result → data → code

Current manuscript identity and SHA-256 values: `manuscript_identity.json`. Public repository outputs are CSV/PDF, not regenerated Word typesetting. Word, cover letters and simulated peer-review responses are unnecessary for code reproduction and are excluded.

| Evidence | Reviewed aggregate source | Executable source |
|---|---|---|
| Overall relative/absolute effects; main Table 1; Figure 1a–c | `02_analysis/formal/point_estimates.csv`, reported-seed intervals; `Table1_overall_two_scales.csv` | `analysis/compute_full_window.py` with external patterns; `build_publication_tables.py`; `figure_scripts/create_figures.R` |
| Eight fixed code groups; Table 2; Figure 2a–b; direct K83.0/K83.1 contrast | `Table2_fixed_eight_codes_two_scales.csv`, `Supplement_K83_H_and_expH.csv` | Same primary compute/table/plot stages; groups must not be summed as mutually exclusive |
| Joint certificate contexts and Figure 3a | `Supplement_M1_joint_all_240.csv`, focus/top-plus-remainder tables | `helpers/pilot/scripts/retained/run_retained_m1_m3.py` with external retained rows; table builder and main plotting script |
| Malignancy restrictions and Figure 3b–c | `Supplement_primary_record_restrictions.csv` | Primary compute and table builder, full denominators retained after record restrictions |
| Early/late age-0–74 monitoring; Figure 4a–b | `02_analysis/reused/time_0_74/`, `Supplement_time_0_74_six_family.csv` | `helpers/bridge/run_usbr_monitoring.py` with external patterns; main plotting script |
| Annual all-age series; Figure S1 | `Supplement_annual_overall_all_age.csv` | Table builder; `figure_scripts/create_supplementary_figure_s1.R` |
| 2024 source-version support; US record-axis alternative | `Supplement_2012_2024_support.csv`, `Supplement_primary_record_axis.csv` | Primary preparation/compute/table stages |
| DANE 2023 bounded external comparison | `02_analysis/reused/co_2023/m5_microdata_standardized_rates.csv`, `Supplement_CO_2023_integrated_bounds.csv` | `analysis/integrate_co_2023.py` (aggregate arithmetic only) |
| Historical model specification sensitivity | `Supplement_legacy_2012_2023_model_sensitivity.csv` | `optional_sources/legacy_model/joint_monitoring.R` (external original input prerequisites) |
| New cohort flow, disease-specific demographic exclusions, missing-allocation bounds and residence restriction | `revision/results/Revision_*.csv` (non-population files) | `revision/code/audit_residence_missing.py`, portable adapter; summary checks in `scripts/verify_revision_summaries.py` |
| New official population-vintage sensitivity | `revision/results/Revision_population_*.csv`, Census source CSVs | `revision/code/run_population_sensitivity.py`, portable adapter with external paired patterns |

All named table CSVs above are under `03_figures_tables/source_data/` unless a different path is written. Numeric panel CSVs are under `03_figures_tables/figures/source_data/`. A machine-readable CSV schema catalog is supplied separately. Supporting table labels in Word may have subpanels; the descriptive filenames identify the evidence without renumbering the paper.
