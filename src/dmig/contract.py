"""The result in the shape the integration contract asks for.

Every implementation, global-phase and validation run writes `result.json` (the detailed, internal
format) and, next to it, `dmig-result.json` in the contract's shape, built from the same data, plus
`migration-report.md` (and `migration.patch` when there is a change) through the delivery adapter.

Request: `normalize_request()` accepts the contract's camelCase fields (repository, mode,
targetFramework, group, maxRepairs, outputDir, allowPublicNuget) and the older snake_case ones.
"""
from __future__ import annotations

import json
from pathlib import Path

STATUS = {"SUCCESS": "SUCCEEDED", "VALIDATION_PASSED": "SUCCEEDED", "NOTHING_TO_DO": "SUCCEEDED",
          "ROLLED_BACK": "ROLLED_BACK", "VALIDATION_FAILED": "FAILED", "BLOCKED_PLATFORM": "BLOCKED"}
SOURCE_EXT = (".cs", ".vb", ".fs", ".razor", ".cshtml")
REQUEST_FIELDS = {"repository": "repository_path", "targetFramework": "target_framework", "group": "selected_group_id",
                  "maxRepairs": "max_repair_attempts", "outputDir": "output_directory",
                  "allowPublicNuget": "allow_public_nuget"}
MODES = {"analysis": "analysis", "implementation": "implementation", "global": "global", "validate": "validation",
         "validation": "validation", "rollback": "rollback"}


def normalize_request(request: dict | None) -> dict:
    """The contract's camelCase request as the internal snake_case one; both spellings work."""
    out = dict(request or {})
    for camel, snake in REQUEST_FIELDS.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]
    if "mode" in out:
        out["mode"] = MODES.get(out["mode"], out["mode"])
    if "max_repair_attempts" in out:
        out["max_repair_attempts"] = min(int(out["max_repair_attempts"]), 3)
    return out


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


def _unvalidated(result: dict) -> list[str]:
    """What the run could not check (never counted as passed): NOT_EXECUTED_ENVIRONMENT_BLOCKED."""
    final = result.get("final_validation") or {}
    items = [f"{r['test']} ({r.get('tfm')}): environment unstable" for r in final.get("environment_unstable_tests") or []]
    items += [f"{r.get('test') or r.get('project')}: not run" for r in final.get("tests_not_run") or []]
    if final.get("environment_blocked_projects"):
        items.append(f"{final['environment_blocked_projects']} test project(s): NOT_EXECUTED_ENVIRONMENT_BLOCKED")
    if result.get("status") == "BLOCKED_PLATFORM":
        items.append(f"group {result.get('group_id')}: needs Windows (BLOCKED_PLATFORM)")
    return items


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
    artifacts = {"inventory": "inventory.json", "plan": "migration-plan.json", "analysisReport": "migration-report.md",
                 "doctor": "environment-capabilities.json"}
    if analysis_dir:
        artifacts = {k: str(analysis_dir / v) for k, v in artifacts.items()}
    artifacts["report"] = "migration-report.md"
    if result.get("patch_path"):
        artifacts["patch"] = "migration.patch"
    unvalidated = _unvalidated(result)
    status = STATUS.get(result.get("status"), "BLOCKED")
    if status == "SUCCEEDED" and unvalidated:
        status = "PARTIAL"  # the gate passed, but part of it could not be checked here
    rollback_verified = bool(result.get("rollback_verified", (result.get("rollback") or {}).get("verified")))
    return {
        "schemaVersion": "1.0",
        "status": status,
        "internalStatus": result.get("status"),
        "mode": mode,
        "phase": result.get("phase"),
        "sourceFrameworks": (plan or {}).get("source_frameworks") or ["net8.0"],
        "targetFramework": result.get("target_framework", "net10.0"),
        "repositoryCommit": (inventory or {}).get("repository_commit") or (plan or {}).get("repository_commit"),
        "groupId": result.get("group_id"),
        "groups": [result["group_id"]] if result.get("group_id") else [],
        "projectsDiscovered": len(planned),
        "projectsChanged": len(changed_projects),
        "projectsSkipped": max(0, len(planned) - len(changed_projects)),
        "changes": [{"file": t.get("file"), "kind": t.get("kind"), "changeClass": t.get("change_class")}
                    for t in trans if t.get("file")],
        "packageChanges": [t for t in trans if "package" in t.get("kind", "") or t.get("package")],
        "sourceChanges": [f for f in changed_files if f.endswith(SOURCE_EXT)],
        "globalChangesProposed": result.get("proposed_global_changes", []),
        "baseline": _tests(result.get("baseline")),
        "finalValidation": _tests(result.get("final_validation")),
        "repairs": result.get("repair_attempts", []),
        "repairAttempts": result.get("repair_attempts", []),  # earlier name
        "stopReason": result.get("stop_reason") or "",
        "missingCapabilities": result.get("missing_capabilities", []),
        "unvalidated": unvalidated,
        "manualActions": list(result.get("manual_follow_ups") or []) + list(result.get("unresolved_items") or []),
        "scopeViolations": [g["detail"] for g in gate
                            if not g["passed"] and g["check"] in {"diff_within_group_scope", "no_repository_global_change",
                                                                  "diff_limited_to_phase_files"}],
        "rollbackVerified": rollback_verified,
        "rollback": {"required": bool((result.get("rollback") or {}).get("performed")), "verified": rollback_verified},
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
