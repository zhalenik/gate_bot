"""Scan the exact tracked publication set; report locations, never secret values."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    'telegram-token': re.compile(r'\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential-url': re.compile(r'https?://[^/\s:@]+:[^/\s@]+@'),
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
