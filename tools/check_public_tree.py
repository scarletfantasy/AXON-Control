"""Check Git-index contents for common accidental private files and credentials.

Print file names and rule labels only; never echo matching sensitive text.
This is a guardrail, not a replacement for reviewing a staged diff.
"""
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_NAMES = (
    '*.exe', '*.dll', '*.pdb', '*.zip', '*.7z', '*.pyc', '*.log', '*.syx', '*.pcap*',
    '*.pem', '*.key', '.env', '.env.*', '*-packets.json',
    'last-session.json', 'recovery-session.json', 'desktop-settings.json',
    'preset-library.json', 'legacy-data-locations.json', 'hardware-validation.json',
    'ui-validation.json', 'performance-*.json', 'spectrum-*.json', 'startup-error.txt',
)
PRIVATE_DIRS = {'backups', 'drafts', 'validation-history', 'build', 'dist', '.venv', '__pycache__'}
RULES = {
    'absolute personal Windows path': re.compile(r'\b[A-Za-z]:[\\/]+Users[\\/]+[^\s<>\\/]+', re.I),
    'absolute personal Unix path': re.compile(r'/(?:home|Users)/[^\s<>/]+/'),
    'GitHub access token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    'AWS access key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private key block': re.compile(r'-{5}BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-{5}'),
    'literal bearer credential': re.compile(r'\bBearer\s+[A-Za-z0-9_./+=-]{30,}', re.I),
}
BINARY_ASSETS = {'axon-icon.png', 'axon-icon.ico'}


def git(*arguments):
    return subprocess.run(['git', '-C', str(ROOT), *arguments], check=True,
                          capture_output=True).stdout


def main():
    try:
        paths = git('ls-files', '-z').decode('utf-8').split('\0')
    except (OSError, subprocess.CalledProcessError):
        print('Run this check inside a Git checkout after staging the intended files.', file=sys.stderr)
        return 2
    paths = [path for path in paths if path]
    if not paths:
        print('No staged/tracked files found; stage the intended source files first.', file=sys.stderr)
        return 2
    findings = []
    for path in paths:
        item = PurePosixPath(path)
        if any(fnmatch(item.name.lower(), name) for name in PRIVATE_NAMES) or PRIVATE_DIRS.intersection(item.parts):
            findings.append((path, 'private data or build artifact filename'))
            continue
        # Inspect the staged bytes, not a possibly different working-tree version.
        data = git('show', ':' + path)
        if path in BINARY_ASSETS:
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            findings.append((path, 'unreviewed binary file'))
            continue
        for label, pattern in RULES.items():
            if pattern.search(text):
                findings.append((path, label))
    if findings:
        for path, label in findings:
            print(f'{path}: {label}', file=sys.stderr)
        print(f'Public-tree check failed: {len(findings)} finding(s).', file=sys.stderr)
        return 1
    print(f'Public-tree check passed: {len(paths)} tracked files; no matching private-data patterns.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
