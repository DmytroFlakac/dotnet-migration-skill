"""Each integration interface has a mock that works on any machine."""
import json
import platform
import re
import subprocess
from pathlib import Path

import pytest

from dmig import adapters
from dmig.adapters import (EnvSecretProvider, ICiExecutor, IDelivery, IPackageCatalog, ISecretProvider, ISourceControl,
                           ITelemetry, IWorkItemAdapter, JsonlTelemetry, LocalCatalog, LocalFileDelivery,
                           LocalGitSourceControl, LocalProcessExecutor, MockEnterprisePackageCatalog,
                           MockWorkItemAdapter)

from conftest import CATALOG


def test_package_catalog_mock_is_the_local_mirror():
    c = LocalCatalog(CATALOG)
    assert isinstance(c, IPackageCatalog) and c.newest_allowed("Microsoft.Extensions.Hosting", 10) == "10.0.0"
    m = MockEnterprisePackageCatalog()                          # fixtures/package-catalog.json by default
    assert m.newest_allowed("Microsoft.Extensions.Hosting", 10) == "10.0.0"
    assert m.is_available("Microsoft.Extensions.Hosting", "9.9.9") is False   # never a version not available


def test_delivery_mock_writes_only_the_patch_and_the_report(tmp_path: Path):
    d = LocalFileDelivery(tmp_path / "delivery")
    assert isinstance(d, IDelivery)
    patch = tmp_path / "g.patch"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    out = d.deliver(patch, {"status": "SUCCESS", "ready_for_human_review": True, "rollback_verified": True},
                    "TEST-123", "Migrate App A")
    assert out["branch"] == "feature/TEST-123-migrate-app-a"
    assert sorted(p.name for p in (tmp_path / "delivery").iterdir()) == ["migration-report.md", "migration.patch"]
    assert "Rollback verified:** True" in (tmp_path / "delivery" / "migration-report.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        d.branch_name("NOPE", "x")               # does not match feature/TEST-<n>-...


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="missing: git")
def test_source_control_mock_is_local_git(sample_repo: Path, tmp_path: Path):
    sc = LocalGitSourceControl()
    assert isinstance(sc, ISourceControl) and len(sc.head(sample_repo)) == 40
    wt = tmp_path / "wt"
    sc.create_worktree(sample_repo, wt)
    (wt / "new.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-N", "new.txt"], cwd=wt, check=True)
    assert "new.txt" in sc.diff(wt)
    sc.remove_worktree(sample_repo, wt)
    assert not wt.exists()


def test_work_items_mock_returns_a_synthetic_ticket():
    w = MockWorkItemAdapter()
    assert isinstance(w, IWorkItemAdapter) and w.ticket_for("Migrate group app-a") == "TEST-123"


def test_secrets_mock_uses_dummy_values_and_never_reveals_them(monkeypatch):
    s = EnvSecretProvider()
    assert isinstance(s, ISecretProvider) and s.get("feed_token") == "dummy-feed_token"
    monkeypatch.setenv("DMIG_SECRET_FEED_TOKEN", "real-value-123")
    assert s.get("feed_token") == "real-value-123"
    assert "real-value" not in s.describe("feed_token")


def test_telemetry_mock_writes_redacted_jsonl(tmp_path: Path):
    t = JsonlTelemetry(tmp_path / "t.jsonl")
    assert isinstance(t, ITelemetry)
    t.event("run_finished", status="SUCCESS", conn="Server=db;Password=Sup3rS3cret!")
    line = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["event"] == "run_finished" and "Sup3rS3cret" not in json.dumps(line)


def test_ci_executor_mock_is_a_local_process_and_knows_its_platform(tmp_path: Path):
    r = LocalProcessExecutor()
    assert isinstance(r, ICiExecutor) and r.can_run("ANY")
    assert r.can_run("WINDOWS") == (platform.system() == "Windows")
    assert hasattr(r.command_runner(None, tmp_path), "run")


def test_defaults_are_all_mocks(tmp_path: Path):
    d = adapters.defaults(tmp_path, CATALOG)
    assert set(d) == {"package_catalog", "source_control", "work_items", "secrets", "ci_executor", "telemetry",
                      "delivery"}


def test_earlier_names_still_work():
    assert adapters.LocalReviewPackage is LocalFileDelivery and adapters.FixedWorkItems is MockWorkItemAdapter
    assert adapters.EnvSecrets is EnvSecretProvider and adapters.LocalRunner is LocalProcessExecutor


def test_the_core_never_imports_a_concrete_adapter():
    """Only the composition root (cli.py) picks concrete adapters."""
    src = Path(__file__).resolve().parents[1] / "src" / "dmig"
    users = [p.name for p in src.glob("*.py") if p.name != "cli.py"
             and re.search(r"from \.adapters|import adapters", p.read_text(encoding="utf-8"))]
    assert users == []
