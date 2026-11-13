"""Repository-global phases as separate, explicitly approved patches.

PHASE_1_SDK_GLOBAL   global.json → .NET 10 SDK band with an explicit rollForward; CI SDK image 8.0 → 10.0
                     plus the .NET 8 runtime while net8.0 targets remain (otherwise CI cannot run them).
PHASE_5_RETIRE_NET8  drop net8.0 from shared libraries (and their tests) whose consumers all left net8.0.
PHASE_6_CONVERGE     align central package versions once no net8.0 project uses the package.
PHASE_7_REPO_VALIDATION  full build/test compared with the analysis baseline; no patch.
PHASE_8_CUTOVER      remove the temporary .NET 8 runtime from CI once nothing targets net8.0.

Same safety model as group runs: disposable worktree, full-repository build/test before and after,
a final gate, then a patch for review or a rollback. Never runs without explicit approval.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

from .console import progress
from . import edits, SCHEMA_VERSION, TARGET_TFM, cache
from .buildtest import run_build_and_test
from .compat import NuGetClient, _latest_stable
from .discovery import run_inventory
from .dotnet import Runner
from .secrets import added_lines, find_secrets
from .experiment import settle_regressions, compare_tests, new_diagnostics, snapshot
from .graph import ProjectGraph
from .implement import PROP_TF, PROP_TFS, _bt_summary, _changed_files, _validate_result, set_target_frameworks

PHASES = ["PHASE_1_SDK_GLOBAL", "PHASE_5_RETIRE_NET8", "PHASE_6_CONVERGE", "PHASE_7_REPO_VALIDATION", "PHASE_8_CUTOVER"]
MARKER = "dmig PHASE_1: temporary .NET 8 runtime for net8.0 targets (removed by PHASE_8_CUTOVER)"
RUNTIME8_LINE = ("curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin --channel 8.0 "
                 "--runtime dotnet --install-dir /usr/share/dotnet")
SDK_IMAGE = re.compile(r"(mcr\.microsoft\.com/dotnet/sdk):8\.0(?=[\s\"'@-]|$)", re.M)


def _progress(step: str) -> None:
    progress(step)


class _AnyTagLoader(yaml.SafeLoader):
    """GitLab YAML uses custom tags (!reference); they must not make validation fail."""


_AnyTagLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def yaml_ok(text: str) -> str | None:
    try:
        yaml.load(text, Loader=_AnyTagLoader)
        return None
    except yaml.YAMLError as exc:
        return str(exc).splitlines()[0]


def _net8_projects(inventory: dict) -> list[str]:
    return [p["id"] for p in inventory["projects"] if any(t.startswith("net8.0") for t in p["target_frameworks"])]


# ------------------------------------------------------------------------------- PHASE_1
def phase1_edits(wt: Path, inventory: dict, sdks: list[dict]) -> tuple[list[dict], list[str]]:
    changes, follow_ups = [], []
    sdk10 = sorted((s["version"] for s in sdks if s["version"].startswith("10.")),
                   key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3]), reverse=True)
    for rel in inventory["files"]["global_json"] or ["global.json"]:
        path = wt / rel
        original = edits.read(path) if path.exists() else ""
        data = json.loads(original.lstrip(edits.BOM)) if original.strip() else {}
        sdk = data.setdefault("sdk", {})
        before = dict(sdk)
        if not str(sdk.get("version", "")).startswith("10.") and sdk10:
            major, minor, patch = (int(x) for x in re.findall(r"\d+", sdk10[0])[:3])
            sdk["version"] = f"{major}.{minor}.{patch // 100 * 100}"  # the feature band, e.g. 10.0.100
        sdk.setdefault("rollForward", "latestFeature")  # the policy must be explicit
        if sdk != before:
            edits.write_json_like(path, data, original or "{\n}\n")
            changes.append({"file": rel, "kind": "sdk_pin", "before": before, "after": dict(sdk)})
    still_net8 = bool(_net8_projects(inventory))
    for rel in inventory["ci_entrypoints"]:
        path = wt / rel
        text = path.read_text(encoding="utf-8")
        new = SDK_IMAGE.sub(r"\1:10.0", text)
        if rel.endswith(".gitlab-ci.yml") and still_net8 and MARKER not in new and new != text:
            new, note = _gitlab_add_runtime8(new)
            if note:
                follow_ups.append(f"{rel}: {note}")
        if rel.startswith(".github/workflows/"):
            new = _github_add_sdk10(new, keep8=still_net8)
        if new != text:
            err = yaml_ok(new)
            if err:
                follow_ups.append(f"{rel}: edit skipped, the result would not parse ({err})")
                continue
            edits.write_like(path, new, edits.read(path))
            changes.append({"file": rel, "kind": "ci_sdk", "before": "SDK 8.0", "after": "SDK 10.0"
                            + (" + .NET 8 runtime" if still_net8 else "")})
        elif re.search(r"8\.0", text) and "dotnet" in text.lower():
            follow_ups.append(f"{rel}: mentions 8.0 in a form this tool does not edit; check it by hand")
    for rel in inventory["files"]["global_json"]:  # MSBuild SDK pins are versioned separately from the .NET SDK
        try:
            pins = json.loads((wt / rel).read_text(encoding="utf-8")).get("msbuild-sdks", {})
        except (OSError, json.JSONDecodeError):
            pins = {}
        if pins:
            follow_ups.append(f"{rel} also pins MSBuild SDKs ({', '.join(f'{k} {v}' for k, v in pins.items())}): check "
                              "each for a version that supports .NET 10 (e.g. Microsoft.Build.Sql needed a newer major in a "
                              "real upgrade); this tool does not change them.")
    return changes, follow_ups


MSBUILD_SDK_FILE = re.compile(r"[\\/]([\w.\-]+)[\\/](\d[\w.\-]*)[\\/]Sdk[\\/]", re.I)


def bump_msbuild_sdk_pins(wt: Path, inventory: dict, errors: list[dict], nuget: NuGetClient) -> list[dict]:
    """Repair for SDK-MSBUILD-SDK-TOO-OLD: an MSB4019 raised inside an MSBuild project SDK package
    (…/nuget/<id>/<version>/Sdk/…) that global.json pins. Raise the pin to the newest stable release;
    the rebuild and the gate decide whether it stays."""
    broken = set()
    for e in errors:
        if e.get("code") == "MSB4019":
            m = MSBUILD_SDK_FILE.search(e.get("file") or "") or MSBUILD_SDK_FILE.search(e.get("message") or "")
            if m:
                broken.add((m.group(1).lower(), m.group(2)))
    changes = []
    for rel in inventory["files"]["global_json"]:
        path = wt / rel
        try:
            original = edits.read(path)
            data = json.loads(original.lstrip(edits.BOM))
        except (OSError, json.JSONDecodeError):
            continue
        pins = data.get("msbuild-sdks", {})
        for name, version in list(pins.items()):
            if (name.lower(), version) not in broken:
                continue
            newest = nuget.newest_allowed(name)  # approved versions only
            if newest and newest != version:
                pins[name] = newest
                changes.append({"file": rel, "kind": "msbuild_sdk_pin", "package": name, "before": version,
                                "after": newest, "rule": "SDK-MSBUILD-SDK-TOO-OLD"})
        if changes:
            edits.write_json_like(path, data, original)
    return changes


def _gitlab_add_runtime8(text: str) -> tuple[str, str | None]:
    """Default before_script installs the .NET 8 runtime into the SDK 10 image."""
    item = f"  # {MARKER}\n  - {RUNTIME8_LINE}\n"
    data = yaml.load(text, Loader=_AnyTagLoader) or {}
    jobs_with_own = sorted(k for k, v in data.items() if isinstance(v, dict) and "before_script" in v
                           and not k.startswith("."))
    note = (f"jobs {', '.join(jobs_with_own)} define their own before_script, which replaces the default: "
            f"add the .NET 8 runtime line there by hand") if jobs_with_own else         "checked: no job defines its own before_script, so the default one covers every job"
    m = re.search(r"^before_script:\s*\n", text, re.M)
    if m:
        return text[: m.end()] + item + text[m.end():], note
    m = re.search(r"^image:.*\n", text, re.M)
    block = f"\nbefore_script:\n{item}"
    if m:
        return text[: m.end()] + block + text[m.end():], note
    return block.lstrip("\n") + "\n" + text, note


def _github_add_sdk10(text: str, keep8: bool) -> str:
    def repl(m: re.Match) -> str:
        indent = m.group(1)
        versions = (["8.0.x"] if keep8 else []) + ["10.0.x"]
        return f"{indent}dotnet-version: |\n" + "".join(f"{indent}  {v}\n" for v in versions)
    return re.sub(r"^([ \t]*)dotnet-version:\s*['\"]?8\.0(?:\.x)?['\"]?\s*\n", repl, text, flags=re.M)


# ------------------------------------------------------------------------------- PHASE_5
def phase5_edits(wt: Path, inventory: dict, plan: dict) -> tuple[list[dict], list[str]]:
    graph = ProjectGraph(inventory["projects"])
    projects = {p["id"]: p for p in inventory["projects"]}
    changes, notes = [], []
    for s in plan["shared_projects"]:
        pid = s["project_id"]
        if pid not in projects:
            notes.append(f"{pid}: no longer exists")
            continue
        own_tests = list(s["validation_matrix"][0]["tests"]) if s.get("validation_matrix") else []
        # Every consumer counts, including other groups' test projects that reference it directly.
        consumers = [c for c in graph.all_consumers(pid) if c not in own_tests]
        still8 = [c for c in consumers if any(t.startswith("net8.0") for t in projects[c]["target_frameworks"])]
        if still8:
            notes.append(f"{s['name']} keeps net8.0: still used by {', '.join(projects[c]['name'] for c in still8)}")
            continue
        for target in [pid] + [t for t in own_tests if t in projects]:
            tfms = projects[target]["target_frameworks"]
            new = [t for t in tfms if not t.startswith("net8.0")]
            if new and new != tfms:
                change = set_target_frameworks(wt / target, new)
                changes.append({"file": target, "kind": "retire_net8", **change})
                changes += _drop_net8_conditions(wt, target)
    return changes, notes


def _drop_net8_conditions(wt: Path, rel: str) -> list[dict]:
    """Package references kept only for net8.0 have no purpose once net8.0 is gone."""
    path = wt / rel
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"[ \t]*<PackageReference[^>]*Condition=\"\$\(TargetFramework\.StartsWith\('net8\.0'\)\)\"[^>]*/>\r?\n?",
                 "", text)
    new = re.sub(r"[ \t]*<ItemGroup>\s*</ItemGroup>[ \t]*\r?\n?", "", new)
    if new != text:
        edits.write_like(path, new, edits.read(path))
        return [{"file": rel, "kind": "remove_net8_only_package_reference"}]
    return []


# ------------------------------------------------------------------------------- PHASE_6
def phase6_edits(wt: Path, inventory: dict, plan: dict, nuget: NuGetClient,
                 original_inventory: dict | None = None) -> tuple[list[dict], list[str]]:
    changes, notes = [], []
    users: dict[str, list[dict]] = {}
    for p in inventory["projects"]:
        for pk in p["packages"]:
            users.setdefault(pk["id"].lower(), []).append(p)
    for c in plan["central_package_strategies"]:
        pkg = c["package"]
        on8 = [p["name"] for p in users.get(pkg.lower(), []) if any(t.startswith("net8.0") for t in p["target_frameworks"])]
        if on8:
            notes.append(f"{pkg}: deferred, still used by net8.0 projects {', '.join(on8)}")
            continue
        target = c.get("net10_version")
        if not target:
            target = nuget.newest_allowed(pkg, 10)
        elif nuget.is_approved(pkg, target) is False:
            target = nuget.newest_allowed(pkg, 10)
        if not target:
            notes.append(f"{pkg}: no approved 10.x version in the package catalogue (or lookup failed); not changed")
            continue
        for rel in inventory["central_package_files"]:
            path = wt / rel
            text = path.read_text(encoding="utf-8")
            rx = re.compile(r'(<PackageVersion\s+Include="' + re.escape(pkg) + r'"\s+Version=")([^"]+)(")', re.I)
            m = rx.search(text)
            if m and m.group(2) != target:
                edits.write_like(path, rx.sub(lambda mm: mm.group(1) + target + mm.group(3), text, count=1),
                                 edits.read(path))
                changes.append({"file": rel, "kind": "central_package_version", "package": pkg,
                                "before": m.group(2), "after": target})
    # Central versions that became unused through the migration (used at analysis time, unused now).
    # Entries that were already unused before are left alone: they may be there on purpose.
    used_before = {pk["id"].lower() for p in (original_inventory or {}).get("projects", []) for pk in p["packages"]}
    for rel in inventory["central_package_files"]:
        path = wt / rel
        text = path.read_text(encoding="utf-8")
        for pkg in re.findall(r'<PackageVersion\s+Include="([^"]+)"', text):
            if pkg.lower() in used_before and pkg.lower() not in users:
                text = re.sub(r'[ \t]*<PackageVersion\s+Include="' + re.escape(pkg) + r'"[^>]*/>[ \t]*\r?\n?', "", text, count=1)
                changes.append({"file": rel, "kind": "remove_unused_central_package", "package": pkg})
        if text != path.read_text(encoding="utf-8"):
            edits.write_like(path, text, edits.read(path))
    return changes, notes


# ------------------------------------------------------------------------------- PHASE_8
def phase8_edits(wt: Path, inventory: dict) -> tuple[list[dict], list[str]]:
    remaining = _net8_projects(inventory)
    if remaining:
        return [], [f"net8.0 still targeted by: {', '.join(remaining)}"]
    changes = []
    for rel in inventory["ci_entrypoints"]:
        path = wt / rel
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"[ \t]*# " + re.escape(MARKER) + r"\r?\n[ \t]*- " + re.escape(RUNTIME8_LINE) + r"\r?\n", "", text)
        new = re.sub(r"^before_script:\s*\n(?=\S|\Z)", "", new, flags=re.M)  # drop an emptied default block
        new = re.sub(r"\n{3,}", "\n\n", new)
        new = re.sub(r"^([ \t]*)8\.0\.x\r?\n(?=[ \t]*10\.0\.x)", "", new, flags=re.M)  # GitHub version list
        if new != text and not yaml_ok(new):
            edits.write_like(path, new, edits.read(path))
            changes.append({"file": rel, "kind": "ci_remove_runtime8"})
    changes += fold_central_tfm(wt, inventory)
    return changes, []


CENTRAL_TF = re.compile(r"(<TargetFramework>)net8\.0(</TargetFramework>)")


def fold_central_tfm(wt: Path, inventory: dict) -> list[dict]:
    """If the root Directory.Build.props sets the target framework (net8.0), group runs had to add
    per-project overrides. Once every project is on net10.0, move the central value to net10.0 and
    delete the per-project lines that only repeat it (platform-specific values such as -windows stay)."""
    central = wt / "Directory.Build.props"
    if not central.exists() or not CENTRAL_TF.search(central.read_text(encoding="utf-8")):
        return []
    changes = []
    text = central.read_text(encoding="utf-8")
    edits.write_like(central, CENTRAL_TF.sub(r"\g<1>net10.0\2", text, count=1), edits.read(central))
    changes.append({"file": "Directory.Build.props", "kind": "central_target_framework", "before": "net8.0", "after": "net10.0"})
    for rel in inventory["files"]["projects"]:
        path = wt / rel
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"[ \t]*<TargetFramework>(?:net10\.0)?</TargetFramework>[ \t]*\r?\n?", "", text)
        new = re.sub(r"[ \t]*<PropertyGroup>\s*</PropertyGroup>[ \t]*\r?\n?", "", new)
        if new != text:
            edits.write_like(path, new, edits.read(path))
            changes.append({"file": rel, "kind": "remove_redundant_target_framework"})
    return changes


# ------------------------------------------------------------------------------- driver
def run_global_phase(repo: Path, out: Path, runner: Runner, plan: dict, phase: str, request: dict,
                     preflight: dict | None, nuget: NuGetClient | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result: dict = {"schema_version": SCHEMA_VERSION, "mode": "global_phase", "phase": phase, "group_id": None,
                    "plan_id": plan.get("plan_id"), "target_framework": TARGET_TFM, "recipe_ids": [plan.get("recipe_id")],
                    "status": "STARTED", "files_changed": [], "transformations": [], "repair_attempts": [],
                    "unresolved_items": [], "excluded_work": [], "proposed_global_changes": [], "manual_follow_ups": [],
                    "rollback": {"performed": False, "verified": False}, "ready_for_human_review": False}

    def finish(status: str) -> dict:
        result.update(status=status, duration_s=round(time.monotonic() - started, 1))
        return result

    if phase not in PHASES:
        result["unresolved_items"].append(f"unknown phase {phase}; one of {PHASES}")
        return _write(out, finish("REJECTED"))
    if not request.get("approved"):
        result["unresolved_items"].append("Repository-global phases change files every group shares; "
                                          "rerun with explicit approval (--approve-global).")
        return _write(out, finish("REJECTED"))
    before = snapshot(repo, runner)
    if before["commit"] != plan["repository_commit"] and not request.get("allow_newer_commit"):
        result["unresolved_items"].append("Repository moved since the analysis; pass --allow-newer-commit or rerun it.")
        return _write(out, finish("REJECTED"))

    wt = out / "worktree"
    shutil.rmtree(wt, ignore_errors=True)
    add = runner.git(["worktree", "add", "--detach", str(wt), "HEAD"], repo, "global-worktree-add")
    if not add.ok:
        result["unresolved_items"].append("git worktree add failed: " + add.tail(3))
        return _write(out, finish("REJECTED"))
    try:
        _progress(f"{phase}: reading projects")
        inventory = run_inventory(wt, runner, before["commit"])
        caps = (preflight or {}).get("capabilities", {"can_test_net8": True, "can_test_net10": True})
        fp = cache.fingerprint(runner, preflight)
        cache_dir = Path(request["baseline_cache_dir"]) if request.get("baseline_cache_dir") else None
        tree = cache.head_tree(wt, runner)
        base, reason = ((None, "cache disabled (--no-cache)") if request.get("no_cache")
                        else cache.load(cache_dir, tree, fp, {p["id"] for p in inventory["projects"]}))
        if base is None:
            _progress(f"baseline build/test of the whole repository (slow; {reason})")
            base = run_build_and_test(wt, out, "global-baseline", runner, inventory, caps)
            cache.save(cache_dir, tree, fp, base, None)
            reason = "built (" + reason + ")"
        result["baseline"] = _bt_summary(base) | {"source": reason, "tree": tree}
        ref = request.get("reference_baseline")
        if (phase != "PHASE_7_REPO_VALIDATION" and not base["tests"]["results"]
                and ref and ref.get("tests", {}).get("results")):
            # The phase's own baseline could not run (typically: global.json pins an SDK that is not
            # installed). Comparing with nothing would let every gate pass vacuously; compare with the
            # analysis baseline of the same code instead, and say so.
            base = ref
            result["baseline"] = _bt_summary(ref) | {
                "source": result["baseline"].get("source"), "tree": result["baseline"].get("tree"),
                "compared_with": "analysis baseline (this phase's own baseline produced no test results)"}

        if phase == "PHASE_7_REPO_VALIDATION":
            ref = request.get("reference_baseline")  # the analysis baseline (original net8.0 repository)
            remaining = _net8_projects(inventory)
            tests = compare_tests(ref, base) if ref else None
            diag = new_diagnostics(ref, base) if ref else None
            ok = base["build_succeeded"] and base["restore_succeeded"] and (tests is None or not tests["regressions"])
            result["validation"] = {
                "compared_with": "analysis baseline (original repository)" if ref else "nothing (no analysis baseline)",
                "regressions": tests["regressions"] if tests else None,
                "baseline_failures_still_failing": tests["baseline_failures_still_failing"] if tests else None,
                "new_errors": diag["new_errors"] if diag else None,
                "projects_still_on_net8": remaining}
            result["unresolved_items"] += [f"still targets net8.0: {p}" for p in remaining]
            return finish("VALIDATION_PASSED" if ok else "VALIDATION_FAILED")

        if phase == "PHASE_1_SDK_GLOBAL":
            changes, notes = phase1_edits(wt, inventory, (preflight or {}).get("sdks", []))
        elif phase == "PHASE_5_RETIRE_NET8":
            changes, notes = phase5_edits(wt, inventory, plan)
        elif phase == "PHASE_6_CONVERGE":
            changes, notes = phase6_edits(wt, inventory, plan, nuget or NuGetClient(out / "nuget-cache"),
                                          request.get("original_inventory"))
        else:
            changes, notes = phase8_edits(wt, inventory)
        result["transformations"] = [dict(c, change_class="REPOSITORY_GLOBAL" if phase != "PHASE_5_RETIRE_NET8"
                                          else "GROUP_SHARED") for c in changes]
        result["manual_follow_ups"] = notes
        if not changes:
            result["unresolved_items"] += notes or ["nothing to change in this phase"]
            blocked = phase in {"PHASE_5_RETIRE_NET8", "PHASE_8_CUTOVER"} and notes
            return finish("BLOCKED_NOT_READY" if blocked else "NOTHING_TO_DO")

        _progress(f"{phase}: {len(changes)} change(s) applied; full build/test")
        new_inv = run_inventory(wt, runner, before["commit"])
        current = run_build_and_test(wt, out, "global-after", runner, new_inv, caps)
        tests = settle_regressions(compare_tests(base, current), wt, out, "global-after", runner)
        diag = new_diagnostics(base, current)
        if phase == "PHASE_1_SDK_GLOBAL" and not current["build_succeeded"]:
            bumps = bump_msbuild_sdk_pins(wt, new_inv, current["errors"], nuget or NuGetClient(out / "nuget-cache"))
            record = {"attempt": 1, "failures": [{k: e.get(k) for k in ("code", "file", "project")} for e in current["errors"][:20]],
                      "selected_rules": ["SDK-MSBUILD-SDK-TOO-OLD"] if bumps else [],
                      "files_changed": sorted({b["file"] for b in bumps}), "changes": bumps,
                      "validation_result": None if bumps else "not_attempted",
                      "next_action": "final_gate" if bumps else "stop_for_human_review"}
            result["repair_attempts"].append(record)
            if bumps:
                _progress(f"{phase}: repair: " + ", ".join(f"{b['package']} {b['before']} → {b['after']}" for b in bumps))
                changes += bumps
                result["transformations"] += [dict(b, change_class="REPOSITORY_GLOBAL") for b in bumps]
                result["manual_follow_ups"] = [n for n in result["manual_follow_ups"] if "pins MSBuild SDKs" not in n] + [
                    f"MSBuild SDK {b['package']} raised {b['before']} → {b['after']} (it failed with SDK 10): read its "
                    "release notes for breaking changes before merging." for b in bumps]
                current = run_build_and_test(wt, out, "global-after-repair", runner, new_inv, caps)
                record["validation_result"] = "passed" if current["build_succeeded"] else "failed"
                record["next_action"] = "final_gate" if current["build_succeeded"] else "rollback"
                tests = settle_regressions(compare_tests(base, current), wt, out, "global-after-repair", runner)
                diag = new_diagnostics(base, current)
        changed = _changed_files(wt, runner)
        allowed = {c["file"] for c in changes}
        gate = [
            {"check": "restore_succeeds", "passed": current["restore_succeeded"], "detail": "every entry point"},
            {"check": "build_succeeds", "passed": current["build_succeeded"] and not diag["new_errors"],
             "detail": f"{len(diag['new_errors'])} new error(s)"},
            {"check": "no_new_test_failures", "passed": not tests["regressions"],
             "detail": ", ".join(f"{r['test']} ({r.get('tfm')})" for r in tests["regressions"]) or "none"},
            {"check": "every_test_still_runs", "passed": not tests["not_run_in_experiment"],
             "detail": f"{len(tests['not_run_in_experiment'])} test(s) did not run"},
            {"check": "diff_limited_to_phase_files", "passed": set(changed) <= allowed,
             "detail": ", ".join(sorted(set(changed) - allowed)) or "only this phase's files changed"},
            {"check": "ci_files_parse", "passed": all(not yaml_ok((wt / f).read_text(encoding="utf-8"))
                                                     for f in changed if f.endswith((".yml", ".yaml"))),
             "detail": "YAML parses (CI itself is not executed locally)"},
        ]
        if tests.get("environment_unstable"):
            result["manual_follow_ups"].append(
                f"{len(tests['environment_unstable'])} test(s) NOT VALIDATED: failed twice with a timeout, deadlock or "
                "lost connection (test environment unstable), not counted as passed: "
                + ", ".join(r["test"] for r in tests["environment_unstable"][:10]))
        if tests.get("flaky"):
            result["manual_follow_ups"].append(
                f"{len(tests['flaky'])} test(s) failed once and passed on a re-run (flaky, not blocking): "
                + ", ".join(r["test"] for r in tests["flaky"][:10]))
        result.update(files_changed=changed, final_gate=gate,
                      final_validation=_bt_summary(current) | {"regressions": tests["regressions"],
                                                               "baseline_failures_still_failing": tests["baseline_failures_still_failing"]})
        if all(c["passed"] for c in gate):
            result_tree = cache.working_tree_hash(wt, runner)
            cache.save(cache_dir, result_tree, fp, current, None)
            # The repository's own line-ending settings (core.autocrlf), as for its commits: forcing them off
            # compares a CRLF checkout with LF blobs and turns every line into a change.
            quiet = ["-c", "core.safecrlf=false"]
            patch = runner.git(quiet + ["diff", "--binary", "HEAD"], wt, "global-diff")
            leaks = find_secrets(added_lines(patch.stdout))
            if leaks:  # never hand over a patch that would add a credential to the repository
                result["unresolved_items"].append("the patch would add something that looks like a credential: "
                                                  + ", ".join(leaks))
                result["rollback"]["performed"] = True
                return finish("ROLLED_BACK")
            patch_path = out / f"{phase}.patch"
            patch_path.write_text(patch.stdout, encoding="utf-8", newline="")
            result.update(patch_path=str(patch_path), result_tree=result_tree, ready_for_human_review=True)
            if any(f.endswith((".yml", ".yaml")) for f in changed):
                result["manual_follow_ups"].append("Run the CI pipeline on a branch before merging: CI was not executed locally.")
            return finish("SUCCESS")
        result["unresolved_items"] += [f"{c['check']}: {c['detail']}" for c in gate if not c["passed"]]
        result["rollback"]["performed"] = True
        return finish("ROLLED_BACK")
    finally:
        runner.git(["worktree", "remove", "--force", str(wt)], repo, "global-worktree-remove")
        runner.git(["worktree", "prune"], repo, "global-worktree-prune")
        shutil.rmtree(wt, ignore_errors=True)
        after = snapshot(repo, runner)
        result["rollback"]["verified"] = (before["commit"] == after["commit"] and before["status_sha256"] == after["status_sha256"]
                                          and before["worktree_count"] == after["worktree_count"] and not wt.exists())
        result["rollback_verified"] = result["rollback"]["verified"]
        result["changes_persisted"] = False
        _write(out, result, schema=Path(__file__).resolve().parents[2] / "schemas" / "result.schema.json")
        _progress(f"done: {result['status']}")


def _write(out: Path, result: dict, schema: Path | None = None) -> dict:
    if schema is not None:
        problems = _validate_result(result, schema)
        if problems:
            result["schema_problems"], result["ready_for_human_review"] = problems, False
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
