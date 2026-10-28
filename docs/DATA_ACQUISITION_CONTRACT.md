# Data-acquisition contract

This contract specifies external inputs needed to rerun preparation and inference. It does not include or authorize retrieval of those inputs. Obtain them only through the applicable legal and data-use routes. Do not place raw death records, credentials, host-specific login material, or unrelated project files in this package.

The supported preparation stage starts from release-selected retained streams. It does not perform raw-to-retained extraction, scan raw files, or discover candidate files with a broad historical glob. Raw-to-retained replay was not performed for this package.

## Input roots and required layout

Use the bundled `helpers/extension` and `helpers/pilot` roots as the bases for the corresponding bridge roots, and provide the frozen-input root separately.

`--extension-root` must contain:

- `release_manifest.json`, binding every selected stream and summary to its byte count and SHA-256;
- `scripts/extract_us.py` and `scripts/extract_br.py`, matching the parser identities embedded in the bundled bridge helper;
- `outputs/us/` with one current-parser retained CSV.GZ and adjacent summary JSON for each year 2012–2024;
- `outputs/br/` with `candidate_records_BR_<year>.csv.gz` and `summary_BR_<year>.json` for each year 2012–2024;
- the release-bound identity and audit artifacts at `outputs/identity/source_identity.csv`, `outputs/review/full_window_artifact_bindings.json`, and `outputs/review/final_independent_review.json`.

The bridge selects the US retained basename by its current-parser hash prefix and binds both countries' inputs through `release_manifest.json`. For Brazil, the candidate basename is `candidate_records_BR_<year>.csv.gz`. The release manifest must list the exact relative paths, byte counts, and checksums expected by the bridge.

`--pilot-root` must contain:

- `scripts/retained/run_retained_m1_m3.py` (bundled here);
- `outputs/retained_m2_filter_fix_20261021/M2_code_group_counts_by_stratum.csv`, with the exact corrected-reference SHA-256 required by the bundled bridge.

`--frozen-root` must contain:

- `data/derived/analysis_input_936.csv`;
- `data/derived/subcode_analysis_input_3744.csv`;
- `data/metadata/who_2000_2025_18_group_weights_correct.csv`.

The bridge verifies these fixed inputs by SHA-256 and checks the population/age/sex grids and weight totals before preparation. Do not substitute differently versioned files under the same names.

## Parser boundaries

The current US parser CLI exposes a `--years` option and otherwise retains its fixed defaults. The Brazil parser also exposes `--raw-dir`, `--frozen-dir`, `--ledger-path`, and `--output-dir`. The canonical retained runner uses a fixed local layout and has no CLI. Those entry points are included as source references for provenance and identity checking, not as a portable raw-data acquisition workflow. The preparation command imports only the bridge's read-only selector and identity helpers and uses the paths supplied through its own CLI overrides.

## Included versus externally supplied data

This package includes 14 aggregate publication CSVs, five formal summary tables, and seven previously generated summary inputs. It does not include any of the 26 retained stream files, their release/audit bundle, the frozen denominator/weight inputs, the corrected M2 reference, the prepared joint-pattern payload, bootstrap draw files, or record-level data. The package can rebuild the included publication tables without acquiring those inputs, but a fresh analysis cannot.
