# US–Brazil biliary mortality analysis code

Public code companion to **Adding certificate mentions reduces relative but widens absolute US–Brazil biliary mortality differences**. The primary study period is 2012–2023; 2012–2024 analyses provide version-sensitive support.

Repository: https://github.com/FENG1567/us-brazil-biliary-mortality-code

## Release scope

This code-only release contains analysis and plotting source code, portable revision adapters, tests using synthetic fixtures, environment specifications and descriptive source/field metadata. Original code and documentation are provided under the MIT License. Third-party data retain their originating providers' conditions.

No research-result CSVs, mortality records, population input CSVs, figure PDFs, retained streams or fine joint-pattern inputs are distributed here. The four CSVs under `docs/` are metadata catalogs, not analysis datasets. Aggregate research data in the separate local candidate have not been included while their redistribution and disclosure conditions are being resolved. See [DATA_LICENSE.md](DATA_LICENSE.md) and [release scope](docs/RELEASE_SCOPE.md).

This is a new code-only repository, distinct from the earlier, data-containing legacy repository `FENG1567/us-brazil-biliary-death-certificate-representation`. It does not replace or validate that repository's historical data release.

## Installation and data-free checks

Use Python 3.12 in an activated environment; the commands below must not resolve to Python 2. R packages for plotting are listed in `environment.yml` and `figure_scripts/`.

```sh
python -m pip install -r requirements-test.txt
python scripts/verify_repository.py
python scripts/verify_code_only_release.py
python -m unittest discover -s analysis/tests -v
python -m unittest discover -s revision/code -p "test_*.py" -v
python -m unittest discover -s revision/tests -p "test_*.py" -v
```

Tests exercise synthetic/portable code paths. Population-data integration is skipped when the external fixture is absent. Passing these tests is not a rerun of the paper's analyses. The GitHub Actions workflow runs the same data-free checks; it does not download study data or run inference.

## Running the study code

The analysis and plotting scripts require separately obtained inputs. Downloading this code-only repository is not sufficient to reconstruct the paper's tables, figures or uncertainty intervals. `reproduce.py` is an aggregate reconstruction entry point supplied for use with the specified external summary tree, not a self-contained quick-start command for this release.

Required input structures, expected identities and acquisition routes are described in:

- [External inputs](docs/EXTERNAL_INPUTS.md)
- [Data acquisition contract](docs/DATA_ACQUISITION_CONTRACT.md)
- [Official source registry](docs/SOURCE_REGISTRY.csv)
- [Manuscript-to-output map](docs/MANUSCRIPT_MAP.md)
- [Reproduction limits](docs/REPRODUCIBILITY_LIMITS.md)
- [Revision adapters](revision/README.md)

Exact historical retained/pattern bundles and provenance bindings are additional prerequisites for statistical replay; current official downloads are not automatically interchangeable with those inputs. Some original helpers retain historical workstation-specific defaults. Use the explicit argument/configuration routes described in the documentation. The original scientific source bytes are preserved rather than silently changing hash-bound helper code.

## Code availability and citation

The manuscript-ready statement is in [docs/CODE_AVAILABILITY.md](docs/CODE_AVAILABILITY.md). Cite the repository using `CITATION.cff` and identify the code-only release used. No article or archival DOI has been assigned by this publication task; a GitHub URL is not an archival DOI.

The MIT License applies to original code/documentation only. See [LICENSE](LICENSE) and [DATA_LICENSE.md](DATA_LICENSE.md). Do not add external inputs or aggregate results without a separate source-specific sharing review.
