"""Pre-commit secret scanner (§1.1).

Tested on its own `scan()` against fake staged content rather than through git, so the
patterns are exercised directly. The strings below are invented and match no real key.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import secret_scan

FAKE_EVM_KEY = "0x" + "1234567890abcdef" * 4
FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"


@pytest.fixture
def staged(monkeypatch):
    """Pretend a set of paths is staged with the given content."""

    def _install(files: dict[str, str]):
        monkeypatch.setattr(secret_scan, "staged_files", lambda: list(files))
        monkeypatch.setattr(secret_scan, "staged_content", lambda path: files[path])

    return _install


class TestBlocks:
    def test_pem_private_key(self, staged) -> None:
        staged({"pmx/venues/kalshi.py": FAKE_PEM})
        assert secret_scan.scan(["pmx/venues/kalshi.py"])

    def test_evm_private_key(self, staged) -> None:
        staged({"pmx/config.py": f'PRIVATE_KEY = "{FAKE_EVM_KEY}"'})
        assert secret_scan.scan(["pmx/config.py"])

    def test_api_secret_assignment(self, staged) -> None:
        staged({"pmx/x.py": 'api_secret = "abcd1234efgh5678"'})
        assert secret_scan.scan(["pmx/x.py"])

    def test_passphrase_assignment(self, staged) -> None:
        staged({"pmx/x.py": "passphrase: 'correct-horse-battery'"})
        assert secret_scan.scan(["pmx/x.py"])

    def test_env_file_is_blocked_by_path_alone(self, staged) -> None:
        staged({".env": ""})
        findings = secret_scan.scan([".env"])
        assert findings and "never be committed" in findings[0]

    def test_pem_file_blocked_by_extension(self, staged) -> None:
        staged({"keys/kalshi.pem": ""})
        assert secret_scan.scan(["keys/kalshi.pem"])

    def test_finding_reports_the_line_number(self, staged) -> None:
        staged({"pmx/x.py": "line one\nline two\napi_secret = 'abcd1234efgh'"})
        assert secret_scan.scan(["pmx/x.py"])[0].startswith("pmx/x.py:3:")


class TestAllows:
    def test_clean_file_passes(self, staged) -> None:
        staged({"pmx/core/money.py": "from decimal import Decimal\nx = Decimal('1')"})
        assert secret_scan.scan(["pmx/core/money.py"]) == []

    def test_env_example_is_allowed(self, staged) -> None:
        staged({".env.example": "PMX_KALSHI_KEY_ID=\nPMX_POLYMARKET_API_SECRET="})
        assert secret_scan.scan([".env.example"]) == []

    def test_the_scanner_itself_is_allowed(self, staged) -> None:
        staged({"scripts/secret_scan.py": Path("scripts/secret_scan.py").read_text()})
        assert secret_scan.scan(["scripts/secret_scan.py"]) == []

    def test_quoted_mnemonic_is_blocked(self, staged) -> None:
        words = " ".join(["abandon"] * 11 + ["about"])
        staged({"pmx/x.py": f'SEED = "{words}"'})
        assert secret_scan.scan(["pmx/x.py"])

    def test_prose_is_not_a_mnemonic(self, staged) -> None:
        """The earlier pattern matched any run of twelve lowercase words, which fires on
        ordinary docstrings and trains the operator to pass --no-verify."""
        prose = (
            "this is a perfectly ordinary sentence of prose that happens to contain "
            "rather more than twelve consecutive lowercase words in a row"
        )
        staged({"pmx/x.py": f'"""{prose}"""'})
        assert secret_scan.scan(["pmx/x.py"]) == []

    def test_reference_to_a_variable_name_is_not_a_secret(self, staged) -> None:
        staged({"pmx/x.py": 'key = os.environ["PMX_POLYMARKET_API_SECRET"]'})
        assert secret_scan.scan(["pmx/x.py"]) == []


class TestRepoState:
    def test_gitignore_excludes_env_from_commit_one(self) -> None:
        ignored = Path(".gitignore").read_text()
        for pattern in (".env", "*.pem", "*.key", "KILL"):
            assert pattern in ignored, f"{pattern} must be gitignored"

    def test_env_is_actually_ignored_by_git(self, tmp_path) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], capture_output=True, check=False
        )
        assert result.returncode == 0, ".env is not ignored by git"

    def test_scanner_runs_as_a_script(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/secret_scan.py"], capture_output=True, check=False
        )
        # Exit 0 or 1 are both valid outcomes; a crash is not.
        assert result.returncode in (0, 1), result.stderr.decode()
