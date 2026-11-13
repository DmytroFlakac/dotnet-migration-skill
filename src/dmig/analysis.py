"""Analysis mode: preflight → A1…A10. No permanent change, no delivery action."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .console import progress
from . import SCHEMA_VERSION, SOURCE_TFM, TARGET_TFM
from .buildtest import run_build_and_test
from .catalog import load_catalog, public_nuget_allowed
from .compat import load_recipe, run_compatibility
from .discovery import run_inventory
from .dotnet import Runner
from .experiment import run_experiment, snapshot
from .graph import ProjectGraph, associate_tests
from .grouping import build_groups
from .plan import build_plan
from .preflight import run_preflight
from .report import render
from . import limits

RECIPES = Path(__file__).resolve().parents[2] / "recipes"
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _write(out: Path, name: str, data) -> str:
    path = out / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return name


def validate_outputs(out: Path) -> list[str]:
    """Validate every output JSON against its schema; returns problems (empty = valid)."""
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema not installed: outputs not validated"]
    problems = []
    for name in ("preflight", "inventory", "baseline", "migration-plan", "response"):
        data_path, schema_path = out / f"{name}.json", SCHEMAS / f"{name}.schema.json"
        if not data_path.exists() or not schema_path.exists():
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        for err in validator.iter_errors(json.loads(data_path.read_text(encoding="utf-8"))):
            problems.append(f"{name}.json: {'/'.join(map(str, err.path))}: {err.message}")
    return problems


def _progress(step: str) -> None:
    progress(step)


def _sdk_probe(repo: Path, out: Path, runner: Runner, preflight: dict, start_state: dict | None) -> dict | None:
    if not start_state or not any(b["code"] == "GLOBAL_JSON_UNSATISFIED" for b in preflight["blocking_items"]):
        return None
    if not any(s["version"].startswith("10.") for s in preflight["sdks"]):
        return None
    import shutil
    from .discovery import categorize
    from .experiment import ensure_sdk
    wt = out / "worktrees" / "sdk10-probe"
    shutil.rmtree(wt, ignore_errors=True)
    if not runner.git(["worktree", "add", "--detach", str(wt), "HEAD"], repo, "probe-worktree-add").ok:
        return None
    changes = ensure_sdk(wt, {"files": categorize(wt)}, None, preflight["sdks"])
    check = runner.run(["--version"], wt, "probe-dotnet-version", 120)
    if not changes or not check.ok:
        _remove_worktree(repo, wt, runner)
        return None
    return {"worktree": wt, "pinned": changes[0]["before"], "used": check.output.strip().splitlines()[-1], "changes": changes}


def _remove_worktree(repo: Path, wt: Path, runner: Runner) -> None:
    import shutil
    runner.git(["worktree", "remove", "--force", str(wt)], repo, "worktree-remove")
    runner.git(["worktree", "prune"], repo, "worktree-prune")
    shutil.rmtree(wt, ignore_errors=True)


AUDIT_CODES = {"NU1901", "NU1902", "NU1903", "NU1904"}


def audit_drift(baseline: dict) -> list[str]:
    """Packages behind a baseline restore failure that consists only of NuGet audit errors."""
    if baseline.get("restore_succeeded", True):
        return []
    restore_errors = [e for e in baseline.get("errors", []) if str(e.get("code", "")).startswith("NU")]
    if not restore_errors or any(e["code"] not in AUDIT_CODES for e in restore_errors):
        return []
    found = set()
    for e in restore_errors:
        m = re.search(r"Package '([^']+)' ([\w.\-]+)", e.get("message", ""))
        found.add(f"{m.group(1)} {m.group(2)}" if m else e["code"])
    return sorted(found)


def _baseline_in_worktree(repo: Path, out: Path, runner: Runner, inventory: dict, caps: dict, is_git: bool) -> dict:
    """Build/test the committed code in a disposable worktree, so even ignored build output
    (bin/, obj/, TestResults/) never appears in the person's repository."""
    import shutil
    if not is_git:  # no git: build in place, and say so
        bt = run_build_and_test(repo, out, "baseline", runner, inventory, caps)
        bt["built_in"] = "repository (not a git repository: bin/ and obj/ folders were created there)"
        return bt
    wt = out / "worktrees" / "baseline"
    shutil.rmtree(wt, ignore_errors=True)
    add = runner.git(["worktree", "add", "--detach", str(wt), "HEAD"], repo, "baseline-worktree-add")
    try:
        if not add.ok:
            bt = run_build_and_test(repo, out, "baseline", runner, inventory, caps)
            bt["built_in"] = "repository (git worktree add failed)"
            return bt
        bt = run_build_and_test(wt, out, "baseline", runner, inventory, caps)
        bt["built_in"] = "disposable worktree of HEAD (committed code only)"
        bt["workdir"] = str(repo)
        return bt
    finally:
        runner.git(["worktree", "remove", "--force", str(wt)], repo, "baseline-worktree-remove")
        runner.git(["worktree", "prune"], repo, "baseline-worktree-prune")
        shutil.rmtree(wt, ignore_errors=True)


