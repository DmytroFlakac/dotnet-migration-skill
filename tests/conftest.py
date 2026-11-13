import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SAMPLE = ROOT / "samples" / "synthetic-monorepo"


def dotnet_path() -> str | None:
    """dotnet with SDK 10 (+ runtime 8 for net8.0 tests): $DMIG_DOTNET, else PATH."""
    return os.environ.get("DMIG_DOTNET") or shutil.which("dotnet")


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A fresh git repository containing the synthetic monorepo, committed once."""
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("bin", "obj", "TestResults"))
    git = ["git", "-c", "core.autocrlf=false", "-c", "user.email=test@example.invalid", "-c", "user.name=test"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "synthetic monorepo"], cwd=repo, check=True)
    return repo


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="rewrite tests/golden/*.json from the current outputs")
