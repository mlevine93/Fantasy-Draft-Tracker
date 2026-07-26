#!/usr/bin/env python3
"""Pre-commit secret scanner (§1.1).

Blocks the commit if a staged diff looks like it contains a credential.
Deliberately noisy: a false positive costs a `--no-verify` argument and a
conversation; a false negative costs an RSA key on GitHub.

Install with: python scripts/secret_scan.py --install
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# (name, pattern). Patterns match the *content* of staged changes.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PEM private key block", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("EVM private key (0x + 64 hex)", re.compile(r"\b0x[0-9a-fA-F]{64}\b")),
    ("bare 64-hex secret", re.compile(r"(?i)(private_key|privkey|secret)\D{0,20}\b[0-9a-f]{64}\b")),
    ("Kalshi key id assignment", re.compile(r"(?i)kalshi[_-]?(api[_-]?)?key[_-]?id\s*[:=]\s*['\"][^'\"]{8,}")),
    ("POLY api credential assignment", re.compile(r"(?i)poly[_-]?(api[_-]?key|passphrase|secret)\s*[:=]\s*['\"][^'\"]{8,}")),
    ("generic api secret assignment", re.compile(r"(?i)\b(api[_-]?secret|passphrase|mnemonic|seed[_-]?phrase)\b\s*[:=]\s*['\"][^'\"]{8,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    # Quoted and single-spaced: a real mnemonic is a string literal, and matching bare
    # runs of lowercase words flags ordinary English prose in every docstring.
    ("BIP39-looking mnemonic", re.compile(r"['\"](?:[a-z]{3,8} ){11,23}[a-z]{3,8}['\"]")),
]

# Files whose whole point is to describe secret *shapes*, not hold them.
ALLOWLIST_SUFFIXES = ("scripts/secret_scan.py", ".env.example", "tests/test_secret_scan.py")

BLOCKED_PATHS = re.compile(r"(^|/)(\.env(\..+)?$|.*\.(pem|key|p12|pfx)$)")
ENV_EXAMPLE = re.compile(r"(^|/)\.env\.example$")

HOOK = """#!/bin/sh
exec python3 scripts/secret_scan.py
"""


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def staged_content(path: str) -> str:
    out = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True, text=True, check=False
    )
    return out.stdout if out.returncode == 0 else ""


def scan(paths: list[str]) -> list[str]:
    """Return a list of human-readable findings. Empty list means clean."""
    findings: list[str] = []
    for path in paths:
        if BLOCKED_PATHS.search(path) and not ENV_EXAMPLE.search(path):
            findings.append(f"{path}: secret-bearing file must never be committed")
            continue
        if path.endswith(ALLOWLIST_SUFFIXES):
            continue
        content = staged_content(path)
        if not content:
            continue
        for name, pattern in PATTERNS:
            match = pattern.search(content)
            if match:
                line_no = content[: match.start()].count("\n") + 1
                findings.append(f"{path}:{line_no}: {name}")
    return findings


def install() -> int:
    hook_path = Path(".git/hooks/pre-commit")
    if not hook_path.parent.is_dir():
        print("not a git repository (no .git/hooks)", file=sys.stderr)
        return 1
    hook_path.write_text(HOOK)
    hook_path.chmod(0o755)
    print(f"installed {hook_path}")
    return 0


def main(argv: list[str]) -> int:
    if "--install" in argv:
        return install()
    findings = scan(staged_files())
    if findings:
        print("COMMIT BLOCKED — possible secrets in staged changes:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nIf this is a false positive, move the value to .env and reference it by "
            "name, or commit with --no-verify only after you are certain.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
