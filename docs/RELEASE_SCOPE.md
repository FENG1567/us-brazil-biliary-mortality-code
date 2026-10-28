# Code-only release scope

Version: v1.0.0-code-only

## Included

- The 25 Python/R source files present in the curated complete local candidate, unchanged at publication.
- Environment specifications, synthetic/portable tests, analysis contracts and external-input instructions.
- Descriptive metadata catalogs: `CSV_CATALOG.csv`, `CSV_FIELDS.csv`, `data_release_review.csv` and `SOURCE_REGISTRY.csv`, all under `docs/`.
- Original-code MIT licensing and repository citation information.

## Excluded

No research-result CSVs, population input CSVs, figure PDFs, mortality microdata, retained death rows, fine joint-pattern inputs, uncertainty draws, raw archives, credentials or unpublished Word manuscripts are included. Catalog entries and expected hashes describe external inputs; they do not make those files available in this repository.

The complete local candidate is not uploaded. Its aggregate-data redistribution/disclosure review remains unresolved, including small-count tables. No scientific count, row, interval or result was altered to create this code-only release.

## Checks and limits

The repository manifest verifies the released files and their SHA-256 values. `scripts/verify_code_only_release.py` enforces the four-file CSV metadata allowlist and excludes data/figure/archive file classes. These checks reduce accidental additions; they are not legal clearance or a full privacy assessment of future changes.

Synthetic tests and missing-input checks can run without study data. Full statistical replay requires exact external inputs, source identities and provenance bindings. Instructions distinguish aggregate reconstruction from inference and raw-file extraction. No new statistical analysis or raw-to-final replay was performed for this publication.

Original scientific source files remain byte-identical to their curated candidate versions. Historical helper defaults may contain local path literals; portable adapters and explicit input arguments are the supported configuration route. A manifest refresh corrects stale packaging hashes and records the added release documentation without changing scientific source code.

The earlier legacy GitHub repository is not overwritten. This publication makes no new representation about the legacy repository's data clearance or reproduction status.
