# External inputs for full statistical replay

**Code-only export:** all research/population CSVs and summary results discussed
below are external prerequisites, not files included in this export. The root
input manifest and catalogs specify expected identities, not data availability.

Nothing in this document authorizes bypassing a login, access agreement or CAPTCHA. Retrieve official files through the agency's permitted route. Exact historical bytes matter: a newly revised download is not silently interchangeable with the study's source release.

## A. Formal all-age primary inference

The supported preparation entry point begins with **retained death-level streams**, not arbitrary raw ZIPs. Required inputs are enumerated in `DATA_ACQUISITION_CONTRACT.md`:

- Extension root with exact parser files, 26 selected 2012–2024 retained streams, matching source summaries, release manifest and identity/audit bindings.
- Pilot root with the corrected M2 stratum reference and retained aggregation helper.
- Frozen root with exact 936-row overall and 3,744-row component inputs and the WHO18 weights.

These are external scientific input bundles. Their names/required hashes are checked by the supplied code. The public package does not contain fine death-count strata or death-level streams. Original parser source is supplied, but the historical audit bundle is not a new official download; exact replication may require the authors to provide the lawful derived bundle through a permitted access route. Do not manufacture missing audit records.

```sh
python analysis/prepare_full_window.py \
  --bridge-script helpers/bridge/run_usbr_monitoring.py \
  --extension-root /your/lawfully-acquired/extension \
  --pilot-root /your/lawfully-acquired/pilot \
  --frozen-root /your/lawfully-acquired/frozen \
  --contract-path contract.md \
  --output-dir outputs/prepared_payload

python analysis/compute_full_window.py \
  --input-dir outputs/prepared_payload \
  --output-dir outputs/diagnostic --phase diagnostic

python analysis/compute_full_window.py \
  --input-dir outputs/prepared_payload \
  --output-dir outputs/formal --phase formal
```

Diagnostic and formal runs are separate. Formal runs retain 50,000 draws for seeds 20261025 and 20261026; `USBR_NUM_THREADS` must be an integer 1–8 (default 8). Do not replace the interval family, seed or denominator with whichever gives a preferable result. A complete compute job and inferential acceptance are distinct states. Output manifests record the actual environment and checksums.

## B. Population-vintage sensitivity

The separate complete local candidate includes four official national Census source CSVs and population-only grids/weights; this code-only export does not. Formal temporal interval recalculation additionally needs the exact external `usbr_joint_patterns.csv`, baseline population, baseline points and intervals, and the frozen input grids required by the historical bridge helper. The adapter in `revision/` documents the concrete path map. It verifies required input checksums and refuses output into nonempty directories.

Three variants are baseline split-2020/2024, revised intercensal + Vintage 2024, and revised intercensal + Vintage 2025. Original temporal analysis uses 20,000 draws per seed (20261023, 20261024); six contrasts form the separate time family. National population-only files cannot replace the joint-pattern source.

## C. Residence and demographic-missingness sensitivity

The original source scanner covers 24 country–years (2012–2023) using the original US/Brazil parser definitions, prior country-year identity/ledger, TabNet reference, frozen age/sex inputs and the saved IBGE municipality response. It is not part of the default quick reproduction, and does not run automatically.

A replay from an externally acquired aggregate scan cache or detailed aggregate CSVs avoids reopening raw death files, but still requires the specified source/denominator identities. Fine municipality code and age/sex source tables are not included in this public candidate. The portable adapter provides bounds/point replay; summary-only checks are separately labelled and do not claim to rerun raw scanning.

## D. Historical model and composition code

`optional_sources/legacy_model/joint_monitoring.R` and `optional_sources/composition/summarize_composition.py` are original scientific source snapshots. Their default paths were specific to the original project. They are not the quick-start entrypoint; their original scientific input/ledger dependencies must be configured before use. Main model-free formal results must not be replaced with the older model outputs.

## E. What does not belong to this paper's repository

South Africa/DataFirst, NHIS/ICPSR, SIH/SIA service-use pilots, ONS/UK and other rejected extensions are not supporting inputs of the current manuscript. Do not redistribute their downloads or terms-acceptance/credential artifacts here. Their earlier investigation does not create a requirement to upload them for this paper.
