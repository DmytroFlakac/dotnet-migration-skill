"""Step A10: combine everything into the ordered migration plan (machine-readable)."""
from __future__ import annotations

import hashlib
import re

from . import SCHEMA_VERSION, SOURCE_TFM, TARGET_TFM


def _owner_map(grouping: dict) -> dict[str, str]:
    owner = {}
    for g in grouping["groups"]:
        for pid in g["project_ids"] + g["test_project_ids"]:
            owner[pid] = g["group_id"]
    return owner


def build_plan(inventory: dict, grouping: dict, tests: list[dict], compat: dict, baseline: dict | None,
               experiment: dict | None, preflight: dict, recipe: dict) -> dict:
    owner = _owner_map(grouping)
    plan_id = "plan-" + hashlib.sha256(f"{inventory['repository_commit']}|{len(inventory['projects'])}".encode()).hexdigest()[:12]
    findings = compat["findings"]
    rules = {r["id"]: r for r in recipe["rules"]}
    exp_bt = (experiment or {}).get("build_test") or {}
    # The experiment counts when it ran; each group is then verified only if all its own projects built
    # (one group's restore or build error must not hide the others' results).
    experiment_ok = bool(experiment and experiment.get("status") == "EXECUTED")
    baseline_ok = baseline is not None and baseline.get("build_succeeded", False)
    exp_built = {b["name"].lower() for b in exp_bt.get("projects_built", [])}
    proj_name = {p["id"]: p["name"].lower() for p in inventory["projects"]}
    # Without a baseline, every experiment diagnostic counts (nothing to subtract).
    diag_cmp = (experiment or {}).get("diagnostic_comparison") or {
        "new_errors": exp_bt.get("errors", []), "new_warnings": exp_bt.get("warnings", [])}
    test_cmp = (experiment or {}).get("test_comparison", {})
    validates = {t["test_project"]: t["validates"] for t in tests}
    deps = {p["id"]: set(p["project_references"]) for p in inventory["projects"]}

    def closure(pids) -> set[str]:
        seen, stack = set(), list(pids)
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(deps.get(cur, ()))
        return seen

    # Packages whose projects built and whose tests ran on net10.0 are confirmed by the experiment.
    built = {b["name"].lower() for b in exp_bt.get("projects_built", [])}
    ran_tests = {r["project"] for r in exp_bt.get("tests", {}).get("results", [])}
    for f in findings:
        if f["rule_id"] == "PKG-NOT-PROVEN" and experiment and experiment.get("status") == "EXECUTED":
            names = {p.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() for p in f["projects"]}
            tests_ok = all(p in ran_tests for p in f["projects"]
                           if next((q for q in inventory["projects"] if q["id"] == p), {}).get("kind") == "test")
            if names <= built and tests_ok:
                f["rule_id"], f["confidence"] = "PKG-CONFIRMED-BY-EXPERIMENT", "high"
                f["title"] = f["title"].replace("no known net10.0 issue", "built and ran on net10.0 in the experiment")
                f["note"] = "Every project using it built (and its tests ran) on net10.0."
            else:
                f["note"] = "Not proven: at least one project using it did not build or run on net10.0 in the experiment."

    diag_rules = [r for r in recipe["rules"] if r["kind"] == "diagnostic"]

    def matched_rule(diag: dict) -> str | None:
        for f in findings:
            if diag["code"] in f.get("expected_diagnostics", []) and (not f["projects"] or diag.get("project") in f["projects"]):
                return f["rule_id"]
        exact = next((r["id"] for r in diag_rules if diag["code"] in r.get("expected_diagnostics", [])), None)
        return exact or next((r["id"] for r in diag_rules
                              if any(diag["code"].startswith(p) for p in r.get("expected_diagnostic_prefixes", []))), None)

    groups = []
    for g in grouping["groups"]:
        members = set(g["project_ids"]) | set(g["test_project_ids"])
        g_findings = [f for f in findings if set(f.get("projects") or []) & members
                      and f.get("effect") not in {"none", "none_known"}]
        errors = [dict(d, matched_rule=matched_rule(d)) for d in diag_cmp.get("new_errors", []) if d.get("project") in members]
        warnings = [dict(d, matched_rule=matched_rule(d)) for d in diag_cmp.get("new_warnings", [])
                    if d.get("project") in members and matched_rule(d)]
        regressions = []
        for r in test_cmp.get("regressions", []):
            if r["project"] in members:
                reach = closure(validates.get(r["project"], []))
                causes = [f for f in findings
                          if f.get("effect") in {"silent_behavior_change", "runtime_failure", "runtime_failure_possible"}
                          and set(f.get("projects") or []) & reach
                          and (not rules.get(f["rule_id"], {}).get("regression_message")
                               or re.search(rules[f["rule_id"]]["regression_message"], r.get("message") or ""))]
                inherited = sorted({owner[p] for f in causes for p in f["projects"]
                                    if p in owner and owner[p] != g["group_id"]})
                regressions.append(dict(r, probable_cause=[f["rule_id"] for f in causes] or None,
                                        inherited_from_groups=inherited or None))
        not_run = [r for r in test_cmp.get("not_run_in_experiment", []) if r["project"] in members]
        human = [f["rule_id"] for f in g_findings if (f.get("repair") or {}).get("action", "").startswith("Human review")]
        unexplained = [e for e in errors if not e["matched_rule"]] + [r for r in regressions if not r["probable_cause"]]
        own_problems = errors + [r for r in regressions if not r["inherited_from_groups"]]
        host_windows = preflight.get("capabilities", {}).get("can_build_windows_projects", False)
        decisions = [e for e in errors
                     if rules.get(e["matched_rule"], {}).get("repair", {}).get("action", "").startswith(("Human review", "Check:"))]
        if unexplained:
            readiness = "BLOCKED_NEEDS_INVESTIGATION"
        elif decisions:  # explained, but the fix is a decision or a manual code change, not an automatic repair
            readiness = "NEEDS_HUMAN_REVIEW"
        elif own_problems:
            readiness = "NEEDS_KNOWN_REPAIR"
        elif regressions:
            readiness = "WAITS_FOR_DEPENDENCY_REPAIR"
        elif human:
            readiness = "NEEDS_HUMAN_REVIEW"
        elif g["platform"] == "WINDOWS" and not host_windows:
            readiness = "BLOCKED_PLATFORM"
        elif not experiment_ok or not all(proj_name[p] in exp_built for p in g["project_ids"] + g["test_project_ids"]):
            readiness = "NOT_VERIFIED_NO_EXPERIMENT"
        elif not baseline_ok:
            readiness = "NOT_VERIFIED_NO_BASELINE"
        else:
            readiness = "READY"
        # Scope wins over everything: a group with a project of a disabled framework add-on (or a
        # report-only type) is listed, never migrated.
        scopes = {next((p.get("scope", "IN_SCOPE") for p in inventory["projects"] if p["id"] == m), "IN_SCOPE")
                  for m in g["project_ids"]}
        if "OUT_OF_SCOPE_REPORT_ONLY" in scopes:
            readiness = "OUT_OF_SCOPE_REPORT_ONLY"
        elif "OUT_OF_SCOPE_ADAPTER_DISABLED" in scopes:
            readiness = "OUT_OF_SCOPE_ADAPTER_DISABLED"
        proposed = _proposed_changes(g, inventory, g_findings, grouping)
        phase = "MULTITARGET_SHARED" if g["group_kind"] == "SHARED_FOUNDATION" else "GROUP_MIGRATION"
        groups.append({
            **g,
            "readiness": readiness,
            "findings": [f["rule_id"] for f in g_findings],
            "experiment": {"new_errors": errors, "new_rule_warnings": warnings,
                           "test_regressions": regressions, "tests_not_run": not_run},
            "proposed_changes": proposed,
            "implementation_request": {
                "phase": phase, "selected_group_id": g["group_id"],
                "shared_library_policy": "MULTITARGET_UNTIL_LAST_CONSUMER",
                "allow_repository_global_changes": False, "global_plan_id": plan_id},
        })

    shared = []
    strategies = compat["central_package_strategies"]
    for s in grouping["shared_projects"]:
        pkgs = next((p["packages"] for p in inventory["projects"] if p["id"] == s["project_id"]), [])
        per_tfm = []
        for pk in pkgs:
            strat = next((x for x in strategies if x["package"] == pk["id"]), None)
            per_tfm.append({"package": pk["id"], "strategy": strat["strategy"] if strat else "ONE_VERSION_BOTH_TFMS",
                            "versions": {SOURCE_TFM: pk["version"],
                                         TARGET_TFM: (strat or {}).get("net10_version") or pk["version"]}})
        shared.append(dict(s, package_strategy_per_tfm=per_tfm))

    phases = _phases(recipe, inventory, compat, grouping, groups, preflight, baseline)
    blockers, uncertainties = [], []
    for g in groups:
        if g["readiness"] == "BLOCKED_NEEDS_INVESTIGATION":
            blockers.append({"group_id": g["group_id"], "detail": "net10.0 errors or regressions with no matching recipe rule."})
    for b in preflight.get("blocking_items", []):
        blockers.append({"group_id": None, "detail": f"{b['code']}: {b['detail']}"})
    grouped = {pid for g in groups for pid in g["project_ids"] + g["test_project_ids"]}
    stray = [d for d in diag_cmp.get("new_errors", []) if d.get("project") not in grouped]
    if stray:
        blockers.append({"group_id": None, "detail": f"{len(stray)} net10.0 experiment error(s) not attributable to a "
                         "group (e.g. restore or SDK errors): " + "; ".join(f"{d['code']} {d['message'][:80]}" for d in stray[:3])})
    if experiment and not experiment_ok:
        blockers.append({"group_id": None, "detail": "The net10.0 experiment did not run; no group is verified."})
    if compat.get("nuget_lookup_failures"):
        fails = compat["nuget_lookup_failures"]
        uncertainties.append({"code": "NUGET_LOOKUP_FAILED", "affects": "package strategies",
                              "detail": f"{len(fails)} NuGet metadata lookup(s) failed after a retry "
                                        f"(first: {fails[0]['error']}); affected versions are reported as unknown. "
                                        "Rerun the analysis when the configured package feed is reachable."})
    if baseline is None:
        uncertainties.append({"code": "NO_BASELINE", "affects": "all groups",
                              "detail": "No baseline: failures cannot be separated into pre-existing and migration-caused."})
    for l in preflight.get("limitations", []):
        uncertainties.append({"code": l["code"], "detail": l["detail"], "affects": l["affects"]})
    for u in grouping["unassigned_projects"]:
        uncertainties.append({"code": "UNASSIGNED_PROJECT", "detail": f"{u['project']}: {u['reason']}", "affects": "grouping"})
    for item in inventory["unresolved_project_references"]:
        uncertainties.append({"code": "UNRESOLVED_REFERENCE", "detail": f"{item['project']} → {item['reference']}", "affects": "graph"})
    unproven = [f for f in findings if f["rule_id"] == "PKG-NOT-PROVEN"]
    if unproven:  # one line, however many packages: the list itself is in compatibility.json
        names = ", ".join(f["package"] for f in unproven[:5]) + (f" and {len(unproven) - 5} more" if len(unproven) > 5 else "")
        uncertainties.append({"code": "PACKAGE_NOT_PROVEN", "affects": f"{len(unproven)} package(s)",
                              "detail": f"{len(unproven)} package(s) have no known net10.0 issue but were not proven by the "
                                        f"experiment (a project using them did not build or run): {names}. See compatibility.json."})
    for g in groups:
        if not g["test_project_ids"]:
            uncertainties.append({"code": "NO_TEST_COVERAGE", "affects": g["group_id"],
                                  "detail": f"{g['group_id']} has no test project: its migration is validated by build only."})
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "repository_commit": inventory["repository_commit"],
        "recipe_id": recipe["recipe_id"],
        "source_frameworks": sorted({t for p in inventory["projects"] for t in p["target_frameworks"]}),
        "target_framework": TARGET_TFM,
        "shared_library_policy": "MULTITARGET_UNTIL_LAST_CONSUMER",
        "phases": phases,
        "groups": groups,
        "shared_projects": shared,
        "central_package_strategies": strategies,
        "test_associations": tests,
        "blockers": blockers,
        "uncertainties": uncertainties,
        "final_validation_plan": _final_validation(inventory, grouping),
    }


