"""Readable outputs stay small on huge repositories (synthetic 600-group plan, no dotnet)."""
from pathlib import Path

from dmig import limits
from dmig.obsidian import export
from dmig.report import render


def _huge(n_groups: int = 600):
    projects, groups = [], []
    for i in range(n_groups):
        pid = f"src/App{i}/App{i}.csproj"
        projects.append({"id": pid, "name": f"App{i}", "kind": "application", "target_frameworks": ["net8.0"],
                         "platform": "ANY", "project_references": [], "packages": []})
        groups.append({"group_id": f"app-app{i}", "name": f"App{i}", "group_kind": "APPLICATION", "order": i + 1,
                       "project_ids": [pid], "test_project_ids": [], "shared_project_ids": [], "depends_on_group_ids": [],
                       "entrypoints": [pid], "platform": "ANY", "platform_constraints": [], "grouping_evidence": ["root"],
                       "confidence": "high", "readiness": "READY", "findings": [], "proposed_changes": [],
                       "experiment": {"new_errors": [], "new_rule_warnings": [], "test_regressions": [], "tests_not_run": []},
                       "implementation_request": {}})
    inventory = {"repository_commit": "abc", "projects": projects}
    plan = {"plan_id": "p", "groups": groups, "shared_projects": [], "blockers": [],
            "uncertainties": [{"code": "X", "detail": f"item {i}", "affects": "x"} for i in range(500)],
            "phases": [{"id": "PHASE_0_ANALYSIS", "scope": "REPOSITORY_GLOBAL", "status": "DONE", "description": "d",
                        "items": [{"change": f"c{i}"} for i in range(1000)]}]}
    response = {"status": "READY_FOR_PLAN", "changes_persisted": False, "rollback_verified": True,
                "summary": {"readiness_counts": {"READY": n_groups}}}
    preflight = {"status": "READY", "selected_sdk_in_repository": "10.0.102", "runtimes": [], "blocking_items": [], "limitations": []}
    compat = {"findings": []}
    return response, preflight, inventory, compat, plan


def test_report_is_capped_for_600_groups():
    response, preflight, inventory, compat, plan = _huge()
    md = render(response, preflight, inventory, None, None, compat, plan)
    assert md.count("\n| ") <= limits.REPORT_ROWS + 5
    assert "575 more" in md and len(md) < 12000


def test_obsidian_notes_stay_readable_for_600_groups(tmp_path: Path):
    import json

    from obsreport import Vault
    response, preflight, inventory, compat, plan = _huge()
    root = Path(export(tmp_path, tmp_path / "huge", response, preflight, inventory, None, None, compat, plan))
    start = (root / "huge - 00 Start here.md").read_text(encoding="utf-8")
    assert len(start.splitlines()) <= 70                           # one screen, whatever the size
    map_note = (root / "huge - 03 Map.md").read_text(encoding="utf-8")
    assert map_note.count('["') <= 12                              # layers, not 600 boxes
    assert "400 more" in map_note                                  # the group table is capped
    route = (root / "huge - 02 Route.md").read_text(encoding="utf-8")
    assert route.count("- [") <= limits.NOTE_ROWS + 1 and "800 more" in route
    assert len(list((root / "Groups").glob("*.md"))) == 600       # every group still gets its own note
    board = json.loads((root / "huge - Map board.canvas").read_text(encoding="utf-8"))
    assert len(board["nodes"]) == 600                              # the full map lives on the canvas
    assert Vault(root, fresh=False).validate() == []
