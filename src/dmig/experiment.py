"""Step A9: reversible .NET 10 experiment in a disposable git worktree.

1. Record the original state (HEAD, tracked status, untracked files, submodules).
2. Create a detached worktree of HEAD in the output directory.
3. Apply only target-SDK/framework changes (TFMs; global.json only if the SDK requires it).
4. Restore/build/test with the same runner as the baseline; capture diagnostics.
5. Remove the worktree and prune it.
6. Verify the original workspace exactly matches the initial state.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from . import SCHEMA_VERSION, edits
from .buildtest import parse_trx_files, run_build_and_test
from .dotnet import Runner

TFM_ELEMENT = re.compile(r"(<TargetFrameworks?>)([^<]*)(</TargetFrameworks?>)")
NET8 = re.compile(r"\bnet8\.0(?=$|[-;\s])")


def snapshot(repo: Path, runner: Runner) -> dict:
    head = runner.git(["rev-parse", "HEAD"], repo, "git-head")
    status = runner.git(["status", "--porcelain=v1", "--untracked-files=all"], repo, "git-status")
    subs = runner.git(["submodule", "status", "--recursive"], repo, "git-submodules")
    worktrees = runner.git(["worktree", "list", "--porcelain"], repo, "git-worktrees")
    return {
        "commit": head.output.strip() if head.ok else None,
        "tracked_and_untracked_status": status.output.strip().splitlines() if status.ok else None,
        "status_sha256": hashlib.sha256(status.output.encode()).hexdigest() if status.ok else None,
        "submodules": subs.output.strip().splitlines() if subs.ok else [],
        "worktree_count": len([l for l in worktrees.output.splitlines() if l.startswith("worktree ")]) if worktrees.ok else None,
    }


def change_class(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1].lower()
    if rel.lower() in {"global.json", "directory.build.props", "directory.build.targets",
                       "directory.packages.props", "nuget.config"}:
        return "REPOSITORY_GLOBAL"
    if name.startswith("directory.") or name.endswith((".props", ".targets")):
        return "GROUP_SHARED"
    return "PROJECT_LOCAL"


def retarget(worktree: Path, inventory: dict) -> list[dict]:
    """net8.0 -> net10.0 inside TargetFramework(s) elements, wherever they are defined."""
    candidates = inventory["files"]["projects"] + inventory["files"]["directory_build_props"] \
        + inventory["files"]["directory_build_targets"] + inventory["files"]["custom_props_targets"]
    changes = []
    for rel in candidates:
        path = worktree / rel
        text = path.read_text(encoding="utf-8")

        def swap(m: re.Match) -> str:
            return m.group(1) + NET8.sub("net10.0", m.group(2)) + m.group(3)

        new = TFM_ELEMENT.sub(swap, text)
        if new != text:
            edits.write_like(path, new, edits.read(path))
            before = sorted(set(m.group(2) for m in TFM_ELEMENT.finditer(text)))
            after = sorted(set(m.group(2) for m in TFM_ELEMENT.finditer(new)))
            changes.append({"file": rel, "change_class": change_class(rel), "kind": "target_framework",
                            "before": before, "after": after})
    return changes


def ensure_sdk(worktree: Path, inventory: dict, selected_sdk: str | None, sdks: list[dict]) -> list[dict]:
    """Experiment-only global.json bump when the pinned SDK is not .NET 10 (a REPOSITORY_GLOBAL change)."""
    if selected_sdk and selected_sdk.startswith("10."):
        return []
    sdk10 = sorted((s["version"] for s in sdks if s["version"].startswith("10.")), reverse=True)
    changes = []
    for rel in inventory["files"]["global_json"]:
        path = worktree / rel
        original = edits.read(path)
        data = json.loads(original.lstrip(edits.BOM))
        before = data.get("sdk", {}).get("version")
        if sdk10:
            major, minor, patch = (int(x) for x in re.findall(r"\d+", sdk10[0])[:3])
            data.setdefault("sdk", {})["version"] = f"{major}.{minor}.{patch // 100 * 100}"  # feature band
            data["sdk"]["rollForward"] = "latestFeature"
            edits.write_json_like(path, data, original)
            changes.append({"file": rel, "change_class": "REPOSITORY_GLOBAL", "kind": "sdk_pin",
                            "before": before, "after": data["sdk"]["version"]})
    return changes


def _method(name: str) -> str:
    """Test method without its argument list or data-row label: data-driven tests (AutoFixture,
    random data) print different arguments on every run, and theories run once per provider
    ('Method [SqlServer]'), so names only match without them."""
    return name.split("(", 1)[0].split(" [", 1)[0].strip()


# Failures that point at the test environment, not the code: timeouts, deadlocks, lost connections.
INFRA_FAILURE = re.compile(r"Timeout Expired|timed? ?out|deadlock|server is not responding|network-related|"
                           r"connection (?:was )?(?:refused|reset|closed|forcibly)|SocketException|"
                           r"could not open a connection|Cannot access destination table", re.I)


def settle_regressions(tests: dict, workdir: Path, out_dir: Path, label: str, runner: Runner) -> dict:
    """Re-run only the regressed test methods once. A method that passes on the retry is reported as
    flaky and does not block; one that fails again stays a regression. Each entry records whether
    its failure looks like an environment problem (timeout, deadlock, connection)."""
    regs = tests.get("regressions") or []
    if not regs:
        return tests
    by_project: dict[str, list[str]] = {}
    for r in regs:
        by_project.setdefault(r["project"], []).append(r["test"])
    trx_dir = out_dir / "trx" / f"{label}-retry"
    shutil.rmtree(trx_dir, ignore_errors=True)
    for project, methods in by_project.items():
        names = sorted(set(methods))
        for i in range(0, len(names), 40):  # keep the filter expression short
            filt = "|".join(f"FullyQualifiedName~{n}" for n in names[i:i + 40])
            runner.run(["test", project, "--no-build", "--filter", filt, "--logger", "trx",
                        "--results-directory", str(trx_dir)], workdir, f"{label}-retry", timeout_s=3600)
    rows = parse_trx_files(trx_dir, {Path(p).stem.lower() + ".dll": p for p in by_project})
    retried: dict[tuple, list[str]] = {}
    retry_messages: dict[tuple, list[str]] = {}
    for r in rows:
        retried.setdefault((r["project"], _method(r["test"])), []).append(r["outcome"])
        if r["outcome"] == "failed":
            retry_messages.setdefault((r["project"], _method(r["test"])), []).append(r.get("message") or "")
    still, flaky, unstable = [], [], []
    for r in regs:
        key = (r["project"], r["test"])
        outcome = _fold(retried.get(key, []))
        entry = dict(r, retry=outcome, environment_suspected=bool(INFRA_FAILURE.search(r.get("message") or "")))
        if outcome == "passed":
            flaky.append(entry)
        elif entry["environment_suspected"] and retry_messages.get(key) and \
                all(INFRA_FAILURE.search(m) for m in retry_messages[key]):
            # failed twice, both times with a timeout/deadlock/connection error: the test environment
            # could not give an answer. Not a regression, and never counted as passed.
            unstable.append(entry)
        else:
            still.append(entry)
    return dict(tests, regressions=still, flaky=flaky, environment_unstable=unstable)


def _fold(outcomes: list[str]) -> str:
    return "failed" if "failed" in outcomes else ("passed" if "passed" in outcomes else (outcomes[0] if outcomes else "not_run"))


def compare_tests(baseline: dict, experiment: dict) -> dict:
    """Per test method (all data rows folded: any failed row = failed). A method that passed in
    the baseline and fails now is a regression; multi-targeted runs are folded the same way, so a
    failure that appears only on the new net10.0 target still counts."""
    base_rows: dict[tuple, list] = {}
    for r in baseline["tests"]["results"]:
        base_rows.setdefault((r["project"], _method(r["test"])), []).append(r["outcome"])
    now_rows: dict[tuple, list] = {}
    for r in experiment["tests"]["results"]:
        now_rows.setdefault((r["project"], _method(r["test"])), []).append(r)
    regressions, still_failing, fixed, not_run = [], [], [], []
    for key in sorted(base_rows):
        outcome = _fold(base_rows[key])
        runs = now_rows.get(key)
        if not runs:
            not_run.append({"project": key[0], "test": key[1], "baseline": outcome, "experiment": "not_run",
                            "tfm": None, "message": None})
            continue
        failed = [r for r in runs if r["outcome"] == "failed"]
        now = _fold([r["outcome"] for r in runs])
        entry = {"project": key[0], "test": key[1], "baseline": outcome, "experiment": now,
                 "tfm": (failed[0] if failed else runs[0]).get("tfm"),
                 "message": failed[0].get("message") if failed else None, "rows": len(runs)}
        if outcome == "passed" and now == "failed":
            regressions.append(entry)
        elif outcome == "failed" and now == "failed":
            still_failing.append(entry)
        elif outcome == "failed" and now == "passed":
            fixed.append(entry)
    new_tests = [{"project": k[0], "test": k[1], "outcome": _fold([r["outcome"] for r in rs]),
                  "tfm": rs[0].get("tfm")} for k, rs in now_rows.items() if k not in base_rows]
    return {"regressions": regressions, "baseline_failures_still_failing": still_failing,
            "baseline_failures_now_passing": fixed, "not_run_in_experiment": not_run, "new_tests": new_tests,
            "compared_by": "test method (data rows folded)"}


def new_diagnostics(baseline: dict, experiment: dict) -> dict:
    def key(d: dict) -> tuple:
        return (d["code"], d["file"], d["line"])
    base_keys = {key(d) for d in baseline["warnings"] + baseline["errors"]}
    return {
        "new_errors": [d for d in experiment["errors"] if key(d) not in base_keys],
        "new_warnings": [d for d in experiment["warnings"] if key(d) not in base_keys],
    }


DOWNGRADE = re.compile(r"Detected package downgrade: (\S+) from (\S+) to (\d[\w.\-]*[\w])")


def raise_downgrade_pins(worktree: Path, errors: list[dict], only: set[str] | None = None) -> list[dict]:
    """For NU1605 'X from HIGH to LOW', raise PackageReference/PackageVersion pins of X at LOW
    inside the disposable copy (limited to the relative paths in `only` when given).
    Returns the edits for the report."""
    wanted: dict[tuple[str, str], str] = {}
    for e in errors:
        m = DOWNGRADE.search(e.get("message", "")) if e.get("code") == "NU1605" else None
        if m:
            wanted[(m.group(1), m.group(3))] = m.group(2)
    fixes = []
    if not wanted:
        return fixes
    for f in [*worktree.rglob("*.csproj"), *worktree.rglob("*.props"), *worktree.rglob("*.targets")]:
        if any(part in {"bin", "obj", ".git"} for part in f.relative_to(worktree).parts):
            continue
        if only is not None and f.relative_to(worktree).as_posix() not in only:
            continue
        text = edits.read(f)
        new = text
        for (pkg, low), high in wanted.items():
            pat = re.compile(r'(<Package(?:Reference|Version)\s+(?:Include|Update)="' + re.escape(pkg)
                             + r'"\s+Version=")' + re.escape(low) + '"', re.I)
            new, n = pat.subn(lambda m: m.group(1) + high + '"', new)
            if n:
                fixes.append({"file": f.relative_to(worktree).as_posix(), "package": pkg,
                              "before": low, "after": high, "reason": "NU1605 downgrade (measurement only)"})
        if new != text:
            edits.write_like(f, new, text)
    return fixes


def package_wall(bt: dict) -> list[str]:
    """NuGet codes that stopped restore only because warnings are treated as errors."""
    if bt.get("restore_succeeded", True):
        return []
    nu = [e for e in bt.get("errors", []) if str(e.get("code", "")).startswith("NU")]
    if not nu or not all("Warning As Error" in e.get("message", "") for e in nu):
        return []
    return sorted({e["code"] for e in nu})


# Compiler diagnostics that are warnings by default and only fail because the repository treats
# warnings as errors: obsolete APIs, analyzers, nullability, ASP.NET deprecations.
WARNING_BY_DEFAULT = re.compile(r"^(?:SYSLIB\d+|CA\d+|IDE\d+|ASPDEPR\d+|CS0612|CS0618|CS86\d\d|CS87\d\d|CS89\d\d|CS9258)$")
MAX_LAYERS = 4


def compile_wall(bt: dict) -> list[str]:
    """Codes of build errors that are default-warnings promoted to errors."""
    return sorted({e["code"] for e in bt.get("errors", []) if WARNING_BY_DEFAULT.match(str(e.get("code", "")))})


def measure_in_layers(worktree: Path, out_dir: Path, runner: Runner, inventory: dict, caps: dict,
                      result: dict) -> dict:
    """Build and test; when the build stops on warnings-as-errors, record them, relax only those
    codes and measure again, up to MAX_LAYERS. A root library failing on one obsolete API would
    otherwise hide every project behind it. All recorded errors stay in the final result."""
    relaxed: list[str] = []
    recorded: list[dict] = []
    layers: list[dict] = []
    fixes: list[dict] = []
    saved = {k: runner.env.get(k) for k in ("WarningsNotAsErrors", "NoWarn")}
    try:
        for n in range(1, MAX_LAYERS + 1):
            bt = run_build_and_test(worktree, out_dir, "experiment-net10" + ("" if n == 1 else f"-layer{n}"),
                                    runner, inventory, caps)
            new_codes = [c for c in package_wall(bt) + compile_wall(bt) if c not in relaxed]
            layers.append({"layer": n, "restore_succeeded": bt["restore_succeeded"],
                           "build_succeeded": bt["build_succeeded"], "projects_built": len(bt["projects_built"]),
                           "errors": len(bt["errors"]), "stopped_on": new_codes})
            recorded += [e for e in bt["errors"] if e["code"] in new_codes]
            if not new_codes or n == MAX_LAYERS:
                break
            # NU1605 is always an error and projects may overwrite NoWarn, so a downgrade caused by
            # a pin inside the repository is lifted the way an engineer would: raise the pin.
            fixes += raise_downgrade_pins(worktree, bt["errors"])
            relaxed += new_codes
            # Environment variables only fill properties a project leaves unset or appends to.
            for k in saved:
                runner.env[k] = ";".join(filter(None, [saved[k], *relaxed]))
    finally:
        for k, v in saved.items():
            if v is None:
                runner.env.pop(k, None)
            else:
                runner.env[k] = v
    if len(layers) > 1:
        key = lambda e: (e["code"], e["file"], e["line"], e["project"])  # noqa: E731
        seen = {key(e) for e in bt["errors"]}
        bt["errors"] = bt["errors"] + [e for e in recorded if key(e) not in seen]
        bt["layers"] = {"relaxed_codes": relaxed, "per_layer": layers,
                        "note": "Later layers were measured with these codes as warnings (NoWarn/"
                                "WarningsNotAsErrors) in the disposable copy only; every instance "
                                "recorded before relaxing is kept in errors."}
    if fixes:
        result["measurement_fixes"] = fixes
    return bt


def run_experiment(repo: Path, out_dir: Path, runner: Runner, inventory: dict, baseline: dict | None,
                   preflight: dict) -> dict:
    before = snapshot(repo, runner)
    work_root = out_dir / "worktrees"
    work_root.mkdir(parents=True, exist_ok=True)
    worktree = work_root / f"net10-experiment-{int(time.time())}"
    result: dict = {"schema_version": SCHEMA_VERSION, "method": "git-worktree", "original_state_before": before,
                    "worktree": str(worktree), "applied_changes": [], "build_test": None}
    if before["tracked_and_untracked_status"]:
        result["warning"] = ("The repository has uncommitted or untracked changes; the experiment uses the "
                             "committed HEAD and leaves the working tree untouched.")
    add = runner.git(["worktree", "add", "--detach", str(worktree), "HEAD"], repo, "git-worktree-add")
    if not add.ok:
        result.update({"status": "NOT_EXECUTED", "reason": "git worktree add failed: " + add.tail(5)})
        return result
    try:
        changes = ensure_sdk(worktree, inventory, preflight.get("selected_sdk_in_repository"), preflight.get("sdks", []))
        changes += retarget(worktree, inventory)
        result["applied_changes"] = changes
        caps = preflight.get("capabilities", {})
        bt = measure_in_layers(worktree, out_dir, runner, _retargeted(inventory), caps, result)
        result["build_test"] = bt
        if baseline:
            result["test_comparison"] = compare_tests(baseline, bt)
            result["diagnostic_comparison"] = new_diagnostics(baseline, bt)
        result["status"] = "EXECUTED"
    finally:
        rm = runner.git(["worktree", "remove", "--force", str(worktree)], repo, "git-worktree-remove")
        runner.git(["worktree", "prune"], repo, "git-worktree-prune")
        if worktree.exists():  # Windows can keep a handle briefly; never leave the copy behind
            shutil.rmtree(worktree, ignore_errors=True)
        result["cleanup"] = {"worktree_removed": not worktree.exists(), "git_remove_exit": rm.exit_code}
    after = snapshot(repo, runner)
    result["original_state_after"] = after
    result["rollback_verified"] = (
        before["commit"] == after["commit"]
        and before["status_sha256"] == after["status_sha256"]
        and before["submodules"] == after["submodules"]
        and before["worktree_count"] == after["worktree_count"]
        and not worktree.exists())
    return result


def _retargeted(inventory: dict) -> dict:
    """Inventory view with net10.0 TFMs, so test-runtime checks look at the right runtime."""
    clone = json.loads(json.dumps(inventory))
    for p in clone["projects"]:
        p["target_frameworks"] = [NET8.sub("net10.0", t) for t in p["target_frameworks"]]
    return clone