def _proposed_changes(g: dict, inventory: dict, g_findings: list[dict], grouping: dict) -> list[dict]:
    projects = {p["id"]: p for p in inventory["projects"]}
    out = []
    for pid in g["project_ids"] + g["test_project_ids"]:
        tfms = projects[pid]["target_frameworks"]
        if g["group_kind"] == "SHARED_FOUNDATION":
            new = next((s["temporary_tfms"] for s in grouping["shared_projects"] if s["project_id"] == pid), tfms)
        else:
            new = [t.replace("net8.0", "net10.0", 1) if t.startswith("net8.0") else t for t in tfms]
        if new != tfms:
            out.append({"file": pid, "change_class": "PROJECT_LOCAL", "kind": "target_framework",
                        "from": tfms, "to": new})
    seen = set()
    for f in g_findings:
        rep = f.get("repair") or {}
        if rep and (f["rule_id"], rep.get("id")) not in seen:
            seen.add((f["rule_id"], rep.get("id")))
            out.append({"file": f.get("file") or ", ".join(f.get("projects") or []), "change_class": rep.get("change_class"),
                        "kind": "review" if rep.get("action", "").startswith("Check:") else "recipe_repair",
                        "rule_id": f["rule_id"], "repair_id": rep.get("id"),
                        "action": rep.get("action"),
                        "allowed_in_group_run": rep.get("change_class") != "REPOSITORY_GLOBAL"})
    return out