def run_analysis(repo: Path, out: Path, runner: Runner, request: dict,
                 environment_contract: dict | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    # A0 – environment preflight (mandatory)
    _progress("preflight")
    preflight = run_preflight(repo, out, runner, probe_network=request.get("probe_feeds", True),
                              environment_contract=environment_contract)
    _write(out, "preflight.json", preflight)
    caps = preflight["capabilities"]

    # A1 – clean workspace record
    start_state = snapshot(repo, runner) if preflight["disposable_workspace"]["is_git_repository"] else None

    # SDK-first probe: when global.json pins an SDK that is not installed (typically SDK 8 on a machine
    # with SDK 10), evaluate and baseline in a disposable copy whose global.json is moved to SDK 10.
    # That is exactly PHASE_1 previewed and PHASE_2 proven ("SDK 10 builds the unchanged net8.0 code"),
    # and the person's repository is untouched.
    probe = _sdk_probe(repo, out, runner, preflight, start_state)
    eval_root = probe["worktree"] if probe else repo
    if probe:
        caps = dict(caps, can_evaluate_msbuild=True, can_build=True)
        preflight.setdefault("limitations", []).append({
            "code": "SDK_PROBE_COPY", "affects": "inventory, baseline",
            "detail": f"global.json pins {probe['pinned']}, which is not installed: projects were evaluated and the "
                      f"baseline built in a disposable copy with global.json moved to SDK {probe['used']} "
                      "(PHASE_1 preview). The repository itself still pins the old SDK."})
        _write(out, "preflight.json", preflight)

    # A2/A3 – inventory and MSBuild evaluation
    _progress("inventory + MSBuild evaluation")
    inventory = run_inventory(eval_root, runner, (start_state or {}).get("commit"),
                              evaluate_msbuild=caps["can_evaluate_msbuild"])
    from . import scope as scope_mod
    adapters = scope_mod.parse(request.get("adapters"))
    inventory["adapters_enabled"] = sorted(adapters)
    inventory["out_of_scope_projects"] = scope_mod.annotate(inventory, adapters)
    sol_of = {}
    for s in inventory["solutions"]:
        for pid in s["projects"]:
            sol_of.setdefault(pid, []).append(s["path"])
    for p in inventory["projects"]:
        p["solutions"] = sol_of.get(p["id"], [])
    _write(out, "inventory.json", inventory)
    # Preflight runs before the projects are known: a missing ASP.NET Core runtime only matters when
    # the repository has web projects, so drop that note for repositories without any.
    from .compat import technologies
    if "web" not in technologies(inventory):
        before_count = len(preflight["limitations"])
        preflight["limitations"] = [l for l in preflight["limitations"] if not l["code"].startswith("ASPNETCORE_RUNTIME_")]
        if len(preflight["limitations"]) != before_count:
            _write(out, "preflight.json", preflight)

    # A4–A6 – graph, tests, groups
    _progress("graph, tests, groups")
    graph = ProjectGraph(inventory["projects"])
    tests = associate_tests(graph)
    grouping = build_groups(graph, tests)
    grouping["cycles"] = graph.cycles()

    # A7 – baseline
    _progress("baseline restore/build/test (slow)")
    baseline = None
    # Baselines never edit anything, so they run even when a feed probe failed (restore may
    # still work from the local cache); a failed restore is then recorded, not hidden.
    if request.get("run_baseline", True) and caps["can_evaluate_msbuild"] and not any(
            b["code"] == "INSUFFICIENT_DISK" for b in preflight["blocking_items"]):
        from . import cache

        def measure() -> dict:
            if probe:  # the probe copy is already disposable: build there (this is the PHASE_2 proof)
                bt = run_build_and_test(probe["worktree"], out, "baseline", runner, inventory, caps)
                bt["built_in"] = "disposable SDK-10 probe copy (global.json moved to SDK 10; projects unchanged)"
                bt["workdir"] = str(repo)
                return bt
            return _baseline_in_worktree(repo, out, runner, inventory, caps, bool(start_state))

        baseline = measure()
        drift = audit_drift(baseline)
        if drift:
            # Advisories published after the commit make the *unchanged* code fail restore when
            # warnings are errors. That is not a migration effect: measure both sides with the
            # audit off and report the packages, so the policy decision stays visible.
            runner.env["NuGetAudit"] = "false"
            preflight.setdefault("limitations", []).append({
                "code": "BASELINE_NUGET_AUDIT_DRIFT", "affects": "baseline, experiment",
                "detail": ("The unchanged code fails restore only because of known-vulnerability warnings treated "
                           f"as errors ({', '.join(drift)}). Baseline and experiment were measured with "
                           "NuGetAudit=false; the repository still needs a vulnerability/audit-policy decision "
                           "(rule SDK-NUGET-AUDIT-TRANSITIVE).")})
            _write(out, "preflight.json", preflight)
            baseline = measure()
            baseline["nuget_audit_drift"] = drift
        tree = cache.working_tree_hash(probe["worktree"], runner) if probe else cache.head_tree(repo, runner)
        _write(out, "baseline.json", baseline)
        # Seed the baseline cache so implementation runs on this exact code skip their baseline.
        cache.save(out / "baseline-cache", tree, cache.fingerprint(runner, preflight), baseline, None)
    if probe:
        _remove_worktree(repo, probe["worktree"], runner)

    # A8 – compatibility
    _progress("compatibility rules + NuGet metadata")
    recipe = scoped_recipe(load_recipe(RECIPES), set(inventory.get("adapters_enabled", [])))
    nuget = load_catalog(request.get("package_catalog"), out / "nuget-cache", allow_network=public_nuget_allowed(request),
                         runner=runner, repo=repo)
    compat = run_compatibility(repo, inventory, recipe, nuget, preflight["selected_sdk_in_repository"])
    _write(out, "compatibility.json", compat)

    # A9 – reversible experiment
    _progress("net10.0 experiment in a disposable worktree (slow)")
    experiment = None
    if request.get("allow_reversible_experiments", True) and caps["can_experiment"] \
            and preflight["disposable_workspace"]["is_git_repository"]:
        experiment = run_experiment(repo, out, runner, inventory, baseline, preflight)
        _write(out, "experiment.json", experiment)

    end_state = snapshot(repo, runner) if start_state else None
    unchanged = start_state is None or (start_state["commit"] == end_state["commit"]
                                        and start_state["status_sha256"] == end_state["status_sha256"])
    rollback_verified = unchanged and (experiment is None or experiment.get("rollback_verified", False))
    return _finish(repo, out, request, preflight, inventory, grouping, tests, compat, recipe, baseline,
                   experiment, caps, unchanged, rollback_verified)


def scoped_recipe(recipe: dict, adapters: set[str]) -> dict:
    """The recipe without the rules (and coordinated packages) of framework add-ons that are off."""
    from .scope import rule_enabled
    pk = dict(recipe["transformations"]["packages"])
    pk["coordinated_framework_prefixes"] = [x for x in pk["coordinated_framework_prefixes"]
                                            if not (x.startswith("Microsoft.AspNetCore") and "aspnet_core" not in adapters)
                                            and not (x.startswith("Microsoft.EntityFrameworkCore") and "efcore" not in adapters)]
    return {**recipe, "rules": [r for r in recipe["rules"] if rule_enabled(r, adapters)],
            "transformations": {**recipe["transformations"], "packages": pk}}


def _finish(repo: Path, out: Path, request: dict, preflight: dict, inventory: dict, grouping: dict, tests: dict,
            compat: dict, recipe: dict, baseline: dict | None, experiment: dict | None, caps: dict,
            unchanged: bool, rollback_verified: bool) -> dict:
    """A10: plan, report, drafts, Obsidian export, schema validation (no builds)."""
    # A10 – plan and report
    _progress("plan, report, schema validation")
    plan = build_plan(inventory, grouping, tests, compat, baseline, experiment, preflight, recipe)
    _write(out, "migration-plan.json", plan)
    # The contract's separate graph and group files (§8), derived from the same data.
    graph = ProjectGraph(inventory["projects"])
    _write(out, "project-graph.json", {
        "schema_version": SCHEMA_VERSION, "repository_commit": inventory["repository_commit"],
        "projects": [{"id": p["id"], "name": p["name"], "kind": p["kind"], "target_frameworks": p["target_frameworks"],
                      "platform": p["platform"], "references": p["project_references"],
                      "consumers": sorted(graph.all_consumers(p["id"])),
                      "shared": p["id"] in {s["project_id"] for s in plan["shared_projects"]}}
                     for p in inventory["projects"]],
        "edges": [{"from": p["id"], "to": r} for p in inventory["projects"] for r in p["project_references"]],
        "test_associations": tests, "cycles": grouping.get("cycles", [])})
    _write(out, "migration-groups.json", {
        "schema_version": SCHEMA_VERSION, "plan_id": plan["plan_id"],
        "groups": [{k: g[k] for k in ("group_id", "order", "group_kind", "project_ids", "test_project_ids",
                                      "shared_project_ids", "depends_on_group_ids", "platform", "readiness",
                                      "grouping_evidence", "confidence")} for g in plan["groups"]],
        "shared_libraries": [{k: s[k] for k in ("project_id", "name", "consumer_groups", "temporary_tfms",
                                                "retirement_condition_for_net8")} for s in plan["shared_projects"]]})

    if preflight["status"] == "BLOCKED" and not caps["can_evaluate_msbuild"]:
        status = "BLOCKED"
    elif plan["blockers"] or preflight["status"] != "READY" or not baseline or not experiment:
        status = "READY_FOR_PLAN_WITH_LIMITATIONS"
    else:
        status = "READY_FOR_PLAN"
    response = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "mode": "analysis",
        "source_frameworks": sorted({t for p in inventory["projects"] for t in p["target_frameworks"]}) or [SOURCE_TFM],
        "target_framework": request.get("target_framework", TARGET_TFM),
        "preflight_path": "preflight.json",
        "inventory_path": "inventory.json",
        "baseline_path": "baseline.json" if baseline else None,
        "compatibility_path": "compatibility.json",
        "experiment_path": "experiment.json" if experiment else None,
        "migration_plan_path": "migration-plan.json",
        "human_report_path": "migration-report.md",
        # Fixed-size summary: an agent reads this first, whatever the repository's size.
        "summary": {
            "projects": len(inventory["projects"]),
            "groups": len(plan["groups"]),
            "readiness_counts": {r: sum(g["readiness"] == r for g in plan["groups"])
                                 for r in sorted({g["readiness"] for g in plan["groups"]})},
            "shared_libraries": len(plan["shared_projects"]),
            "blockers": len(plan["blockers"]),
            "uncertainties": len(plan["uncertainties"]),
            "baseline_tests": (baseline or {}).get("tests", {}).get("total"),
            "baseline_failures": (baseline or {}).get("tests", {}).get("failed"),
        },
        "groups": [{"group_id": g["group_id"], "order": g["order"], "readiness": g["readiness"]}
                   for g in plan["groups"]][:limits.RESPONSE_ITEMS * 3],
        "groups_omitted": max(0, len(plan["groups"]) - limits.RESPONSE_ITEMS * 3),
        "package_catalog": dict(compat.get("package_catalog") or {}, spec=request.get("package_catalog")),
        "adapters_enabled": inventory.get("adapters_enabled", []),
        "out_of_scope_projects": inventory.get("out_of_scope_projects", [])[:limits.RESPONSE_ITEMS * 3],
        "changes_persisted": not unchanged,
        "rollback_verified": rollback_verified,
        "unresolved_items": ([b["detail"] for b in plan["blockers"]]
                             + [u["detail"] for u in plan["uncertainties"]])[:limits.RESPONSE_ITEMS],
        "unresolved_omitted": max(0, len(plan["blockers"]) + len(plan["uncertainties"]) - limits.RESPONSE_ITEMS),
        "ready_for_human_review": True,
    }
    (out / "migration-report.md").write_text(render(response, preflight, inventory, baseline, experiment, compat, plan),
                                             encoding="utf-8")
    unexplained = [dict(e, reason=f"group {g['group_id']}: no recipe rule") for g in plan["groups"]
                   for e in g["experiment"]["new_errors"] if not e.get("matched_rule")]
    unexplained += [dict(r, reason=f"group {g['group_id']}: regression with no rule") for g in plan["groups"]
                    for r in g["experiment"]["test_regressions"] if not r.get("probable_cause")]
    if unexplained:
        from .drafts import write_drafts
        response["rule_drafts_path"] = write_drafts(out, unexplained, recipe)
    from .obsidian import build_items
    from .todo import write_todo
    rules = {r["id"]: r for r in recipe["rules"]}
    todo = write_todo(out, repo, plan, build_items(plan, compat, experiment, rules, baseline))
    response["human_todo_path"] = "human-todo.md"
    response["summary"]["human_todo"] = todo["summary"]
    if request.get("obsidian", True):
        from .obsidian import export
        target = Path(request["obsidian_dir"]) if request.get("obsidian_dir") else None
        response["obsidian_path"] = export(out, repo, response, preflight, inventory, baseline, experiment, compat, plan,
                                           target_dir=target, recipe=recipe)
    _write(out, "response.json", response)
    problems = validate_outputs(out)
    if problems:
        response["schema_problems"] = problems
        response["ready_for_human_review"] = False
        _write(out, "response.json", response)
    return response


