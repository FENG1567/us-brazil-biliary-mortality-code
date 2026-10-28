> Export scope: this document describes the companion complete local candidate. The current code-only export does not include the research data it names.

# USBR MCOD reproducibility package

This package preserves the aggregate publication tables, checksum-bound formal summary inputs, figure scripts and panel source data, the 2023 Colombia arithmetic integration, the table builder, and helper code for the retained-stream preparation stage.

The package is intentionally not a raw-data archive. It excludes raw death records, retained compressed streams, the prepared joint-pattern payload and joint-pattern microdata, the prepared-payload manifest itself, bootstrap draw files (including archived remote draws), fine-stratum source counts, remote execution logs, and hostnames or private login-controller details. These exclusions concern death-level records and fine-stratum/pattern inputs; they do not exclude the aggregate 240-row M1 publication table described below. Generated manifests, source maps, and documentation use relative paths and do not expose private source paths. Machine-specific source locations were neutralized in this snapshot; the documented preparation command requires explicit path overrides. The new figure and Colombia-integration scripts default to package-relative inputs, with explicit CLI overrides documented below. The expected prepared-payload-manifest SHA-256 is retained as an identity check only. Raw-to-retained replay was not performed when this package was assembled; no new statistical analysis or bootstrap was run. The existing Colombia arithmetic integration was replayed from included summary tables solely to validate its packaged output.

## Rebuild the included aggregate tables

Use Python 3.12 (for example, the Windows `py -3.12` launcher) and install the pinned dependency:

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m unittest discover -s analysis/tests
py -3.12 analysis/build_publication_tables.py --overwrite
```

The table builder defaults to this package directory, so it can be launched from any working directory. It validates the formal summary tables against `02_analysis/formal/compute_manifest.json`, checks the seven reused summaries against the sanitized relative-path source map, and writes the 14 CSVs under `03_figures_tables/source_data` plus `02_analysis/RESULTS_FOR_WRITING.json`. `--overwrite` applies only to those named generated outputs.

The included aggregate CSVs are already the reviewed source-data outputs. Before packaging, rebuilding the 14 table-builder CSVs produced byte-identical outputs to the previously reviewed files. Rebuilding them is deterministic table assembly over included summary inputs; it does not resample observations or read record-level data. The omitted prepared payload is not needed for this table-assembly step: its expected manifest hash and size are checked against the explicit exclusion identity in `analysis/payload_identity.json`.

The complete 240-row M1 joint-category table is also supplied under the manuscript-friendly name `03_figures_tables/source_data/TableS4_full_joint_categories.csv`. It is a byte-identical copy of `Supplement_M1_joint_all_240.csv`; the source table remains unchanged. This is an aggregate country-by-12-year publication table, not joint-pattern microdata, and cannot replay record-level classifications. It contains 32 US and 26 Brazil aggregate cells with counts of 1–4 and no individual identifiers. Authors should confirm applicable agency release terms before public redistribution.

The legacy 2012–2023 model-sensitivity result is included as the requested Supplementary Table S7 evidence at `03_figures_tables/source_data/Supplement_legacy_2012_2023_model_sensitivity.csv` and as its source snapshot under `02_analysis/reused/model_sensitivity`. It is a previously generated HC1 model result (16 rows), byte-identical across the two paths; it does not alter primary results and is not a new analysis or a Poisson event-process bootstrap.

## Rebuild the figures

The final R scripts, figure contract, panel source-data CSVs, source-data hash map, captions, and QA notes are included. Install R packages `ggplot2`, `patchwork`, `svglite`, `ragg`, and `openssl`; the scripts also use the base `grDevices::cairo_pdf` device. Run from the package root, or invoke each script by its full path:

```powershell
Rscript figure_scripts/create_figures.R
Rscript figure_scripts/create_supplementary_figure_s1.R
```

By default each script locates the package root relative to its own file and writes outputs under `03_figures_tables/figures`. To use another prepared source tree, supply `--project-root <path-to-source-root>` explicitly. Figure inputs are the packaged files under `02_analysis` and `03_figures_tables/source_data`; panel-level hashes are recorded in `03_figures_tables/figures/SOURCE_DATA_HASH_MAP.csv`. See `figure_scripts/FIGURE_CONTRACT.md` for panel definitions and interpretation limits.

On Windows with R 4.3.1, direct invocation from a path containing non-ASCII characters may fail during locale initialization. If that occurs, extract or copy the entire package to an ASCII-only path (for example, `D:\MCOD_reproduction`) and run the scripts there. Do not change the system locale, encoding, or proxy settings for this workflow.

## Reproduce the 2023 Colombia integration

`analysis/integrate_co_2023.py` uses the included aggregate country-rate summary at `02_analysis/reused/co_2023/m5_microdata_standardized_rates.csv` and the annual US/Brazil summary table. The M5 file contains released country/year/sex summary rows, not individual records. `01_inventory/SOURCE_MAP.json` binds that input by relative path, size, and SHA-256. The script performs only arithmetic on existing same-year rates; the available-mention bounds are source-identification bounds, not confidence intervals. Run from the package root:

```powershell
py -3.12 analysis/integrate_co_2023.py
```

The defaults read from and write to this package. Optional `--source-root <path>` and `--output-root <path>` arguments explicitly override those roots when using a separately acquired source tree. The outputs are `03_figures_tables/source_data/Supplement_CO_2023_integrated_bounds.csv` and `06_review/CO_2023_INTEGRATION.json`.

## Optional full analysis stages

The preparation and compute scripts are included for reproducibility when their external inputs are lawfully available. The data-acquisition contract in `docs/DATA_ACQUISITION_CONTRACT.md` describes the exact required folder layout. In brief, preparation consumes release-selected retained streams, parser/release audit artifacts, frozen population and WHO18-weight tables, and the corrected M2 reference. It does not scan raw-death files. None of those retained/frozen input bundles is included here.

After populating the described input roots, the preparation entry point is:

```powershell
py -3.12 analysis/prepare_full_window.py `
  --bridge-script helpers/bridge/run_usbr_monitoring.py `
  --extension-root helpers/extension `
  --pilot-root helpers/pilot `
  --frozen-root <path-to-acquired-frozen-input-root> `
  --contract-path contract.md `
  --output-dir analysis/prepared_payload
```

The compute script accepts a prepared payload and an explicit phase. A formal run uses 50,000 draws for each of the two frozen seeds; that expensive inferential step was not run while assembling this package. The resulting formal summary files must be staged under `02_analysis/formal` with their matching compute manifest before rebuilding publication tables. The included parser entry points have differing CLI/path behavior; this package does not claim portable end-to-end raw-to-retained replay.

## Package integrity

`package_manifest.json` lists the relative path, byte count, and SHA-256 of each packaged file except the manifest itself. `source_identity.json` records source-versus-packaged hashes for the analysis and helper scripts without recording their original filesystem paths. A separate `.sha256` file beside the ZIP authenticates the archive.

The package can be archived with the included standard-library helper; choose an archive path outside the package directory:

```powershell
py -3.12 analysis/build_reproducibility_package.py `
  --package-dir . `
  --archive-path ../USBR_MCOD_reproducibility_20261025.zip
```