def _phases(recipe: dict, inventory: dict, compat: dict, grouping: dict, groups: list[dict],
            preflight: dict, baseline: dict | None) -> list[dict]:
    gj = [f for f in compat["findings"] if f["rule_id"] == "GLOBAL-JSON-SDK"]
    ci = [f for f in compat["findings"] if f["rule_id"] == "FILE-CI-SDK8"]
    central = compat["central_package_strategies"]
    phases = []
    for p in recipe["phases"]:
        items, status = [], "REQUIRED"
        pid = p["id"]
        if pid == "PHASE_0_ANALYSIS":
            status = "DONE"
        elif pid == "PHASE_1_SDK_GLOBAL":
            items += [{"change": f["title"], "change_class": "REPOSITORY_GLOBAL", "files": [f["file"]],
                       "needed": f["effect"] != "none"} for f in gj]
            items += [{"change": f["title"], "change_class": "REPOSITORY_GLOBAL", "files": [f["file"]],
                       "needed": True, "action": f["repair"]["action"]} for f in ci]
            if not gj:
                items.append({"change": "No global.json: pin a .NET 10 SDK with an explicit rollForward policy",
                              "change_class": "REPOSITORY_GLOBAL", "files": ["global.json"], "needed": True})
            status = "REQUIRED" if any(i["needed"] for i in items) else "ALREADY_SATISFIED"
        elif pid == "PHASE_2_SDK_BASELINE":
            ok = baseline is not None and baseline["build_succeeded"]
            status = "DONE" if ok else ("FAILED" if baseline else "NOT_EXECUTED")
            items.append({"change": "Build and test the unchanged net8.0 repository with the .NET 10 SDK",
                          "change_class": "REPOSITORY_GLOBAL", "result": "build succeeded" if ok else "see baseline.json"})
        elif pid == "PHASE_3_MULTITARGET_SHARED":
            for s in grouping["shared_projects"]:
                items.append({"change": f"{s['name']}: {';'.join(s['current_tfms'])} → {';'.join(s['temporary_tfms'])}",
                              "change_class": "GROUP_SHARED", "files": [s["project_id"]],
                              "group_id": "shared-foundation"})
            status = "REQUIRED" if items else "NOT_NEEDED"
        elif pid == "PHASE_4_GROUP_MIGRATION":
            for g in groups:
                if g["group_kind"] != "SHARED_FOUNDATION":
                    items.append({"change": f"Migrate {g['group_id']}", "group_id": g["group_id"],
                                  "order": g["order"], "readiness": g["readiness"],
                                  "change_class": "GROUP_SHARED", "depends_on": g["depends_on_group_ids"]})
        elif pid == "PHASE_5_RETIRE_NET8":
            for s in grouping["shared_projects"]:
                items.append({"change": f"Remove net8.0 from {s['name']}", "change_class": "GROUP_SHARED",
                              "condition": s["retirement_condition_for_net8"], "files": [s["project_id"]]})
            status = "REQUIRED" if items else "NOT_NEEDED"
        elif pid == "PHASE_6_CONVERGE":
            for c in central:
                items.append({"change": f"{c['package']}: converge on {c['net10_version'] or 'a 10.x version (unknown: lookup failed)'} once no net8.0 target uses it",
                              "change_class": c["change_class"], "strategy_until_then": c["strategy"]})
            items.append({"change": "Remove temporary TFM conditions from Directory.Packages.props and project files",
                          "change_class": "REPOSITORY_GLOBAL"})
        elif pid == "PHASE_7_REPO_VALIDATION":
            items.append({"change": "Full restore/build/test of every entry point on net10.0; compare with baseline",
                          "change_class": "REPOSITORY_GLOBAL"})
        elif pid == "PHASE_8_CUTOVER":
            items.append({"change": "Drop the .NET 8 runtime from CI images; keep global.json on the approved SDK 10 band",
                          "change_class": "REPOSITORY_GLOBAL"})
        phases.append({"id": pid, "scope": p["scope"], "description": p["description"], "status": status, "items": items})
    return phases


def _final_validation(inventory: dict, grouping: dict) -> list[str]:
    entry = [s["path"] for s in inventory["solutions"] if s["path"].endswith((".sln", ".slnx"))]
    return [
        "Each group run returns a patch; batches are combined in plan order on one integration branch.",
        f"After each combined batch: restore/build/test {', '.join(entry) or 'every project'}; "
        "shared libraries are tested for every temporary TFM.",
        "Baseline failures stay separated from new failures; a new failure blocks the next batch.",
        "When the last net8.0 consumer of a shared library migrates, PHASE_5 removes net8.0 and the full suite runs again.",
        "PHASE_7 runs the full repository on net10.0 only (Windows projects on a Windows runner), then PHASE_8 updates CI.",
    ]
