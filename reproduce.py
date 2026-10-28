#!/usr/bin/env python3
"""Rebuild supplied aggregate results in a new directory, never over references."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def run(command, cwd):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                            text=True, encoding='utf-8', errors='replace', capture_output=True)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {command[1:]}\n{result.stdout}\n{result.stderr}')
    return {'command': [str(x) if not Path(str(x)).is_absolute() else Path(str(x)).name for x in command],
            'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT/'outputs/reproduced')
    parser.add_argument('--figures', action='store_true', help='also regenerate four main figures and Figure S1')
    parser.add_argument('--rscript', default='Rscript', help='Rscript executable (required only with --figures)')
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out == ROOT or ROOT.is_relative_to(out):
        parser.error('output-dir must not be the repository or an ancestor')
    if out.exists() and any(out.iterdir()):
        parser.error('Choose a new or empty output directory; reviewed inputs are never overwritten')
    out.mkdir(parents=True, exist_ok=True)
    for name in ('analysis', '01_inventory', '02_analysis', '03_figures_tables', 'figure_scripts'):
        shutil.copytree(ROOT/name, out/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pdf', '*.svg', '*.png', '*.tiff'))
    executions = [run([sys.executable, out/'analysis/build_publication_tables.py', '--overwrite'], out),
                  run([sys.executable, ROOT/'analysis/integrate_co_2023.py', '--source-root', out, '--output-root', out], out)]
    expected = ROOT/'03_figures_tables/source_data'
    rebuilt = out/'03_figures_tables/source_data'
    # Only files actually produced by the builder or Colombia integration are
    # claimed as regenerated. Additional reference CSVs were copied, not rerun.
    sys.path.insert(0, str(ROOT/'analysis'))
    import build_publication_tables as builder
    produced, _ = builder.build_all(ROOT)
    generated = list(produced) + ['Supplement_CO_2023_integrated_bounds.csv']
    comparisons = [{'file': f'03_figures_tables/source_data/{name}',
                    'reference_sha256': sha(expected/name), 'rebuilt_sha256': sha(rebuilt/name),
                    'identical': sha(expected/name) == sha(rebuilt/name)} for name in generated]
    if not all(x['identical'] for x in comparisons):
        raise RuntimeError('Rebuilt aggregate CSV differs from the reviewed reference')
    if args.figures:
        for script in ('create_figures.R', 'create_supplementary_figure_s1.R'):
            executions.append(run([args.rscript, out/'figure_scripts'/script, '--project-root', out], out))
        figures = sorted((out/'03_figures_tables/figures').glob('*.pdf'))
        if len(figures) != 5 or any(p.stat().st_size < 1000 for p in figures):
            raise RuntimeError('Expected four main figure PDFs plus supplementary Figure S1')
    # Algebraic checks of current revision tables are not new resampling.
    executions.append(run([sys.executable, ROOT/'scripts/verify_revision_summaries.py', '--root', ROOT], out))
    report = dict(status='PASS', scope='Aggregate table assembly and arithmetic checks; optional figure rendering',
                  raw_data_to_results_verified=False, formal_bootstrap_repeated=False,
                  regenerated_csv_count=len(generated), comparisons=comparisons,
                  figures_regenerated=args.figures, executions=executions)
    (out/'REPRODUCTION_REPORT.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'status':'PASS', 'regenerated_csv_count':len(generated),
                      'figures_regenerated':args.figures, 'raw_replay':False,
                      'report':str(out/'REPRODUCTION_REPORT.json')}, ensure_ascii=False))

if __name__ == '__main__':
    main()
