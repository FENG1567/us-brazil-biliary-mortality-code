# Code-only export: reproduction limits

This export includes scientific code, synthetic tests and metadata. It contains
no mortality summaries, population CSV inputs, plot-source data or reference
figure PDFs. A data catalog/hash is not the dataset it describes.

- The 17 core and five scanner tests use synthetic fixtures and run without
  research data. Three adapter/preflight tests run; the population-data
  integration test is explicitly skipped when its fixture is absent.
- `reproduce.py` is supplied as the companion aggregate-data workflow. It needs
  the exact summary input tree described in `MANUSCRIPT_MAP.md`; downloading
  this code-only export is not enough to execute it.
- Main formal inference requires the identity-bound retained/pattern bundle,
  frozen count/population grids and input provenance described in
  `EXTERNAL_INPUTS.md` and `DATA_ACQUISITION_CONTRACT.md`. Raw-to-final and formal
  interval replay have not been verified in this code-only package.
- Residence-bound replay needs an external aggregate scan cache and population
  inputs. Population point/interval replay needs the external joint-pattern
  input. Even denominator-only replay requires the external national population
  CSVs and weights; none are supplied in this export.
- The companion complete local candidate was checked for deterministic table
  reconstruction, figure rendering and population aggregation. Those checks do
  not turn this separate data-free download into a self-contained reproduction.

Obtain inputs only through lawful source/access routes. Third-party sharing
terms and small-cell disclosure decisions remain separate from MIT code
licensing. No missing data were filled with invented scientific observations.
