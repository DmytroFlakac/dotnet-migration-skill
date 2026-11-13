"""Generates schemas/*.schema.json (JSON Schema 2020-12). Run: python schemas/make_schemas.py

Kept as code so shared definitions (change classes, statuses) stay in one place.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
CHANGE_CLASS = {"enum": ["PROJECT_LOCAL", "GROUP_SHARED", "REPOSITORY_GLOBAL"]}
STR, INT, BOOL, NUM = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
NSTR = {"type": ["string", "null"]}


def arr(items=None):
    return {"type": "array", "items": items or {}}


def obj(required: dict, optional: dict | None = None, extra: bool = True):
    props = {**required, **(optional or {})}
    return {"type": "object", "required": list(required), "properties": props, "additionalProperties": extra}


DIAG = obj({"severity": {"enum": ["error", "warning"]}, "code": STR, "message": STR,
            "file": STR, "line": {"type": ["integer", "null"]}, "project": NSTR, "failure_class": STR})
TEST = obj({"project": STR, "test": STR,
            "outcome": {"enum": ["passed", "failed", "skipped", "unknown", "timeout", "aborted", "inconclusive"]}})
COMMAND = obj({"command": STR, "exit_code": INT, "duration_s": NUM, "log": NSTR})

SCHEMAS = {
    # the contract's camelCase names or the earlier snake_case names; contract.normalize_request maps them
    "request": {**obj({}, {"repository": STR, "mode": {"enum": ["analysis", "implementation", "global", "validate",
                                                                "validation", "rollback"]},
                           "targetFramework": {"const": "net10.0"}, "group": NSTR,
                           "maxRepairs": {"type": "integer", "minimum": 0, "maximum": 3},
                           "outputDir": STR, "allowPublicNuget": BOOL,
                           "repository_path": STR, "target_framework": {"const": "net10.0"},
                           "allow_public_nuget": BOOL, "selected_group_id": NSTR, "allow_reversible_experiments": BOOL,
                    "max_repair_attempts": {"type": "integer", "minimum": 0, "maximum": 3},
                    "output_directory": STR, "network_policy": STR,
                    "phase": {"enum": ["GROUP_MIGRATION", "MULTITARGET_SHARED"]},
                    "shared_library_policy": {"const": "MULTITARGET_UNTIL_LAST_CONSUMER"},
                    "allow_repository_global_changes": BOOL, "global_plan_id": STR,
                    "allow_network": BOOL, "package_catalog": NSTR,
                    "adapters": arr({"enum": ["aspnet_core", "efcore", "windows_service"]}),
                    "scope": {"enum": ["group", "repository"]}, "time_budget_minutes": {"type": ["number", "null"]}}),
                "anyOf": [{"required": ["repository", "mode"]}, {"required": ["repository_path", "mode"]}]},
    "preflight": obj({
        "schema_version": {"const": "1.0"},
        "status": {"enum": ["READY", "READY_WITH_LIMITATIONS", "BLOCKED"]},
        "sdks": arr(obj({"version": STR, "root": STR})),
        "runtimes": arr(obj({"version": STR, "root": STR})),
        "platforms": arr(obj({"os": STR})),
        "package_sources": arr(obj({"name": STR, "url": STR, "status": STR})),
        "resources": obj({"cpu_cores": {"type": ["integer", "null"]}, "memory_mb": {"type": ["integer", "null"]},
                          "disk_free_mb": INT, "network_egress_policy": STR}),
        "disposable_workspace": obj({"git_available": BOOL, "worktree_supported": BOOL, "preferred_method": STR}),
        "external_test_services": arr(), "licensed_tools": arr(),
        "capabilities": obj({k: BOOL for k in ("can_evaluate_msbuild", "can_build", "can_test_net8",
                                               "can_test_net10", "can_build_windows_projects", "can_experiment")}),
        "limitations": arr(obj({"code": STR, "detail": STR, "affects": STR})),
        "blocking_items": arr(obj({"code": STR, "detail": STR})),
    }),
    "inventory": obj({
        "schema_version": {"const": "1.0"}, "repository_commit": NSTR,
        "solutions": arr(obj({"path": STR, "projects": arr(STR)})),
        "projects": arr(obj({
            "id": STR, "name": STR, "kind": {"enum": ["application", "library", "test"]},
            "target_frameworks": arr(STR), "platform": {"enum": ["ANY", "WINDOWS"]}, "sdk_style": BOOL,
            "evaluated": BOOL, "project_references": arr(STR), "packages": arr(obj({"id": STR, "version": NSTR})),
        })),
        "shared_build_files": arr(STR), "central_package_files": arr(STR), "ci_entrypoints": arr(STR),
        "project_reference_edges": arr(obj({"from": STR, "to": STR})), "unresolved_imports": arr(),
    }),
    "baseline": obj({
        "schema_version": {"const": "1.0"}, "commands": arr(COMMAND),
        "restore_succeeded": BOOL, "build_succeeded": BOOL,
        "projects_built": arr(obj({"name": STR, "output": STR})),
        "projects_skipped": arr(obj({"project": STR, "reason": STR})),
        "tests": obj({"executed": BOOL, "total": INT, "passed": INT, "failed": INT, "skipped": INT,
                      "results": arr(TEST)}),
        "warnings": arr(DIAG), "errors": arr(DIAG), "environmental_blocks": arr(obj({"code": STR, "detail": STR})),
    }),
    "migration-plan": obj({
        "schema_version": {"const": "1.0"}, "plan_id": STR, "recipe_id": STR,
        "target_framework": {"const": "net10.0"},
        "shared_library_policy": {"const": "MULTITARGET_UNTIL_LAST_CONSUMER"},
        "phases": arr(obj({"id": STR, "scope": CHANGE_CLASS, "status": STR, "items": arr(obj({"change": STR}))})),
        "groups": arr(obj({
            "group_id": STR, "name": STR, "order": INT,
            "group_kind": {"enum": ["SHARED_FOUNDATION", "APPLICATION", "STANDALONE_LIBRARY"]},
            "project_ids": arr(STR), "test_project_ids": arr(STR), "shared_project_ids": arr(STR),
            "depends_on_group_ids": arr(STR), "entrypoints": arr(STR), "platform": {"enum": ["ANY", "WINDOWS"]},
            "platform_constraints": arr(STR), "grouping_evidence": arr(STR),
            "confidence": {"enum": ["high", "medium", "low"]},
            "readiness": {"enum": ["READY", "NEEDS_KNOWN_REPAIR", "WAITS_FOR_DEPENDENCY_REPAIR", "NEEDS_HUMAN_REVIEW",
                                   "BLOCKED_PLATFORM", "BLOCKED_NEEDS_INVESTIGATION",
                                   "NOT_VERIFIED_NO_EXPERIMENT", "NOT_VERIFIED_NO_BASELINE",
                                   "OUT_OF_SCOPE_ADAPTER_DISABLED", "OUT_OF_SCOPE_REPORT_ONLY"]},
            "proposed_changes": arr(obj({"file": STR, "change_class": CHANGE_CLASS, "kind": STR})),
            "implementation_request": obj({
                "phase": {"enum": ["GROUP_MIGRATION", "MULTITARGET_SHARED"]}, "selected_group_id": STR,
                "shared_library_policy": {"const": "MULTITARGET_UNTIL_LAST_CONSUMER"},
                "allow_repository_global_changes": {"const": False}, "global_plan_id": STR}),
        })),
        "shared_projects": arr(obj({
            "project_id": STR, "current_consumers": arr(STR), "remaining_net8_consumers": arr(STR),
            "temporary_tfms": arr(STR), "retirement_condition_for_net8": STR, "package_strategy_per_tfm": arr(),
            "owning_migration_phase": STR, "validation_matrix": arr(obj({"tfm": STR, "build": BOOL, "tests": arr(STR)})),
        })),
        "central_package_strategies": arr(obj({
            "package": STR, "strategy": {"enum": ["ONE_VERSION_BOTH_TFMS", "CONDITIONAL_PER_TFM",
                                                  "DEFER_AS_CROSS_GROUP_BLOCKER", "UNKNOWN"]},
            "change_class": CHANGE_CLASS})),
        "blockers": arr(obj({"detail": STR})), "uncertainties": arr(obj({"code": STR, "detail": STR})),
        "final_validation_plan": arr(STR),
    }),
    "response": obj({
        "schema_version": {"const": "1.0"},
        "status": {"enum": ["READY_FOR_PLAN", "READY_FOR_PLAN_WITH_LIMITATIONS", "BLOCKED"]},
        "mode": {"const": "analysis"}, "source_frameworks": arr(STR), "target_framework": STR,
        "inventory_path": STR, "baseline_path": NSTR, "migration_plan_path": STR, "human_report_path": STR,
        "groups": arr(obj({"group_id": STR, "order": INT, "readiness": STR})),
        "changes_persisted": {"const": False}, "rollback_verified": BOOL,
        "unresolved_items": arr(STR), "ready_for_human_review": BOOL,
    }),

    "result": obj({
        "schema_version": {"const": "1.0"}, "mode": {"enum": ["implementation", "global_phase"]}, "group_id": NSTR,
        "status": {"enum": ["SUCCESS", "ROLLED_BACK", "REJECTED", "BLOCKED_DEPENDENCY_ORDER",
                            "BLOCKED_GLOBAL_PHASE_REQUIRED", "SKIPPED_NOT_APPLICABLE", "STARTED",
                            "BLOCKED_NOT_READY", "NOTHING_TO_DO", "VALIDATION_PASSED", "VALIDATION_FAILED",
                            "BLOCKED_PLATFORM"]},
        "target_framework": {"const": "net10.0"}, "recipe_ids": arr(NSTR), "files_changed": arr(STR),
        "transformations": arr(obj({"kind": STR, "change_class": {"enum": ["PROJECT_LOCAL", "GROUP_SHARED", "REPOSITORY_GLOBAL", None]}})),
        "repair_attempts": {"type": "array", "maxItems": 3, "items": obj({
            "attempt": {"type": "integer", "minimum": 0, "maximum": 3}, "failures": arr(),  # 0: stopped before any repair
            "selected_rules": arr(STR), "files_changed": arr(STR),
            "validation_result": {"enum": ["passed", "failed", "not_attempted", None]},
            "next_action": {"enum": ["final_gate", "retry", "rollback", "stop_for_human_review", None]}})},
        "unresolved_items": arr(STR), "excluded_work": arr(STR), "proposed_global_changes": arr(obj({"file": STR, "change": STR})),
        "rollback": obj({"performed": BOOL, "verified": BOOL}), "changes_persisted": {"const": False},
        "rollback_verified": BOOL,
        "ready_for_human_review": BOOL,
    }, {"final_gate": arr(obj({"check": STR, "passed": BOOL, "detail": STR})), "patch_path": NSTR,
        "stop_reason": NSTR, "missing_capabilities": arr(STR),
        "phase": {"enum": ["PHASE_1_SDK_GLOBAL", "PHASE_5_RETIRE_NET8", "PHASE_6_CONVERGE",
                           "PHASE_7_REPO_VALIDATION", "PHASE_8_CUTOVER"]},
        "manual_follow_ups": arr(STR)}),

    # The integration result shape, written next to result.json.
    "dmig-result": obj({
        "schemaVersion": {"const": "1.0"},
        "status": {"enum": ["SUCCEEDED", "PARTIAL", "BLOCKED", "FAILED", "ROLLED_BACK"]},
        "mode": {"enum": ["implementation", "validation"]},
        "sourceFrameworks": arr(STR), "targetFramework": STR, "repositoryCommit": NSTR, "groupId": NSTR,
        "projectsDiscovered": INT, "projectsChanged": INT, "projectsSkipped": INT,
        "packageChanges": arr(), "sourceChanges": arr(STR), "globalChangesProposed": arr(),
        "baseline": obj({"buildPassed": BOOL, "testsPassed": INT, "testsFailed": INT, "testsEnvironmentBlocked": INT}),
        "finalValidation": obj({"buildPassed": BOOL, "testsPassed": INT, "testsFailed": INT,
                                "testsEnvironmentBlocked": INT}),
        "repairAttempts": arr(), "manualActions": arr(STR), "scopeViolations": arr(STR),
        "rollback": obj({"required": BOOL, "verified": BOOL}),
        # integration contract fields
        "groups": arr(STR), "changes": arr(), "repairs": arr(), "stopReason": STR,
        "missingCapabilities": arr(STR), "unvalidated": arr(STR), "rollbackVerified": BOOL,
        "artifacts": obj({"report": STR}, {"patch": STR, "inventory": STR, "plan": STR, "analysisReport": STR,
                                            "doctor": STR}),
        "readyForHumanReview": BOOL,
    }),
}

if __name__ == "__main__":
    for name, body in SCHEMAS.items():
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "$id": f"https://example.invalid/dotnet-migration-skill/{name}.schema.json",
                  "title": f"dotnet-migration-skill {name}", **body}
        (HERE / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(SCHEMAS)} schemas")
