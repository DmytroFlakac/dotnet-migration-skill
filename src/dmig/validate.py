"""`dmig validate --scope group`: restore, build and test one migration group as it is now in the
repository (its projects, every project that consumes them, and their tests), in a disposable
worktree, compared with the analysis baseline. Nothing is changed.

`--scope repository` is the repository-wide validation (PHASE_7) in globalphase.py.
A group that needs Windows on a machine without it is reported NOT_EXECUTED_PLATFORM_UNAVAILABLE: analysed, not validated, never "passed".
"""
from __future__ import annotations

import platform
import shutil
import time
from pathlib import Path

from . import SCHEMA_VERSION, TARGET_TFM
from .buildtest import SCOPE_FILTER, run_build_and_test, write_scope_filter
from .discovery import run_inventory
from .dotnet import Runner
from .experiment import compare_tests, settle_regressions, snapshot
from .graph import ProjectGraph
from .implement import _bt_summary


def _in_scope(bt: dict, scope: set[str]) -> dict:
    return dict(bt, tests=dict(bt["tests"], results=[r for r in bt["tests"]["results"] if r["project"] in scope]))


def validate_group(repo: Path, out: Path, runner: Runner, plan: dict, group_id: str, preflight: dict | None,
                   reference_baseline: dict | None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    group = next((g for g in plan["groups"] if g["group_id"] == group_id), None)
    result = {"schema_version": SCHEMA_VERSION, "mode": "validation", "scope": "group", "group_id": group_id,
              "plan_id": plan.get("plan_id"), "target_framework": TARGET_TFM, "status": "STARTED",
              "files_changed": [], "transformations": [], "repair_attempts": [], "unresolved_items": [],
              "proposed_global_changes": [], "rollback": {"performed": False, "verified": False},
              "changes_persisted": False, "rollback_verified": False, "ready_for_human_review": False}
    if group is None:
        result.update(status="REJECTED", unresolved_items=[f"unknown group {group_id}"])
        return result
    if group.get("platform") == "WINDOWS" and platform.system() != "Windows":
        result.update(status="NOT_EXECUTED_PLATFORM_UNAVAILABLE",
                      unresolved_items=["this group needs Windows to build and test; analysed but not validated"])
        return result
    before = snapshot(repo, runner)
    wt = out / "worktree"
    shutil.rmtree(wt, ignore_errors=True)
    if not runner.git(["worktree", "add", "--detach", str(wt), "HEAD"], repo, "validate-worktree-add").ok:
        result.update(status="REJECTED", unresolved_items=["git worktree add failed"])
        return result
    try:
        inventory = run_inventory(wt, runner, before["commit"])
        graph = ProjectGraph(inventory["projects"])
        members = set(group["project_ids"] + group["test_project_ids"])
        scope = members | {c for m in group["project_ids"] for c in graph.all_consumers(m)}
        entry = write_scope_filter(wt, inventory, scope)
        caps = (preflight or {}).get("capabilities", {})
        current = run_build_and_test(wt, out, "validate", runner, inventory, caps, entrypoints=entry, scope=scope)
        (wt / SCOPE_FILTER).unlink(missing_ok=True)
        tests = None
        if reference_baseline:
            tests = settle_regressions(compare_tests(_in_scope(reference_baseline, scope), _in_scope(current, scope)),
                                       wt, out, "validate", runner)
        net8_left = [p["id"] for p in inventory["projects"] if p["id"] in members
                     and not any(t.startswith("net10.0") for t in p["target_frameworks"])]
        ok = current["restore_succeeded"] and current["build_succeeded"] and not (tests and tests["regressions"])
        result.update(
            status="VALIDATION_PASSED" if ok else "VALIDATION_FAILED",
            build_scope={"projects": sorted(scope), "filter": SCOPE_FILTER if entry else None},
            baseline=_bt_summary(reference_baseline) if reference_baseline else None,
            final_validation=_bt_summary(current) | {
                "regressions": (tests or {}).get("regressions", []), "flaky_tests": (tests or {}).get("flaky", []),
                "environmental_blocks": current["environmental_blocks"]},
            ready_for_human_review=True)
        result["unresolved_items"] += [f"still without a net10.0 target: {p}" for p in net8_left]
        if not reference_baseline:
            result["unresolved_items"].append("no analysis baseline to compare with: regressions not judged")
        if current["environmental_blocks"]:
            result["unresolved_items"].append(f"{len(current['environmental_blocks'])} test project(s) not executed "
                                              "(environment): not counted as passed")
        return result
    finally:
        runner.git(["worktree", "remove", "--force", str(wt)], repo, "validate-worktree-remove")
        runner.git(["worktree", "prune"], repo, "validate-worktree-prune")
        shutil.rmtree(wt, ignore_errors=True)
        after = snapshot(repo, runner)
        result["rollback_verified"] = result["rollback"]["verified"] = (
            before["commit"] == after["commit"] and before["status_sha256"] == after["status_sha256"])
        result["duration_s"] = round(time.monotonic() - started, 1)


def rollback_run(run_dir: Path, runner: Runner) -> dict:
    """`dmig rollback <run folder>`: runs never change the repository, so rolling back means making
    sure nothing of the run is left behind (its disposable worktrees) and saying whether its patch
    is currently applied (and how to undo that). Never reverts anything by itself."""
    report = {"run": str(run_dir), "worktrees_removed": [], "patch": None, "repository": None, "notes": []}
    for wt in sorted(p for p in run_dir.glob("**/worktree*") if p.is_dir()):
        common = runner.git(["rev-parse", "--git-common-dir"], wt, "rollback-common-dir")
        if common.ok:
            repo = Path(common.output.strip())
            repo = (wt / repo).resolve().parent if not repo.is_absolute() else repo.parent
            report["repository"] = str(repo)
            runner.git(["worktree", "remove", "--force", str(wt)], repo, "rollback-worktree-remove")
            runner.git(["worktree", "prune"], repo, "rollback-worktree-prune")
        shutil.rmtree(wt, ignore_errors=True)
        report["worktrees_removed"].append(str(wt))
    patch = next(iter(sorted(run_dir.glob("*.patch"))), None)
    result_file = run_dir / "result.json"
    if result_file.exists():
        import json
        r = json.loads(result_file.read_text(encoding="utf-8"))
        report["status"] = r.get("status")
        report["changes_persisted_by_run"] = r.get("changes_persisted", False)
    if patch:
        report["patch"] = str(patch)
        report["notes"].append(f"If you applied this patch and have not committed it: git apply -R \"{patch}\". "
                               "If you committed it: git revert <that commit>.")
    report["verified"] = not any(Path(w).exists() for w in report["worktrees_removed"])
    return report
