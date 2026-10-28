# Portable revision-analysis adapters

**Code-only export:** input datasets are external. The commands below describe
execution after assembling a lawful input tree, not commands that can compute
results from this data-free download alone.

These adapters replay selected calculations from the unchanged 2026-10-26
revision sources. Set `--input-root` to this repository's root directory;
the source tree, helper scripts, Census inputs, and denominator-only files are
resolved there. The adapters relocate path globals before importing modules;
the original statistical functions are used for residence bounds and
population sensitivity points.

The adapter does not download inputs or scan raw records. It writes only to a
new/empty output directory separate from `--input-root`. Every input must be
listed in `input_manifest.json` with its path relative to the input root and a
SHA-256. Published source files also have fixed SHA-256 checks in the adapter.
The residence aggregate cache may be redacted before distribution, so its
manifest checksum is the pin for the exact external copy used in a replay.

## Input tree

Only the inputs needed for the selected mode are mandatory.

```text
input-root/
  input_manifest.json  # may instead be supplied with --manifest
  revision/code/audit_residence_missing.py
  revision/code/run_population_sensitivity.py
  revision/external/
    residence_missing_reconstructed_aggregate_cache.json  # external
    usbr_joint_patterns.csv  # external; do not package here
    usbr_primary_bootstrap_intervals.csv  # required only with --formal
  helpers/extension/scripts/{extract_br.py,extract_us.py}
  helpers/bridge/run_usbr_monitoring.py
  data/metadata/population.csv  # 936 population cells only; no death counts
  data/metadata/who_2000_2025_18_group_weights_correct.csv
  data/population_sources/{nc-est2020int-agesex-res.csv,...}
```

The input manifest schema is:

```json
{
  "schema": "revision_adapter_inputs_v1",
  "files": {
    "residence_audit_source": {
      "path": "revision/code/audit_residence_missing.py",
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  }
}
```

Add one entry for each required role for your selected mode. The complete
role-to-path mapping is in `INPUT_PATHS` in `revision_adapter.py`. Use
checksums from the acquired source files, then retain the manifest with the
run outputs. The bundled runner/helper/parser/Census/denominator inputs are
checksum-checked against published hashes. The residence cache and joint
patterns remain external, with their manifest checksums pinning the supplied
copies.

## Commands

Residence deterministic bounds from the external aggregate-only cache:

```text
python revision/code/revision_adapter.py --analysis residence --input-root . --output-dir ../residence-replay
```

Population denominator variants without mortality-pattern inputs (a coarse
source check only):

```text
python revision/code/revision_adapter.py --analysis population --denominators-only --input-root . --output-dir ../denominator-check
```

Population point estimates are the default population action. The externally
acquired pattern file and the hash-matched 2025 helper are mandatory:

```text
python revision/code/revision_adapter.py --analysis population --input-root . --output-dir ../population-points
```

The point-only path never resamples. Full bootstrap inference runs only when
explicitly requested with `--formal`; it additionally requires the archived
baseline interval input. The pattern file must be lawfully acquired and its
checksum must match the original analysis contract.

## Scope and limitations

The residence mode replays the deterministic bounds and residence-scope
sensitivity from the supplied aggregate cache. It does not reopen the raw
archives, rerun source identity/TabNet/ledger checks, or verify the original
cache's source metadata beyond the supplied checksum. The cache is not included
in this adapter folder; the source-metadata-redacted copy and its checksum must
be provided externally.

Population point/formal modes require the external joint-pattern CSV and use
the checksum-verified 2025 bridge helper from `helpers/bridge/`. The pattern
file is not included here. The `--denominators-only` mode
can verify Census vintage aggregation and denominator cells without those
death-pattern inputs, but it is not a point-estimate or inference replay.
No mode changes the frozen analytic cohort or overwrites existing outputs.

The repository-root `input_manifest.json` records hashes for 11 source/population
roles from the complete companion candidate. In this code-only export, dataset
roles are **not bundled**. Even the denominator-only command needs the external
population CSVs and weights at the listed paths. For residence/point/formal
replay, also add the required lawfully acquired event inputs and pin them in a
separate manifest (`--manifest`).
`revision/external/` is ignored by Git. Do not commit the cache or fine pattern
files. Outputs must be outside `--input-root`, or use a separate temporary input
tree. Five synthetic scanner tests and four public-data adapter tests are
portable and included in CI; the private-cache replay test evidence is recorded
separately, not distributed with its fine-grained fixture.
