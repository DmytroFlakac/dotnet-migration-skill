"""The integration surface (§8, §19): command names, default folders, the camelCase result,
rollback of a run's workspace, group validation."""
import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from dmig import cli
from dmig.contract import to_contract

from conftest import CATALOG, FULL, ROOT, dotnet_path, requires

GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _parse(monkeypatch, argv):
    seen = {}
    for name in ("cmd_doctor", "cmd_analyze", "cmd_implement", "cmd_validate", "cmd_global", "cmd_rollback"):
        monkeypatch.setattr(cli, name, lambda a, n=name: seen.setdefault("call", (n, a)) and 0)
    cli.main(argv)
    return seen["call"]


def test_contract_command_forms_and_default_folders(monkeypatch, tmp_path: Path):
    repo = tmp_path / "shop"
    repo.mkdir()
    name, a = _parse(monkeypatch, ["doctor", str(repo)])
    assert name == "cmd_doctor" and Path(a.out) == tmp_path / "shop.dmig" / "doctor"
    name, a = _parse(monkeypatch, ["analyze", str(repo), "--target", "net10.0"])
    assert Path(a.out) == tmp_path / "shop.dmig" / "analysis"
    name, a = _parse(monkeypatch, ["migrate", str(repo), "--group", "app-a", "--target", "net10.0"])
    assert name == "cmd_implement" and Path(a.out).name == "migrate-app-a"
    assert Path(a.plan) == tmp_path / "shop.dmig" / "analysis" / "migration-plan.json"
    name, a = _parse(monkeypatch, ["validate", str(repo), "--scope", "repository"])
    assert name == "cmd_validate" and Path(a.out).name == "validate-repository"
    name, a = _parse(monkeypatch, ["implement", "--repo", str(repo), "--group", "g", "--out", str(tmp_path / "o")])
    assert Path(a.out) == tmp_path / "o"                       # the older form still works
    with pytest.raises(SystemExit):
        cli.main(["analyze", str(repo), "--out", str(repo / "inside")])   # never inside the repository


def test_result_in_the_contract_shape_validates():
    result = {"status": "ROLLED_BACK", "group_id": "app-a", "target_framework": "net10.0",
              "files_changed": ["src/A/A.csproj", "src/A/Program.cs"], "transformations": [
                  {"kind": "target_framework", "file": "src/A/A.csproj"},
                  {"kind": "central_package_version", "package": "X", "file": "Directory.Packages.props"}],
              "baseline": {"restore_succeeded": True, "build_succeeded": True,
                           "tests": {"total": 10, "passed": 9, "failed": 1, "skipped": 2}},
              "final_validation": {"restore_succeeded": True, "build_succeeded": False,
                                   "tests": {"total": 10, "passed": 8, "failed": 2, "skipped": 2}},
              "final_gate": [{"check": "diff_within_group_scope", "passed": False, "detail": "src/B/B.cs"}],
              "rollback": {"performed": True}, "rollback_verified": True, "unresolved_items": ["x"],
              "ready_for_human_review": False}
    plan = {"source_frameworks": ["net8.0"], "repository_commit": "abc",
            "groups": [{"group_id": "app-a", "project_ids": ["src/A/A.csproj"], "test_project_ids": ["tests/A/A.csproj"]}]}
    doc = to_contract(result, plan)
    schema = json.loads((ROOT / "schemas" / "dmig-result.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(doc, schema)
    assert doc["status"] == "ROLLED_BACK" and doc["projectsDiscovered"] == 2 and doc["projectsChanged"] == 1
    assert doc["sourceChanges"] == ["src/A/Program.cs"] and doc["packageChanges"][0]["package"] == "X"
    assert doc["scopeViolations"] == ["src/B/B.cs"] and doc["rollback"] == {"required": True, "verified": True}
    assert doc["baseline"]["testsEnvironmentBlocked"] == 2


def test_rollback_cleans_a_left_over_workspace(tmp_path: Path):
    from dmig.dotnet import Runner
    from dmig.validate import rollback_run
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("a", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(GIT + args, cwd=repo, check=True, capture_output=True)
    run = tmp_path / "run"
    run.mkdir()
    subprocess.run(GIT + ["worktree", "add", "--detach", str(run / "worktree"), "HEAD"], cwd=repo, check=True,
                   capture_output=True)
    (run / "g.patch").write_text("diff", encoding="utf-8")
    report = rollback_run(run, Runner("dotnet", run / "logs"))
    assert report["verified"] and not (run / "worktree").exists()
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert len([line for line in listed.splitlines() if line.strip()]) == 1      # only the main checkout is left
    assert "git apply -R" in report["notes"][0]


@requires(*FULL)
def test_validate_one_group_changes_nothing(sample_repo: Path, tmp_path: Path):
    from dmig.analysis import run_analysis
    from dmig.dotnet import Runner
    from dmig.validate import validate_group
    out = tmp_path / "analysis"
    run_analysis(sample_repo, out, Runner(dotnet_path(), out / "logs"), {"run_baseline": True, "package_catalog": str(CATALOG),
                                                                          "allow_reversible_experiments": False})
    plan = json.loads((out / "migration-plan.json").read_text(encoding="utf-8"))
    baseline = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    r = validate_group(sample_repo, tmp_path / "v", Runner(dotnet_path(), tmp_path / "v" / "logs"), plan,
                       "app-appb-worker", None, baseline)
    assert r["status"] == "VALIDATION_PASSED", r["unresolved_items"]
    assert r["rollback_verified"] and not (tmp_path / "v" / "worktree").exists()
    assert any("net10.0" in u for u in r["unresolved_items"])      # still on net8.0: said, not hidden
    assert (out / "project-graph.json").exists() and (out / "migration-groups.json").exists()
