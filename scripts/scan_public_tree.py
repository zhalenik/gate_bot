"""Scan the exact tracked publication set; report locations, never secret values."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    'gatebot-api-token': re.compile(r'\bgbt_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])'),
    'telegram-token': re.compile(r'\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential-url': re.compile(r'https?://[^/\s:@]+:[^/\s@]+@'),
}

# Unmodified Google Fonts WOFF2 subsets. Their OFL notices ship beside them.
PUBLIC_FONTS = {
    'static/fonts/inter-cyrillic.woff2': '71d5ee93cc1e9f1d520a3a8b66456de18c7879d8df09d57fcd2eaff75fef0075',
    'static/fonts/inter-latin.woff2': '3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62',
    'static/fonts/mulish-cyrillic.woff2': '270a9552cb39c2aa23054f70bf8c770bc1a9c2d6e0df2841bad89f6597bcd483',
    'static/fonts/mulish-latin.woff2': '8d1d33d6beea5a722b8f336d79c61c07405949457e37b5e65454c72dc10aba1a',
    'static/fonts/nunito-cyrillic.woff2': '63b14e3ef0966785438b8947380dfbcad178cbd3c6a5ed2172672ca0d8b839ac',
    'static/fonts/nunito-latin.woff2': 'ba344451eab25b217a165363b1982048a5e5830a0daf36577973955a04cac793',
    'static/fonts/rubik-cyrillic.woff2': 'bdb2b33ab916e9e7b42edb61f8aea5f5dd83ad773d6ee4e9b55665daf8954ce8',
    'static/fonts/rubik-latin.woff2': '691cd1d9b4c0cdf31a1dcf04259c86f92c85e69f6622abab7964d81e36890691',
}


def scan(root, paths, known=()):
    findings = []
    for relative in paths:
        path = Path(relative)
        if path.name.startswith('.env') and path.name != '.env.example' or any(part in {'data', 'private', 'logs', 'webhook_logs', 'originals', 'upload', '.venv'} for part in path.parts) or path.suffix in {'.key', '.pem', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.zip', '.log', '.jsonl'} or path.as_posix().startswith('storage/') and path.suffix == '.json':
            findings.append((relative, 0, 'private-runtime-file'))
            continue
        try:
            # Read the index blob, not an unstaged working-tree replacement.
            blob = subprocess.run(['git', 'show', ':' + relative], cwd=root, capture_output=True, check=True).stdout
            if relative in PUBLIC_FONTS:
                if hashlib.sha256(blob).hexdigest() != PUBLIC_FONTS[relative]:
                    findings.append((relative, 0, 'unexpected-font-content'))
                continue
            text = blob.decode('utf-8')
        except UnicodeDecodeError:
            findings.append((relative, 0, 'unreviewed-binary'))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                candidate = line
                # Two reviewed non-secret credentials in negative URL tests.
                if kind == 'credential-url' and relative.startswith('tests/'):
                    candidate = line.replace('name:pass@', 'fixture@').replace('user:password@', 'fixture@')
                if pattern.search(candidate):
                    findings.append((relative, number, kind))
            if any(value in line for value in known if len(value) >= 8):
                findings.append((relative, number, 'known-private-value'))
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--known-secrets', help='Private local JSON list, never commit this file')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True)
    paths = result.stdout.decode().split('\0')
    paths = [p for p in paths if p]
    known = json.loads(Path(args.known_secrets).read_text()) if args.known_secrets else []
    findings = scan(root, paths, known)
    for path, line, kind in findings:
        print(f'{path}:{line}: {kind}')
    print(f'Checked {len(paths)} tracked files; findings: {len(findings)}')
    raise SystemExit(1 if findings else 0)


if __name__ == '__main__':
    main()
