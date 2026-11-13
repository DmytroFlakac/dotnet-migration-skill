"""Implementation mode for ONE approved group.

Everything happens in a disposable git worktree outside the repository. The result is a patch
and a review package; the repository itself is never modified, committed or pushed. If the final
gate fails, the worktree is discarded and no patch is returned (that is the rollback).

Flow: validate inputs → group baseline → apply recipe changes (TFMs, required packages, project
post-actions) → restore/build/test → classify failures → at most N narrow recipe repairs →
compare with baseline (per TFM) → validate dependents → final gate → patch or rollback.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

from . import edits, SCHEMA_VERSION, TARGET_TFM
from . import cache
from .buildtest import SCOPE_FILTER, run_build_and_test, write_scope_filter
from .cache import fingerprint, head_tree
from .compat import load_recipe, source_and_file_findings
from .discovery import run_inventory
from .dotnet import Runner
from .secrets import added_lines, find_secrets
from .catalog import load_catalog
from .experiment import DOWNGRADE, raise_downgrade_pins, settle_regressions, compare_tests, new_diagnostics, snapshot
from .drafts import write_drafts
from .graph import ProjectGraph

PROP_TF = re.compile(r"<TargetFramework>([^<]*)</TargetFramework>")
PROP_TFS = re.compile(r"<TargetFrameworks>([^<]*)</TargetFrameworks>")
GLOBAL_NAMES = {"global.json", "directory.build.props", "directory.build.targets",
                "directory.packages.props", "nuget.config"}


def _progress(step: str) -> None:
    print(f"[dmig {time.strftime('%H:%M:%S')}] {step}", file=sys.stderr, flush=True)


class GateFailure(Exception):
    pass


# --------------------------------------------------------------------------- file edits
def set_target_frameworks(path: Path, new_tfms: list[str]) -> dict:
    """Write the project's own TFM(s). Uses TargetFramework for one value, TargetFrameworks for
    several, and clears an inherited singular TargetFramework (which would otherwise win)."""
    text = path.read_text(encoding="utf-8")
    value = ";".join(new_tfms)
    before = PROP_TFS.search(text) or PROP_TF.search(text)
    if len(new_tfms) == 1:
        if PROP_TFS.search(text):
            new = PROP_TFS.sub(f"<TargetFramework>{value}</TargetFramework>", text, count=1)
        elif PROP_TF.search(text):
            new = PROP_TF.sub(f"<TargetFramework>{value}</TargetFramework>", text, count=1)
        else:
            new = _insert_property(text, f"<TargetFramework>{value}</TargetFramework>")
    else:
        if PROP_TFS.search(text):
            new = PROP_TFS.sub(f"<TargetFrameworks>{value}</TargetFrameworks>", text, count=1)
            new = PROP_TF.sub("", new)
        elif PROP_TF.search(text):
            new = PROP_TF.sub(f"<TargetFrameworks>{value}</TargetFrameworks>", text, count=1)
        else:  # inherited from a shared file: clear the singular and set the plural locally
            new = _insert_property(text, f"<TargetFramework></TargetFramework>\n    "
                                         f"<TargetFrameworks>{value}</TargetFrameworks>")
    edits.write_like(path, new, edits.read(path))
    return {"before": before.group(1) if before else "(inherited)", "after": value}


def _insert_property(text: str, xml: str) -> str:
    m = re.search(r"<PropertyGroup[^>]*>", text)
    if m:
        return text[: m.end()] + "\n    " + xml + text[m.end():]
    m = re.search(r"<Project[^>]*>", text)
    return text[: m.end()] + f"\n  <PropertyGroup>\n    {xml}\n  </PropertyGroup>" + text[m.end():]


def repair_cs14_field(worktree: Path, warnings: list[dict]) -> list[str]:
    """CS9258: inside the flagged accessor lines, refer to the member explicitly as this.field."""
    changed = []
    by_file: dict[str, set[int]] = {}
    for w in warnings:
        if w["code"] == "CS9258" and w["file"] and w["line"]:
            by_file.setdefault(w["file"], set()).add(w["line"])
    for rel, lines in by_file.items():
        path = worktree / rel
        src = path.read_text(encoding="utf-8").split("\n")
        for ln in sorted(lines):
            src[ln - 1] = re.sub(r"(?<![\w.@])field(?!\w)", "this.field", src[ln - 1])
        edits.write_like(path, "\n".join(src), edits.read(path))
        changed.append(rel)
    return changed


def repair_remove_package(worktree: Path, project_rel: str, package: str) -> list[str]:
    """Drop a package for net10.0: remove it when the project is net10.0-only, else condition it on net8.0."""
    path = worktree / project_rel
    text = path.read_text(encoding="utf-8")
    tfms = (PROP_TFS.search(text) or PROP_TF.search(text))
    tfms = [t.strip() for t in (tfms.group(1) if tfms else "").split(";") if t.strip()]
    rx = re.compile(r'[ \t]*<PackageReference\s+Include="' + re.escape(package) + r'"([^>]*?)/>[ \t]*\r?\n?', re.I)
    if not rx.search(text):
        return []
    if any(t.startswith("net8.0") for t in tfms):
        # Keep it for net8.0 exactly as before. In a project that also targets net10.0 the SDK applies
        # package pruning to every target and still raises NU1510 for the net8.0 reference (seen on a
        # real repository), so silence that one warning on that one reference; PHASE_5 removes it.
        def keep_for_net8(m: re.Match) -> str:
            attrs = m.group(1).rstrip()
            nowarn = "" if "NoWarn" in attrs else ' NoWarn="NU1510"'
            return (f'    <PackageReference Include="{package}"{attrs} '
                    f'Condition="$(TargetFramework.StartsWith(\'net8.0\'))"{nowarn} />\n')
        new = rx.sub(keep_for_net8, text, count=1)
    else:
        new = rx.sub("", text, count=1)
        new = re.sub(r"[ \t]*<ItemGroup>\s*</ItemGroup>[ \t]*\r?\n?", "", new)  # drop a now-empty group
    edits.write_like(path, new, edits.read(path))
    return [project_rel]


def update_docker_image(worktree: Path, rel: str) -> list[str]:
    path = worktree / rel
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"(mcr\.microsoft\.com/dotnet/(?:aspnet|runtime|runtime-deps|sdk)):8\.0", r"\1:10.0", text)
    if new != text:
        edits.write_like(path, new, edits.read(path))
        return [rel]
    return []


# --------------------------------------------------------------------------- helpers
def _is_global(rel: str, inventory: dict) -> bool:
    if rel.lower() in GLOBAL_NAMES or "/" not in rel and rel.lower().endswith((".props", ".targets")):
        return True
    return rel in inventory["ci_entrypoints"]


def _project_dirs(pids: list[str]) -> list[str]:
    return sorted({p.rsplit("/", 1)[0] + "/" for p in pids if "/" in p})


def _changed_files(worktree: Path, runner: Runner) -> list[str]:
    st = runner.git(["status", "--porcelain=v1", "--untracked-files=all"], worktree, "impl-status")
    return sorted(line[3:].strip().strip('"') for line in st.output.splitlines() if line.strip())


def _problems(group_baseline: dict, current: dict, scope: set[str], settle=None) -> dict:
    diag = new_diagnostics(group_baseline, current)
    tests = compare_tests(group_baseline, current)
    if settle:  # re-run regressed tests once: flaky ones do not block
        tests = settle(tests)
    return {
        "restore_failed": not current["restore_succeeded"],
        "new_errors": [d for d in diag["new_errors"] if d.get("project") in scope or not d.get("project")],
        "new_warnings": [d for d in diag["new_warnings"] if d.get("project") in scope],
        "regressions": [r for r in tests["regressions"] if r["project"] in scope],
        "baseline_failures_still_failing": tests["baseline_failures_still_failing"],
        "not_run": [r for r in tests["not_run_in_experiment"] if r["project"] in scope],
        "flaky": [r for r in tests.get("flaky", []) if r["project"] in scope],
        "environment_unstable": [r for r in tests.get("environment_unstable", []) if r["project"] in scope],
        "test_comparison": tests,
    }


# --------------------------------------------------------------------------- main
def run_implementation(repo: Path, out: Path, runner: Runner, plan: dict, request: dict,
                       recipes_dir: Path, preflight: dict | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    gid = request["selected_group_id"]
    max_attempts = min(int(request.get("max_repair_attempts", 3)), 3)
    allow_global = bool(request.get("allow_repository_global_changes", False))
    started = time.monotonic()
    result: dict = {"schema_version": SCHEMA_VERSION, "mode": "implementation", "group_id": gid,
                    "plan_id": plan.get("plan_id"), "target_framework": TARGET_TFM, "recipe_ids": [plan.get("recipe_id")],
                    "status": "STARTED", "files_changed": [], "transformations": [], "repair_attempts": [],
                    "unresolved_items": [], "excluded_work": [], "proposed_global_changes": [],
                    "rollback": {"performed": False, "verified": False}, "ready_for_human_review": False}

    def finish(status: str, **extra) -> dict:
        result.update(status=status, duration_s=round(time.monotonic() - started, 1), **extra)
        return result

    group = next((g for g in plan["groups"] if g["group_id"] == gid), None)
    if group is None:
        return finish("REJECTED", unresolved_items=[f"group {gid} is not in plan {plan.get('plan_id')}"])
    if request.get("global_plan_id") and request["global_plan_id"] != plan.get("plan_id"):
        return finish("REJECTED", unresolved_items=["global_plan_id does not match the plan"])

    # 1. the analysis must still describe this commit
    before = snapshot(repo, runner)
    if before["commit"] != plan["repository_commit"] and request.get("allow_newer_commit"):
        result["unresolved_items"].append(f"Plan {plan['repository_commit'][:10]} used on newer commit "
                                          f"{before['commit'][:10]} (allow_newer_commit): projects are re-read from the repository.")
    elif before["commit"] != plan["repository_commit"]:
        return finish("REJECTED", unresolved_items=[
            f"Repository is at {before['commit']} but the plan was made at {plan['repository_commit']}; rerun the analysis."])

    worktree = out / "worktree"
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    add = runner.git(["worktree", "add", "--detach", str(worktree), "HEAD"], repo, "impl-worktree-add")
    if not add.ok:
        return finish("REJECTED", unresolved_items=["git worktree add failed: " + add.tail(3)])
    patch_path = None
    try:
        _progress("worktree created; reading projects")
        inventory = run_inventory(worktree, runner, before["commit"])
        projects = {p["id"]: p for p in inventory["projects"]}
        graph = ProjectGraph(inventory["projects"])
        members = group["project_ids"] + group["test_project_ids"]
        missing = [m for m in members if m not in projects]
        if missing:
            return finish("REJECTED", unresolved_items=[f"planned project no longer exists: {m}" for m in missing])

        # dependency order: groups this one depends on must already offer net10.0
        not_ready = []
        for dep in group["depends_on_group_ids"]:
            dep_group = next(g for g in plan["groups"] if g["group_id"] == dep)
            for pid in dep_group["project_ids"]:
                if not any(t.startswith("net10.0") for t in projects.get(pid, {}).get("target_frameworks", [])):
                    not_ready.append(f"{dep}: {pid} has no net10.0 target yet")
        if not_ready and not request.get("allow_out_of_order"):
            return finish("BLOCKED_DEPENDENCY_ORDER", unresolved_items=not_ready)

        # 2. group baseline. Scope = the group, everything that consumes it, and their tests; built
        #    through a solution filter so big monorepos only build what this group can affect.
        caps = (preflight or {}).get("capabilities", {"can_test_net8": True, "can_test_net10": True})
        scope = set(members) | {c for m in group["project_ids"] for c in graph.all_consumers(m)}
        entry = write_scope_filter(worktree, inventory, scope) if request.get("scoped_build", True) else None
        result["build_scope"] = {"projects": sorted(scope), "filter": SCOPE_FILTER if entry else None,
                                 "entrypoints": entry or "all"}
        fp = fingerprint(runner, preflight)
        cache_dir = Path(request["baseline_cache_dir"]) if request.get("baseline_cache_dir") else None
        tree = head_tree(worktree, runner)
        base, reason = (None, "cache disabled") if request.get("no_cache") else cache.load(cache_dir, tree, fp, scope)
        if base is None:
            _progress(f"group baseline build/test (slow; {reason})")
            base = run_build_and_test(worktree, out, "group-baseline", runner, inventory, caps,
                                      entrypoints=entry, scope=scope if entry else None)
            cache.save(cache_dir, tree, fp, base, scope if entry else None)
            reason = "built (" + reason + ")"
        else:
            _progress(reason)
        result["baseline"] = _bt_summary(base) | {"source": reason, "tree": tree}

        # 3–4. recipe and applicability
        recipe = load_recipe(recipes_dir)
        # The catalogue the analysis used (a organisation mirror decides which versions are approved).
        catalog = load_catalog(request.get("package_catalog"), out / "nuget-cache", online=False)             if request.get("package_catalog") not in (None, "public") else None
        is_shared = group["group_kind"] == "SHARED_FOUNDATION"
        applicable = [m for m in members if any(t.startswith("net8.0") for t in projects[m]["target_frameworks"])]
        if not applicable:
            return finish("SKIPPED_NOT_APPLICABLE", unresolved_items=["no project in the group targets net8.0"])

        # 5. SDK constraints: repository-global; never edited by a group run
        sdk = (preflight or {}).get("selected_sdk_in_repository")
        if sdk and not sdk.startswith("10."):
            result["proposed_global_changes"].append({"file": "global.json", "change": "move to a .NET 10 SDK (PHASE_1)"})
            if not allow_global:
                return finish("BLOCKED_GLOBAL_PHASE_REQUIRED",
                              unresolved_items=["The repository selects SDK " + sdk + "; PHASE_1 must run first."])

        # 6. target frameworks
        for pid in applicable:
            cur = projects[pid]["target_frameworks"]
            if is_shared:
                new = list(dict.fromkeys(t for x in cur for t in (x, x.replace("net8.0", "net10.0", 1) if x.startswith("net8.0") else x)))
            else:
                new = [t.replace("net8.0", "net10.0", 1) if t.startswith("net8.0") else t for t in cur]
            change = set_target_frameworks(worktree / pid, new)
            result["transformations"].append({"kind": "target_framework", "file": pid, "change_class": "PROJECT_LOCAL", **change})

        # 7. build tooling: nothing required for 8 -> 10 by this recipe
        result["transformations"].append({"kind": "build_tooling", "file": None, "change_class": None,
                                          "note": "No build-tool update is required by the dotnet-8-to-10 recipe."})
        # 8. packages: only migration-required ones (applied as repairs when the build proves them needed);
        #    framework-package alignment is PHASE_6 (global) and recorded, not applied.
        for p in applicable:
            for pk in projects[p]["packages"]:
                if any(pk["id"].startswith(x) for x in recipe["transformations"]["packages"]["coordinated_framework_prefixes"]):
                    result["excluded_work"].append(f"{pk['id']} {pk['version']} alignment deferred to PHASE_6 (repository-global)")
        # 9. Roslyn code fixes: none configured in the recipe
        # 10. post-actions that are PROJECT_LOCAL for this group (container images); CI is global
        dirs = _project_dirs(group["project_ids"])
        for rel in inventory["files"]["dockerfiles"]:
            if any(rel.startswith(d) for d in dirs) and not is_shared:
                for f in update_docker_image(worktree, rel):
                    result["transformations"].append({"kind": "post_action:update-container-base-images", "file": f,
                                                      "change_class": "PROJECT_LOCAL", "before": "8.0", "after": "10.0"})
        for rel in inventory["ci_entrypoints"]:  # look at the file as it is now, not at the plan
            ci_text = (worktree / rel).read_text(encoding="utf-8", errors="replace")
            if re.search(r"mcr\.microsoft\.com/dotnet/sdk:8\.0|dotnet-version:\s*['\"]?8\.0", ci_text):
                result["proposed_global_changes"].append(
                    {"file": rel, "change": "CI SDK image to .NET 10 (+ .NET 8 runtime while net8.0 targets remain): PHASE_1"})

        # 11–14. build/test, classify, bounded repairs
        new_inv = run_inventory(worktree, runner, before["commit"])
        scoped = {"entrypoints": entry, "scope": scope if entry else None}
        _progress("changes applied; build/test on net10.0 (slow)")
        current = run_build_and_test(worktree, out, "group-attempt-0", runner, new_inv, caps, **scoped)

        def settle(label: str):
            return lambda tests: settle_regressions(tests, worktree, out, label, runner)
        probs = _problems(base, current, scope, settle("group-attempt-0"))
        rules = recipe["rules"]
        attempt = 0
        # A failed restore is repairable only when every error has a recipe rule (e.g. NU1510);
        # _select_repairs reports any other restore error as an environment problem and stops.
        while (probs["new_errors"] or probs["regressions"]) and attempt < max_attempts:
            attempt += 1
            actions, unknown = _select_repairs(worktree, probs, current, rules, recipe, new_inv, graph, catalog)
            record = {"attempt": attempt, "failures": _failure_summary(probs), "selected_rules": [a["rule_id"] for a in actions],
                      "unknown_failures": unknown, "files_changed": [], "validation_result": None, "next_action": None}
            if unknown or not actions:
                record.update(validation_result="not_attempted", next_action="stop_for_human_review")
                result["repair_attempts"].append(record)
                if unknown:  # turn each unexplained failure into a rule draft for a person to complete
                    result["rule_drafts_path"] = write_drafts(out, unknown, recipe)
                break
            _progress(f"repair attempt {attempt}: {', '.join(a['rule_id'] for a in actions)}")
            for a in actions:
                record["files_changed"] += a["apply"]()
            new_inv = run_inventory(worktree, runner, before["commit"])
            current = run_build_and_test(worktree, out, f"group-attempt-{attempt}", runner, new_inv, caps, **scoped)
            probs = _problems(base, current, scope, settle(f"group-attempt-{attempt}"))
            ok = not (probs["new_errors"] or probs["regressions"] or probs["restore_failed"])
            record.update(validation_result="passed" if ok else "failed",
                          next_action="final_gate" if ok else ("retry" if attempt < max_attempts else "rollback"))
            result["repair_attempts"].append(record)

        # 15–16. final gate (the scope filter is a tool file, never part of the change)
        (worktree / SCOPE_FILTER).unlink(missing_ok=True)
        changed = _changed_files(worktree, runner)
        result["files_changed"] = changed
        if probs["environment_unstable"]:
            result["unresolved_items"].append(
                f"{len(probs['environment_unstable'])} test(s) NOT VALIDATED: failed twice with a timeout, deadlock or "
                "lost connection (test environment unstable), not counted as passed: "
                + ", ".join(r["test"] for r in probs["environment_unstable"][:10]))
        if probs["flaky"]:
            result["unresolved_items"].append(
                f"{len(probs['flaky'])} test(s) failed once and passed on a re-run (flaky, not blocking): "
                + ", ".join(r["test"] for r in probs["flaky"][:10]))
        result["final_validation"] = _bt_summary(current) | {
            "flaky_tests": probs["flaky"], "environment_unstable_tests": probs["environment_unstable"],
            "new_errors": probs["new_errors"], "regressions": probs["regressions"],
            "baseline_failures_still_failing": probs["baseline_failures_still_failing"],
            "tests_not_run": probs["not_run"]}
        # New warnings on net10.0 that a catalogue rule explains (obsolete APIs, …): not failures, but the
        # person should see them with the Microsoft page that says what to use instead.
        diag_rules = [r for r in rules if r["kind"] == "diagnostic"]
        review = []
        for w in probs["new_warnings"]:
            rule = next((r for r in diag_rules if w["code"] in r.get("expected_diagnostics", [])), None) or next(
                (r for r in diag_rules if any(w["code"].startswith(p) for p in r.get("expected_diagnostic_prefixes", []))), None)
            if rule:
                review.append({"code": w["code"], "file": w["file"], "line": w["line"], "rule_id": rule["id"],
                                "reference": rule.get("reference"), "message": w["message"][:200]})
        result["warnings_to_review"] = review[:50]
        gate = _final_gate(group, changed, probs, current, inventory, allow_global, attempt, max_attempts, is_shared, new_inv)
        result["final_gate"] = gate
        if all(c["passed"] for c in gate):
            # The final state becomes the next group's cached baseline once a person applies the patch
            # (same tree hash). Staging uses the repository's own line-ending settings, like their commit.
            result_tree = cache.working_tree_hash(worktree, runner)
            result["result_tree"] = result_tree
            cache.save(cache_dir, result_tree, fp, current, scope if entry else None)
            # The repository's own line-ending settings (core.autocrlf), as for its commits: forcing them off
            # compares a CRLF checkout with LF blobs and turns every line into a change.
            quiet = ["-c", "core.safecrlf=false"]
            patch = runner.git(quiet + ["diff", "--binary", "HEAD"], worktree, "impl-diff")
            if not patch.ok or not patch.stdout.startswith("diff --git"):
                raise GateFailure("could not produce a clean patch: " + patch.tail(3))
            leaks = find_secrets(added_lines(patch.stdout))
            if leaks:  # never hand over a patch that would add a credential to the repository
                raise GateFailure("the patch would add something that looks like a credential: " + ", ".join(leaks))
            patch_path = out / f"{gid}.patch"
            patch_path.write_text(patch.stdout, encoding="utf-8", newline="")
            result.update(patch_path=str(patch_path), ready_for_human_review=True)
            result["unresolved_items"] += [f"baseline failure kept: {r['test']} ({r.get('tfm')})"
                                           for r in probs["baseline_failures_still_failing"]]
            return finish("SUCCESS")
        # 17. rollback: discard the worktree, return evidence but no patch
        result["unresolved_items"] += [f"{c['check']}: {c['detail']}" for c in gate if not c["passed"]]
        result["rollback"]["performed"] = True
        return finish("ROLLED_BACK")
    finally:
        runner.git(["worktree", "remove", "--force", str(worktree)], repo, "impl-worktree-remove")
        runner.git(["worktree", "prune"], repo, "impl-worktree-prune")
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        after = snapshot(repo, runner)
        result["rollback"]["verified"] = (before["commit"] == after["commit"]
                                          and before["status_sha256"] == after["status_sha256"]
                                          and before["worktree_count"] == after["worktree_count"]
                                          and not worktree.exists())
        result["changes_persisted"] = False
        result["rollback_verified"] = result["rollback"]["verified"]  # same name as in analysis mode
        _progress(f"done: {result['status']}")
        problems = _validate_result(result, recipes_dir.parent / "schemas" / "result.schema.json")
        if problems:  # a result that breaks its contract is never ready for review
            result["schema_problems"], result["ready_for_human_review"] = problems, False
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _validate_result(result: dict, schema_path: Path) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    if not schema_path.exists():
        return [f"schema missing: {schema_path}"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return [f"{'/'.join(map(str, e.path))}: {e.message}"
            for e in jsonschema.Draft202012Validator(schema).iter_errors(json.loads(json.dumps(result)))]


def _bt_summary(bt: dict) -> dict:
    return {"restore_succeeded": bt["restore_succeeded"], "build_succeeded": bt["build_succeeded"],
            "tests": {k: bt["tests"][k] for k in ("total", "passed", "failed", "skipped")},
            "errors": len(bt["errors"]), "warnings": len(bt["warnings"]), "duration_s": bt["duration_s"],
            "environment_blocked_projects": len(bt.get("environmental_blocks", []))}


def _failure_summary(probs: dict) -> list[dict]:
    return ([{"failure_class": e["failure_class"], "code": e["code"], "file": e["file"], "line": e["line"],
              "baseline_status": "new"} for e in probs["new_errors"]]
            + [{"failure_class": "test_regression", "test": r["test"], "tfm": r.get("tfm"), "baseline_status": "new"}
               for r in probs["regressions"]])


def _select_repairs(worktree: Path, probs: dict, current: dict, rules: list[dict], recipe: dict,
                    inventory: dict, graph: ProjectGraph, catalog=None) -> tuple[list[dict], list[dict]]:
    """exact recipe rule → narrow deterministic repair; anything else → stop."""
    actions, unknown, chosen = [], [], set()
    projects = {p["id"]: p for p in inventory["projects"]}
    # NU1605 (PKG-DOWNGRADE-PIN): a pin in one of this run's project files is lower than what a
    # .NET 10-era dependency needs. Raise it to the version the error names, in project files only
    # (never a repository-global file such as Directory.Packages.props: that is a global phase).
    downgrades = [e for e in probs["new_errors"] if e["code"] == "NU1605" and DOWNGRADE.search(e.get("message", ""))]
    if catalog is not None:  # never raise a pin to a version the configured catalogue does not approve
        refused = [e for e in downgrades
                   if not catalog.is_approved(DOWNGRADE.search(e["message"]).group(1), DOWNGRADE.search(e["message"]).group(2))]
        for e in refused:
            m = DOWNGRADE.search(e["message"])
            unknown.append({**_short(e), "reason": f"{m.group(1)} {m.group(2)} is not approved in the package catalogue: "
                                                   "stop, do not choose another version"})
        downgrades = [e for e in downgrades if e not in refused]
    if downgrades:
        own_files = {pid for pid in projects}
        actions.append({"rule_id": "PKG-DOWNGRADE-PIN",
                        "apply": lambda d=downgrades, own=own_files: [f["file"] for f in raise_downgrade_pins(worktree, d, own)]})
    for err in probs["new_errors"]:
        if err in downgrades or any(u.get("code") == "NU1605" and u.get("project") == err.get("project") for u in unknown):
            continue
        proj = projects.get(err.get("project") or "", {})
        refs = {pk["id"].lower(): pk["id"] for pk in proj.get("packages", [])}
        rule, package = None, None
        for r in rules:  # a package rule matches by diagnostic code AND a package the project really references
            if r["kind"] != "package" or err["code"] not in r.get("expected_diagnostics", []):
                continue
            candidates = [r["package"]] if r.get("package") else [p for p in r.get("packages_any", [])
                                                                    if p.lower() in err["message"].lower()]
            hit = next((refs[c.lower()] for c in candidates if c.lower() in refs), None)
            if hit:
                rule, package = r, hit
                break
        if rule:
            key = (rule["id"], proj["id"], package)
            if key not in chosen:
                chosen.add(key)
                actions.append({"rule_id": rule["id"],
                                "apply": lambda p=proj["id"], pkg=package: repair_remove_package(worktree, p, pkg)})
        elif err["failure_class"] in {"sdk_not_found", "workload_missing", "package_restore_failure",
                                      "unavailable_package_version", "platform_incompatibility"}:
            unknown.append({**_short(err), "reason": "environment or restore problem: stop, do not alter source"})
        else:
            unknown.append({**_short(err), "reason": "no recipe rule for this diagnostic"})
    if probs["regressions"]:
        findings = source_and_file_findings(worktree, inventory, recipe)
        silent = [f for f in findings if f.get("effect") == "silent_behavior_change"]
        for reg in probs["regressions"]:
            refs = projects.get(reg["project"], {}).get("project_references", [])
            validated = set().union(*(graph.closure(r) for r in refs)) if refs else set()
            cause = next((f for f in silent if set(f["projects"]) & validated), None)
            if cause and cause["rule_id"] == "SRC-CS14-FIELD":
                warnings = [w for w in current["warnings"] if w["code"] == "CS9258" and w.get("project") in cause["projects"]]
                if warnings and cause["rule_id"] not in chosen:
                    chosen.add(cause["rule_id"])
                    actions.append({"rule_id": cause["rule_id"], "apply": lambda w=warnings: repair_cs14_field(worktree, w)})
                elif not warnings:
                    unknown.append({"test": reg["test"], "reason": "suspected C# 14 field change but no CS9258 location"})
            elif not cause:
                unknown.append({"test": reg["test"], "tfm": reg.get("tfm"), "reason": "regression with no recipe rule"})
    return actions, unknown


def _short(d: dict) -> dict:
    return {k: d.get(k) for k in ("code", "file", "line", "project", "failure_class")}


def _final_gate(group: dict, changed: list[str], probs: dict, current: dict, inventory: dict, allow_global: bool,
                attempts: int, max_attempts: int, is_shared: bool, new_inv: dict) -> list[dict]:
    dirs = _project_dirs(group["project_ids"] + group["test_project_ids"])
    out_of_scope = [f for f in changed if not any(f.startswith(d) for d in dirs)]
    global_changed = [f for f in changed if _is_global(f, inventory)]
    planned = group["project_ids"] + group["test_project_ids"]
    projects = {p["id"]: p for p in new_inv["projects"]}
    not_migrated = [p for p in planned if not any(t.startswith("net10.0") for t in projects[p]["target_frameworks"])]
    built = {b["output"] for b in current["projects_built"]}
    checks = [
        {"check": "restore_succeeds", "passed": current["restore_succeeded"], "detail": "restore of every entry point"},
        {"check": "build_succeeds", "passed": current["build_succeeded"] and not probs["new_errors"],
         "detail": f"{len(probs['new_errors'])} new error(s)"},
        {"check": "no_migration_caused_test_failures", "passed": not probs["regressions"],
         "detail": ", ".join(f"{r['test']} ({r.get('tfm')})" for r in probs["regressions"]) or "none"},
        {"check": "baseline_failures_separated", "passed": True,
         "detail": f"{len(probs['baseline_failures_still_failing'])} pre-existing failure result(s) kept apart"},
        {"check": "no_planned_project_silently_excluded", "passed": not not_migrated,
         "detail": ", ".join(not_migrated) or "all planned projects target net10.0"},
        {"check": "diff_within_group_scope", "passed": not out_of_scope,
         "detail": ", ".join(out_of_scope) or "only files inside the group's project folders changed"},
        {"check": "no_repository_global_change", "passed": allow_global or not global_changed,
         "detail": ", ".join(global_changed) or "none"},
        {"check": "repair_budget_respected", "passed": attempts <= max_attempts, "detail": f"{attempts}/{max_attempts}"},
        {"check": "dependent_tests_ran", "passed": not probs["not_run"],
         "detail": f"{len(probs['not_run'])} test(s) in scope did not run"},
    ]
    if is_shared:
        both = all(any(f"/{t}/" in b for b in built) for p in group["project_ids"]
                   for t in projects[p]["target_frameworks"])
        checks.append({"check": "every_temporary_tfm_compiles", "passed": both,
                       "detail": "net8.0 and net10.0 outputs found for every shared library" if both else "missing an output"})
    return checks
