"""Tests for code quality using ruff and black."""

import subprocess
import sys
from pathlib import Path

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent
SRC_DIR = PROJECT_ROOT / "src"


class TestRuff:
    """Test suite for ruff linting."""

    def test_ruff_check(self):
        """Run ruff linter and ensure no issues are found."""
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(SRC_DIR)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert (
            result.returncode == 0
        ), f"Ruff found linting issues:\n{result.stdout}\n{result.stderr}"

    def test_ruff_format_check(self):
        """Run ruff format check to ensure code is properly formatted."""
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", str(SRC_DIR)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert (
            result.returncode == 0
        ), f"Ruff found formatting issues:\n{result.stdout}\n{result.stderr}"


class TestBlack:
    """Test suite for black formatting."""

    def test_black_check(self):
        """Run black formatter check to ensure code is properly formatted."""
        result = subprocess.run(
            [sys.executable, "-m", "black", "--check", str(SRC_DIR)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        assert (
            result.returncode == 0
        ), f"Black found formatting issues:\n{result.stdout}\n{result.stderr}"