def rerender(repo: Path, out: Path, request: dict | None = None) -> dict:
    """Rebuild compatibility, test comparison, plan, report and notes from an existing analysis
    folder with the current rules and code: no restore, build or test. For new rules or a new
    note layout without a 15-minute rerun."""
    request = request or {}
    def load(name: str):
        f = out / name
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    preflight, inventory, old = load("preflight.json"), load("inventory.json"), load("response.json") or {}
    if not preflight or not inventory:
        raise SystemExit(f"{out} is not an analysis folder (preflight.json/inventory.json missing)")
    baseline, experiment = load("baseline.json"), load("experiment.json")
    from . import scope as scope_mod
    adapters = scope_mod.parse(request.get("adapters") if request.get("adapters") is not None
                               else old.get("adapters_enabled", []))
    inventory["adapters_enabled"] = sorted(adapters)
    inventory["out_of_scope_projects"] = scope_mod.annotate(inventory, adapters)
    graph = ProjectGraph(inventory["projects"])
    tests = associate_tests(graph)
    grouping = build_groups(graph, tests)
    grouping["cycles"] = graph.cycles()
    recipe = scoped_recipe(load_recipe(RECIPES), set(inventory.get("adapters_enabled", [])))
    spec = request.get("package_catalog") or (old.get("package_catalog") or {}).get("spec")
    nuget = load_catalog(spec, out / "nuget-cache", allow_network=public_nuget_allowed(request),
                         runner=Runner(None, out / "logs" / "report"), repo=repo)
    request = dict(request, package_catalog=spec)
    compat = run_compatibility(repo, inventory, recipe, nuget, preflight["selected_sdk_in_repository"])
    _write(out, "compatibility.json", compat)
    if baseline and experiment and experiment.get("build_test"):
        from .experiment import compare_tests
        experiment["test_comparison"] = compare_tests(baseline, experiment["build_test"])
        _write(out, "experiment.json", experiment)
    caps = dict(preflight.get("capabilities", {}))
    if any(l["code"] == "SDK_PROBE_COPY" for l in preflight.get("limitations", [])):
        caps.update(can_evaluate_msbuild=True, can_build=True)  # as in the original run
    return _finish(repo, out, request, preflight, inventory, grouping, tests, compat, recipe, baseline, experiment,
                   caps, not old.get("changes_persisted", False),
                   bool(old.get("rollback_verified", True)))
