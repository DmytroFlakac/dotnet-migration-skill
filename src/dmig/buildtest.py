"""Restore, build and test one working copy, with machine-readable results (step A7).

Used for the baseline (original repository) and for the reversible experiment
(disposable worktree), so both are measured the same way and can be compared.
"""
from __future__ import annotations

import json
import platform
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import SCHEMA_VERSION
from .dotnet import Runner

DIAG = re.compile(
    r"^(?P<file>[^\r\n]*?)(?:\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\))?\s*:\s*"
    r"(?P<sev>error|warning)\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<msg>.*?)(?:\s+\[(?P<proj>[^\]]+)\])?\s*$")
BUILT = re.compile(r"^\s*(?P<name>[\w.\-]+) -> (?P<path>.+\.(?:dll|exe))\s*$", re.I)
# "Framework: 'Microsoft.AspNetCore.App', version '8.0.0' (x64)" printed when a test host cannot start
MISSING_FRAMEWORK = re.compile(r"Framework: '([\w.]+)', version '([\d.]+)'")


def classify(code: str, severity: str, file: str = "") -> str:
    """Failure categories for one diagnostic."""
    c = code.upper()
    if c in {"NETSDK1045", "NETSDK1141"} or "MSB4236" == c:
        return "sdk_not_found"
    if c in {"NETSDK1147", "NETSDK1178"}:
        return "workload_missing"
    if c in {"NU1101", "NU1102", "NU1103"}:
        return "unavailable_package_version"
    if c.startswith("NU"):
        return "package_restore_failure"
    if c == "NETSDK1100" or c == "NETSDK1136":
        return "platform_incompatibility"
    if c in {"CS8784", "CS8785"}:
        return "source_generator_failure"
    if c.startswith("MSB"):
        return "custom_msbuild_failure"
    if c.startswith("CS") or c.startswith("FS") or c.startswith("BC"):
        if "/obj/" in file.replace("\\", "/").lower() or file.lower().endswith(".g.cs"):
            return "generated_code_failure"
        return "compile_time_api_break" if severity == "error" else "compiler_warning"
    if c.startswith(("CA", "IDE", "SYSLIB", "ASPDEPR", "RS")):
        return "analyzer_diagnostic"
    return "unknown_failure"


def parse_diagnostics(output: str, root: Path) -> list[dict]:
    seen, out = set(), []
    for line in output.splitlines():
        m = DIAG.match(line.strip())
        if not m:
            continue
        file = m.group("file").strip()
        proj_raw = (m.group("proj") or "").strip()
        proj, _, proj_tf = proj_raw.partition("::")  # multi-target builds: "X.csproj::TargetFramework=net10.0"
        tfm = proj_tf.split("=", 1)[1] if proj_tf.startswith("TargetFramework=") else None
        rel_file, rel_proj = _rel(file, root), _rel(proj, root)
        # NuGet reports against the solution and names the real project in the file position.
        if rel_file.lower().endswith((".csproj", ".fsproj", ".vbproj")) and (
                not rel_proj or rel_proj.lower().endswith((".sln", ".slnx", ".slnf"))):
            rel_proj = rel_file
        key = (m.group("code"), rel_file, m.group("line"), m.group("sev"), rel_proj, tfm)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "severity": m.group("sev"), "code": m.group("code"), "message": m.group("msg").strip(),
            "file": rel_file, "line": int(m.group("line")) if m.group("line") else None,
            "project": rel_proj or None, "tfm": tfm, "failure_class": classify(m.group("code"), m.group("sev"), rel_file),
        })
    return out


def _rel(path: str, root: Path) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.replace("\\", "/")


