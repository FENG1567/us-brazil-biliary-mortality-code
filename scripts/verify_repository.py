"""Verify an immutable release manifest and conservative public-file checks."""
from pathlib import Path
import argparse
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {'outputs', '__pycache__', '.git', '.venv'}
KEY_PATTERN = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
SECRET_PATTERN = re.compile(r'''(?im)^\s*(?:password|passwd|api_key|access_token|ssh_password)\s*[:=]\s*["'](?!["'])([^"'\r\n]+)["']''')

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def inspect(root, verify_manifest=True):
    findings = []
    count = 0
    for path in root.rglob('*'):
        if not path.is_file() or set(path.relative_to(root).parts) & IGNORED_PARTS:
            continue
        count += 1
        if path.stat().st_size >= 100_000_000:
            findings.append({'file':path.relative_to(root).as_posix(), 'rule':'GitHub individual file size'})
        if path.suffix.lower() in {'.pem', '.key', '.parquet', '.zip', '.7z'} or path.name.startswith('.env'):
            findings.append({'file':path.relative_to(root).as_posix(), 'rule':'excluded file class'})
        if path.suffix.lower() in {'.py', '.r', '.md', '.json', '.txt', '.csv', '.yml', '.yaml', '.cff'}:
            text = path.read_text(encoding='utf-8-sig', errors='replace')
            for rule, pattern in [('private key',KEY_PATTERN), ('literal secret assignment',SECRET_PATTERN)]:
                if pattern.search(text):
                    findings.append({'file':path.relative_to(root).as_posix(), 'rule':rule})
    checked = 0
    if verify_manifest:
        manifest = json.loads((root/'REPOSITORY_MANIFEST.json').read_text(encoding='utf-8'))
        expected = {r['path'] for r in manifest['files']}
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and not set(p.relative_to(root).parts) & IGNORED_PARTS
                  and p.name != 'REPOSITORY_MANIFEST.json'}
        if actual != expected:
            findings.append({'rule':'manifest file set mismatch', 'added':sorted(actual-expected), 'missing':sorted(expected-actual)})
        for row in manifest['files']:
            p = root/row['path']
            if not p.is_file() or sha(p) != row['sha256'] or p.stat().st_size != row['bytes']:
                findings.append({'file':row['path'], 'rule':'manifest SHA-256/size mismatch'})
            checked += 1
    return dict(status='PASS' if not findings else 'FAIL', scanned_files=count,
                manifest_entries_checked=checked, findings=findings,
                scope='File integrity and specified secret/file-class patterns; not a data-redistribution legal clearance')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT)
    p.add_argument('--without-manifest', action='store_true')
    a = p.parse_args()
    result = inspect(a.root.resolve(), not a.without_manifest)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['status']=='PASS' else 1)
