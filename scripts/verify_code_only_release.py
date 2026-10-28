"""Conservative file-class allowlist for this data-free public release."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
IGNORE = {'.git', '__pycache__', '.venv', 'outputs'}
CSV_METADATA = {
    'docs/CSV_CATALOG.csv',
    'docs/CSV_FIELDS.csv',
    'docs/data_release_review.csv',
    'docs/SOURCE_REGISTRY.csv',
}
EXCLUDED = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.zip', '.7z', '.gz',
            '.parquet', '.feather', '.dta', '.sav', '.rds', '.rdata', '.db',
            '.sqlite', '.pem', '.key'}

def inspect(root=ROOT):
    findings = []
    csvs = []
    for p in root.rglob('*'):
        if not p.is_file() or set(p.relative_to(root).parts) & IGNORE:
            continue
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() == '.csv':
            csvs.append(rel)
            if rel not in CSV_METADATA:
                findings.append({'path': rel, 'rule': 'non-metadata CSV'})
        if p.suffix.lower() in EXCLUDED or p.name.startswith('.env'):
            findings.append({'path': rel, 'rule': 'excluded data/figure/secret class'})
    return {'status': 'PASS' if not findings else 'FAIL',
            'metadata_csvs': sorted(csvs), 'findings': findings,
            'scope': 'File-class checks; not legal clearance or a complete privacy assessment'}

if __name__ == '__main__':
    result = inspect()
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