def parse_trx_files(trx_dir: Path, dll_to_project: dict[str, str]) -> list[dict]:
    results = []
    for trx in sorted(trx_dir.rglob("*.trx")):
        try:
            root = ET.parse(trx).getroot()
        except ET.ParseError:
            continue
        ns = {"t": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        q = (lambda tag: f"t:{tag}") if ns else (lambda tag: tag)
        storage = {}
        for ut in root.iter(f"{{{ns['t']}}}UnitTest" if ns else "UnitTest"):
            raw = ut.get("storage") or ""
            tfm = re.search(r"[\\/](net\d+\.\d+(?:-[a-z]+)?)[\\/]", raw, re.I)
            storage[ut.get("id")] = (Path(raw).name.lower(), tfm.group(1).lower() if tfm else None)
        for r in root.iterfind(f".//{q('UnitTestResult')}", ns):
            dll, tfm = storage.get(r.get("testId"), ("", None))
            message = r.find(f".//{q('Message')}", ns)
            results.append({
                "project": dll_to_project.get(dll, dll),
                "test": r.get("testName"),
                "tfm": tfm,
                "outcome": {"Passed": "passed", "Failed": "failed", "NotExecuted": "skipped"}.get(
                    r.get("outcome"), (r.get("outcome") or "unknown").lower()),
                "duration": r.get("duration"),
                "message": (message.text or "").strip()[:500] if message is not None and message.text else None,
            })
    return results


def choose_entrypoints(repo: Path, inventory: dict) -> tuple[list[str], list[str]]:
    """Root solution(s) first; projects that no root solution covers are built on their own."""
    root_solutions = [s for s in inventory["solutions"]
                      if "/" not in s["path"] and s["path"].lower().endswith((".sln", ".slnx"))]
    root_solutions.sort(key=lambda s: -len(s["projects"]))
    chosen, covered = [], set()
    for sol in root_solutions:
        if set(sol["projects"]) - covered:
            chosen.append(sol["path"])
            covered |= set(sol["projects"])
    extra = sorted(p["id"] for p in inventory["projects"] if p["id"] not in covered)
    return chosen, extra


SCOPE_FILTER = ".dmig-scope.slnf"


def write_scope_filter(workdir: Path, inventory: dict, scope: set[str]) -> list[str] | None:
    """Solution filter limited to `scope` (plus everything those projects reference), next to the
    root solution. Returns the entry points to use, or None when there is no root solution."""
    entries, _ = choose_entrypoints(workdir, inventory)
    sln = next((e for e in entries if e.lower().endswith(".sln") or e.lower().endswith(".slnx")), None)
    if sln is None:
        return None
    deps = {p["id"]: p["project_references"] for p in inventory["projects"]}
    full, stack = set(), list(scope)
    while stack:
        cur = stack.pop()
        if cur not in full:
            full.add(cur)
            stack.extend(deps.get(cur, ()))
    sol_projects = set(next(s["projects"] for s in inventory["solutions"] if s["path"] == sln))
    listed = sorted(full & sol_projects)
    outside = sorted(full - sol_projects)  # not in the solution: built on their own
    (workdir / SCOPE_FILTER).write_text(json.dumps({"solution": {
        "path": sln, "projects": [p.replace("/", "\\") for p in listed]}}, indent=2), encoding="utf-8")
    return [SCOPE_FILTER] + outside


def run_build_and_test(workdir: Path, out_dir: Path, label: str, runner: Runner, inventory: dict,
                       capabilities: dict, run_tests: bool = True,
                       entrypoints: list[str] | None = None, scope: set[str] | None = None) -> dict:
    """Restore → build → test every entry point in workdir (or the given ones, e.g. a scope
    filter). Never raises on build failures."""
    started = time.monotonic()
    if entrypoints is not None:
        targets = entrypoints
    else:
        entries, extra = choose_entrypoints(workdir, inventory)
        targets = entries + extra
    lock_mode = bool(inventory["files"]["lock_files"])
    windows_props = [] if platform.system() == "Windows" else ["-p:EnableWindowsTargeting=true"]
    commands, diagnostics, built = [], [], []
    restore_ok = build_ok = True
    for target in targets:
        r = runner.run(["restore", target, *(["--locked-mode"] if lock_mode else []), *windows_props],
                       workdir, f"{label}-restore", timeout_s=3600)
        commands.append(r.to_record())
        diagnostics += parse_diagnostics(r.output, workdir)
        restore_ok &= r.ok
        b = runner.run(["build", target, "--no-restore", "-nologo", "-clp:NoSummary", *windows_props],
                       workdir, f"{label}-build", timeout_s=3600)
        commands.append(b.to_record())
        diagnostics += parse_diagnostics(b.output, workdir)
        build_ok &= b.ok
        for line in b.output.splitlines():
            m = BUILT.match(line)
            if m:
                built.append({"name": m.group("name"), "output": _rel(m.group("path").strip(), workdir)})
    # de-duplicate diagnostics across restore/build passes
    uniq, seen = [], set()
    for d in diagnostics:
        k = (d["code"], d["file"], d["line"], d["severity"], d["project"])
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    diagnostics = uniq

    tests_section: dict = {"executed": False, "results": [],
                           "total": 0, "passed": 0, "failed": 0, "skipped": 0}
    environmental_blocks = []
    in_scope = (lambda pid: True) if scope is None else (lambda pid: pid in scope)
    test_projects = [p for p in inventory["projects"] if p["kind"] == "test" and in_scope(p["id"])]
    if run_tests and test_projects:
        needs8 = any(t.startswith("net8.0") for p in test_projects for t in p["target_frameworks"])
        needs10 = any(t.startswith("net10.0") for p in test_projects for t in p["target_frameworks"])
        missing = [rt for rt, need, cap in (("net8.0", needs8, "can_test_net8"), ("net10.0", needs10, "can_test_net10"))
                   if need and not capabilities.get(cap, True)]
        if missing:
            environmental_blocks.append({"code": "NOT_EXECUTED_ENVIRONMENT_BLOCKED",
                                         "detail": f"No runtime for {', '.join(missing)} test targets."})
        trx_dir = out_dir / "trx" / label
        dll_map = {p["name"].lower() + ".dll": p["id"] for p in test_projects}
        for target in targets:
            t = runner.run(["test", target, "--no-build", "--logger", "trx",
                            "--results-directory", str(trx_dir), *windows_props],
                           workdir, f"{label}-test", timeout_s=3600)
            commands.append(t.to_record())
            for fw in sorted(set(MISSING_FRAMEWORK.findall(t.output))):
                environmental_blocks.append({
                    "code": "NOT_EXECUTED_ENVIRONMENT_BLOCKED",
                    "detail": f"Test host could not start: shared framework {fw[0]} {fw[1]} is not installed."})
        results = parse_trx_files(trx_dir, dll_map)
        tests_section = {
            "executed": True, "results": results, "total": len(results),
            "passed": sum(r["outcome"] == "passed" for r in results),
            "failed": sum(r["outcome"] == "failed" for r in results),
            "skipped": sum(r["outcome"] == "skipped" for r in results),
        }
        ran = {r["project"] for r in results}
        for p in test_projects:
            if p["id"] not in ran:
                environmental_blocks.append({
                    "code": "TEST_PROJECT_NOT_EXECUTED", "project": p["id"],
                    "detail": "No results: the project did not build, or its runtime/platform is unavailable."})
    elif run_tests:
        tests_section["note"] = "No test projects found."

    built_names = {b["name"].lower() for b in built}
    skipped = [{"project": p["id"], "reason": "no build output (failed, not in an entry point, or not buildable here)"}
               for p in inventory["projects"] if p["name"].lower() not in built_names and in_scope(p["id"])]
    return {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "workdir": str(workdir),
        "entrypoints": targets,
        "scope": sorted(scope) if scope is not None else None,
        "restore_succeeded": restore_ok,
        "build_succeeded": build_ok,
        "commands": commands,
        "projects_built": sorted(built, key=lambda b: b["name"]),
        "projects_skipped": skipped,
        "tests": tests_section,
        "warnings": [d for d in diagnostics if d["severity"] == "warning"],
        "errors": [d for d in diagnostics if d["severity"] == "error"],
        "environmental_blocks": environmental_blocks,
        "duration_s": round(time.monotonic() - started, 1),
        "peak_memory_mb": None,
    }
