"""The result in the shape agreed for integrations.

Every implementation, global-phase and validation run writes `result.json` (the detailed, internal
format) and, next to it, `dmig-result.json` in the contract's shape, built from the same data.
"""
from __future__ import annotations

import json
from pathlib import Path

STATUS = {"SUCCESS": "SUCCEEDED", "VALIDATION_PASSED": "SUCCEEDED", "NOTHING_TO_DO": "SUCCEEDED",
          "ROLLED_BACK": "ROLLED_BACK", "VALIDATION_FAILED": "FAILED"}
SOURCE_EXT = (".cs", ".vb", ".fs", ".razor", ".cshtml")


def _tests(summary: dict | None) -> dict:
    s = summary or {}
    t = s.get("tests") or {}
    return {"buildPassed": bool(s.get("restore_succeeded")) and bool(s.get("build_succeeded")),
            "testsPassed": t.get("passed", 0), "testsFailed": t.get("failed", 0),
            # skipped tests are the ones whose service/platform was not configured (e.g. a database
            # provider without settings); test projects that could not run at all are counted apart
            # ... plus tests that failed twice with timeouts/deadlocks (environment unstable)
            "testsEnvironmentBlocked": t.get("skipped", 0) + len(s.get("environment_unstable_tests") or []),
            "testProjectsNotRun": s.get("environment_blocked_projects", 0)}


def to_contract(result: dict, plan: dict | None = None, inventory: dict | None = None,
                analysis_dir: Path | None = None, mode: str = "implementation") -> dict:
    group = next((g for g in (plan or {}).get("groups", []) if g["group_id"] == result.get("group_id")), None)
    planned = (group["project_ids"] + group["test_project_ids"]) if group else \
        [p["id"] for p in (inventory or {}).get("projects", [])]
    changed_files = result.get("files_changed", [])
    changed_projects = sorted({p for p in planned for f in changed_files
                               if f == p or f.startswith(p.rsplit("/", 1)[0] + "/")})
    trans = result.get("transformations", [])
    gate = result.get("final_gate") or []
    artifacts = {"inventory": "inventory.json", "plan": "migration-plan.json", "report": "migration-report.md",
                 "doctor": "environment-capabilities.json"}
    if analysis_dir:
        artifacts = {k: str(analysis_dir / v) for k, v in artifacts.items()}
    if result.get("patch_path"):
        artifacts["patch"] = result["patch_path"]
    return {
        "schemaVersion": "1.0",
        "status": STATUS.get(result.get("status"), "BLOCKED"),
        "internalStatus": result.get("status"),
        "mode": mode,
        "phase": result.get("phase"),
        "sourceFrameworks": (plan or {}).get("source_frameworks") or ["net8.0"],
        "targetFramework": result.get("target_framework", "net10.0"),
        "repositoryCommit": (inventory or {}).get("repository_commit") or (plan or {}).get("repository_commit"),
        "groupId": result.get("group_id"),
        "projectsDiscovered": len(planned),
        "projectsChanged": len(changed_projects),
        "projectsSkipped": max(0, len(planned) - len(changed_projects)),
        "packageChanges": [t for t in trans if "package" in t.get("kind", "") or t.get("package")],
        "sourceChanges": [f for f in changed_files if f.endswith(SOURCE_EXT)],
        "globalChangesProposed": result.get("proposed_global_changes", []),
        "baseline": _tests(result.get("baseline")),
        "finalValidation": _tests(result.get("final_validation")),
        "repairAttempts": result.get("repair_attempts", []),
        "manualActions": list(result.get("manual_follow_ups") or []) + list(result.get("unresolved_items") or []),
        "scopeViolations": [g["detail"] for g in gate
                            if not g["passed"] and g["check"] in {"diff_within_group_scope", "no_repository_global_change",
                                                                  "diff_limited_to_phase_files"}],
        "rollback": {"required": bool((result.get("rollback") or {}).get("performed")),
                     "verified": bool(result.get("rollback_verified"))},
        "artifacts": artifacts,
        "readyForHumanReview": bool(result.get("ready_for_human_review")),
    }


def write_contract(out: Path, result: dict, plan_path: Path | None, mode: str = "implementation") -> Path:
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path and plan_path.exists() else None
    inv_path = plan_path.parent / "inventory.json" if plan_path else None
    inventory = json.loads(inv_path.read_text(encoding="utf-8")) if inv_path and inv_path.exists() else None
    doc = to_contract(result, plan, inventory, plan_path.parent if plan_path else None, mode)
    path = out / "dmig-result.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
