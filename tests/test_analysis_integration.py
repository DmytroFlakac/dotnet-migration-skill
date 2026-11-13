"""Acceptance test for analysis mode, against the synthetic monorepo.

Needs a dotnet with SDK 10 and runtimes 8 + 10 ($DMIG_DOTNET or PATH) and git. Slow (~30–60 s).
Run: DMIG_DOTNET=/path/to/dotnet pytest tests/test_analysis_integration.py
"""
import json
import subprocess
from pathlib import Path

import pytest

from dmig.analysis import run_analysis, validate_outputs
from dmig.dotnet import Runner

from conftest import CATALOG, FULL, ROOT, dotnet_path, requires

GOLDEN = ROOT / "tests" / "golden" / "synthetic-analysis.json"


def _status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


def summarize(out: Path) -> dict:
    """The stable, meaningful subset of the outputs (no paths, timings or commit ids)."""
    plan = json.loads((out / "migration-plan.json").read_text(encoding="utf-8"))
    base = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    exp = json.loads((out / "experiment.json").read_text(encoding="utf-8"))
    return {
        "groups": [{k: g[k] for k in ("group_id", "order", "group_kind", "platform", "project_ids",
                                      "test_project_ids", "depends_on_group_ids", "readiness")}
                   for g in plan["groups"]],
        "shared_projects": [{k: s[k] for k in ("project_id", "temporary_tfms", "consumer_groups")}
                            for s in plan["shared_projects"]],
        "baseline_failures": sorted(r["test"] for r in base["tests"]["results"] if r["outcome"] == "failed"),
        "baseline_totals": {k: base["tests"][k] for k in ("total", "passed", "failed")},
        "experiment_new_errors": sorted({(d["code"], d["project"]) for d in exp["diagnostic_comparison"]["new_errors"]}),
        "experiment_regressions": sorted(r["test"] for r in exp["test_comparison"]["regressions"]),
        "experiment_baseline_failures_kept": sorted(r["test"] for r in exp["test_comparison"]["baseline_failures_still_failing"]),
        "phase_statuses": {p["id"]: p["status"] for p in plan["phases"]},
    }


@requires(*FULL)
def test_analysis_mode_acceptance(sample_repo: Path, tmp_path: Path, request):
    before = _status(sample_repo)
    out = tmp_path / "out"
    response = run_analysis(sample_repo, out, Runner(dotnet_path(), out / "logs"),
                            {"run_baseline": True, "allow_reversible_experiments": True, "package_catalog": str(CATALOG)})

    # 10. schema-valid JSON and a human-readable report
    assert validate_outputs(out) == []
    assert (out / "migration-report.md").read_text(encoding="utf-8").startswith("# .NET 8")
    # Analysis mode never persists changes; the experiment's rollback is verified.
    assert response["changes_persisted"] is False
    assert response["rollback_verified"] is True
    assert _status(sample_repo) == before
    # Even ignored build output must not appear in the repository: the baseline runs in a worktree.
    assert not [p for p in sample_repo.rglob("*") if p.name in {"bin", "obj", "TestResults"}]
    assert not list((out / "worktrees").iterdir())

    # Obsidian export: links resolve, every note has properties, diagrams within limits, start note
    # fits one screen (all checked by obsreport's validator), and the note set has its fixed spine.
    from obsreport import Vault
    root = next((out / "obsidian").iterdir())
    r = root.name.replace(" .NET 10 migration", "")
    assert Vault(root, fresh=False).validate({f"{r} - 00 Start here": 70}) == []
    names = {n.stem for n in root.rglob("*.md")}
    for spine in ("00 Start here", "01 What will break", "02 Route", "03 Map", "04 Decisions", "05 Reviewer FAQ"):
        assert f"{r} - {spine}" in names
    assert (root / f"{r} - Map board.canvas").exists()
    start = (root / f"{r} - 00 Start here.md").read_text(encoding="utf-8")
    assert "Top fixes" in start and "confirmed" in start
    # Readable outputs are summary-sized.
    assert (out / "response.json").stat().st_size < 4000
    assert (out / "migration-report.md").stat().st_size < 8000

    got = summarize(out)
    if request.config.getoption("--update-golden", default=False):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2, default=list) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert json.loads(json.dumps(got, default=list)) == expected

    # Explicit acceptance criteria (also covered by the golden file, spelled out for readers):
    # 1–3. inventory, graph, sensible groups and ordering
    assert [g["group_id"] for g in got["groups"]][0] == "shared-foundation"
    # 4. + 8. the pre-existing failure is recorded and stays a baseline failure, not a regression
    assert got["baseline_failures"] == ["AppA.Tests.InvoiceCalculatorTests.VatRate_Germany_KnownBaselineFailure"]
    assert got["experiment_baseline_failures_kept"] == got["baseline_failures"]
    # 5. the reversible experiment finds both migration-induced failures
    assert ["CS0121", "src/AppB/AppB.Core/AppB.Core.csproj"] in [list(e) for e in got["experiment_new_errors"]]
    assert "Shared.Core.Tests.MoneyTests.Constructor_SetsAmount" in got["experiment_regressions"]
