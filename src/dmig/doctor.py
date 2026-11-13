"""`dmig doctor`: is this machine ready to run the migration? A plain-language checklist.

Run it first on a new machine (a organisation laptop, a build agent). It reuses the preflight and adds
what organisation machines often differ in: proxies, git settings, Windows long paths, free disk on every
drive involved, the Python side. Every problem comes with what to do or what to ask IT for.
Writes doctor-report.md (for people) and environment-capabilities.json (for tools), the contract's
names; outcome READY, READY_WITH_LIMITATIONS or BLOCKED; exit code 0 / 0 / 1. Changes no source file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from .dotnet import Runner
from .preflight import run_preflight
from .secrets import redact

OK, WARN, FAIL, INFO = "✅", "⚠️", "❌", "ℹ️"


def _free_gb(path: Path) -> float | None:
    try:
        p = path if path.exists() else path.parent
        return round(shutil.disk_usage(p).free / 1024 ** 3, 1)
    except OSError:
        return None


def _long_paths_enabled() -> bool | None:
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:
        return False


def _repo_size(repo: Path, runner: Runner) -> tuple[int, float] | None:
    files = runner.git(["ls-files", "-z"], repo, "git-ls-files")
    if not files.ok:
        return None
    names = [n for n in files.stdout.split("\0") if n]
    size = 0
    for n in names:
        try:
            size += (repo / n).stat().st_size
        except OSError:
            pass
    return len(names), round(size / 1024 ** 2, 1)


def _cache_packages(folder: Path) -> int | None:
    try:
        return sum(1 for d in folder.iterdir() if d.is_dir()) if folder.exists() else 0
    except OSError:
        return None


def _workspace_roundtrip(repo: Path, out: Path, runner: Runner) -> tuple[bool, str]:
    """Create and delete a disposable worktree, and prove the repository did not change."""
    from .experiment import snapshot
    wt = out / "doctor-worktree"
    shutil.rmtree(wt, ignore_errors=True)
    before = snapshot(repo, runner)
    add = runner.git(["worktree", "add", "--detach", str(wt), "HEAD"], repo, "doctor-worktree-add")
    if not add.ok:
        return False, "could not create a git worktree: " + add.tail(2)
    runner.git(["worktree", "remove", "--force", str(wt)], repo, "doctor-worktree-remove")
    runner.git(["worktree", "prune"], repo, "doctor-worktree-prune")
    shutil.rmtree(wt, ignore_errors=True)
    after = snapshot(repo, runner)
    same = before["commit"] == after["commit"] and before["status_sha256"] == after["status_sha256"]
    return (not wt.exists() and same), ("created and deleted; repository unchanged" if same
                                        else "created and deleted, but the repository status changed")


def run_doctor(repo: Path, out: Path, runner: Runner) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    pf = run_preflight(repo, out, runner, probe_network=True)
    checks: list[dict] = []

    def add(area: str, mark: str, what: str, fix: str = "") -> None:
        checks.append({"area": area, "result": mark, "what": what, "fix": fix})

    # .NET --------------------------------------------------------------------------------------
    sdks = [s["version"] for s in pf["sdks"]]
    add(".NET", OK if any(v.startswith("10.") for v in sdks) else FAIL,
        f"`{runner.dotnet}` · SDKs: {', '.join(sdks) or 'none found'}",
        "" if any(v.startswith("10.") for v in sdks) else
        "Ask IT for the .NET 10 SDK (10.0.1xx), or point the skill at an approved copy with --dotnet / DMIG_DOTNET.")
    have = {(r["name"], r["version"].split(".")[0]) for r in pf["runtimes"]}
    for name, label in (("Microsoft.NETCore.App", ".NET runtime"), ("Microsoft.AspNetCore.App", "ASP.NET Core runtime")):
        for major in ("8", "10"):
            ok = (name, major) in have
            add(".NET", OK if ok else WARN, f"{label} {major}: {'installed' if ok else 'missing'}",
                "" if ok else f"Needed to run .NET {major} tests{' of web projects' if 'Asp' in name else ''}. "
                              f"Ask IT for the {label} {major}.")
    if any(b["code"] == "GLOBAL_JSON_UNSATISFIED" for b in pf["blocking_items"]):
        add(".NET", INFO, "The repository's global.json asks for an SDK that is not installed.",
            "Normal before the migration: the analysis uses a throwaway copy moved to SDK 10.")

    # Package feeds ---------------------------------------------------------------------------------
    if not pf["package_sources"]:
        add("Packages", WARN, "No package feed found in the NuGet configuration.",
            "Restore will fail. Ask how this machine is meant to get NuGet packages.")
    for s in pf["package_sources"]:
        st = s.get("status")
        fix = {"AUTH_REQUIRED": "The SDK could not log in to this feed. Install/sign in to the organisation's NuGet "
                                "credential provider (the same login `dotnet restore` needs).",
               "FORBIDDEN": "Your account has no access to this feed. Ask the feed owner for read access.",
               "FEED_UNREACHABLE": "The machine cannot reach this feed. Check VPN/proxy, or ask IT whether it "
                                   "is allowed from this machine."}.get(st, "")
        add("Packages", OK if st in {"OK", "LOCAL_PATH"} else FAIL, f"Feed `{s['url']}`: {s.get('detail', st)}", fix)
    proxies = {k: redact(v) for k, v in os.environ.items() if k.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}}
    add("Network", INFO, "Proxy settings: " + (", ".join(f"{k}={v}" for k, v in proxies.items()) or "none set"),
        "The skill reaches feeds only through `dotnet`, which uses the same proxy and certificates as restore.")

    # git -----------------------------------------------------------------------------------------
    git_ok = pf["disposable_workspace"]["git_available"]
    add("git", OK if git_ok else FAIL, pf["disposable_workspace"].get("git_version") or "git not found",
        "" if git_ok else "Install git (2.20+); the skill makes throwaway copies with git worktrees.")
    if git_ok:
        add("git", OK if pf["disposable_workspace"]["worktree_supported"] else FAIL, "git worktrees: "
            + ("supported" if pf["disposable_workspace"]["worktree_supported"] else "not available here"),
            "" if pf["disposable_workspace"]["worktree_supported"] else "Run the skill inside a git clone.")
        crlf = runner.git(["config", "--get", "core.autocrlf"], repo, "git-autocrlf").output.strip() or "not set"
        add("git", INFO, f"core.autocrlf = {crlf}", "Patches use this same setting, so they apply cleanly here.")
        if os.name == "nt":
            lp = runner.git(["config", "--get", "core.longpaths"], repo, "git-longpaths").output.strip()
            add("git", OK if lp == "true" else WARN, f"core.longpaths = {lp or 'not set'}",
                "" if lp == "true" else "Deep folders can exceed 260 characters: run `git config --global core.longpaths true`.")
    if os.name == "nt":
        lpe = _long_paths_enabled()
        add("Windows", OK if lpe else WARN, f"Windows long paths: {'enabled' if lpe else 'disabled'}",
            "" if lpe else "Builds in deep folders may fail with 'path too long'. Ask IT to enable LongPathsEnabled, "
                           "or keep the repository and output folders short (e.g. C:\\w\\repo).")

    # Repository and disposable workspace -----------------------------------------------------------
    size = _repo_size(repo, runner) if git_ok else None
    add("Repository", INFO, f"{size[0]} tracked files, {size[1]} MB" if size else "size unknown (not a git repository?)",
        "Builds need roughly 5-10x the source size in free disk for outputs and a throwaway copy.")
    if git_ok and pf["disposable_workspace"]["worktree_supported"]:
        ok, detail = _workspace_roundtrip(repo, out, runner)
        add("git", OK if ok else FAIL, f"Disposable workspace: {detail}",
            "" if ok else "The skill needs to create and delete throwaway copies; check folder permissions and disk.")

    # Disk, CPU, memory ------------------------------------------------------------------------------
    nuget_dir = Path(os.environ.get("NUGET_PACKAGES") or Path.home() / ".nuget" / "packages")
    temp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")
    for label, path, need in (("repository drive", repo, 6), ("output drive", out, 2),
                              ("NuGet packages", nuget_dir, 3), ("temp folder", temp_dir, 1)):
        free = _free_gb(path)
        add("Disk", INFO if free is None else (OK if free >= need else FAIL),
            f"{label} ({path}): {free if free is not None else '?'} GB free (needs about {need} GB)",
            "" if free is None or free >= need else f"Free space on that drive, or move it (NUGET_PACKAGES, TEMP).")
    cached = _cache_packages(nuget_dir)
    add("Packages", INFO, f"NuGet package cache: {cached if cached is not None else '?'} package(s) in {nuget_dir}",
        "An empty cache is fine; the first restore downloads everything (slower, more disk).")
    res = pf["resources"]
    add("Machine", INFO, f"{res.get('cpu_cores')} CPU threads, {round((res.get('memory_mb') or 0) / 1024, 1)} GB memory, "
        f"{platform.system()} {platform.release()}",
        "Big repositories build faster on more cores; 16 GB memory or more is comfortable.")

    # Python -----------------------------------------------------------------------------------------
    py_ok = sys.version_info >= (3, 11)
    add("Python", OK if py_ok else FAIL, f"Python {platform.python_version()}",
        "" if py_ok else "Python 3.11 or newer is needed.")
    for lib, pip_name in (("yaml", "pyyaml"), ("jsonschema", "jsonschema")):
        ok = importlib.util.find_spec(lib) is not None
        add("Python", OK if ok else FAIL, f"library `{pip_name}`: {'installed' if ok else 'missing'}",
            "" if ok else f"Run `pip install -r requirements.txt` (or ask for `{pip_name}` from the organisation's Python mirror).")

    # Test services ------------------------------------------------------------------------------------
    add("Tests", INFO, "Databases, queues and other services the tests need are not checked here.",
        "Pass their settings as environment variables or an environment contract; tests that cannot reach "
        "them are reported as not run, never as passed.")

    ready = not any(c["result"] == FAIL for c in checks)
    outcome = "BLOCKED" if not ready else ("READY_WITH_LIMITATIONS" if any(c["result"] == WARN for c in checks)
                                           else "READY")
    report = {"schema_version": "1.0", "status": outcome, "ready": ready, "repository": str(repo),
              "checks": checks,
              # the environment contract's fields (§9), as measured here
              "execution_environment": {
                  "os": f"{platform.system()} {platform.release()}",
                  "windows_execution": platform.system() == "Windows",
                  "linux_execution": platform.system() == "Linux",
                  "disposable_workspace": any(c["what"].startswith("Disposable workspace") and c["result"] == OK
                                              for c in checks),
                  "cpu_limit": res.get("cpu_cores"), "memory_limit_mb": res.get("memory_mb"),
                  "disk_free_mb": res.get("disk_free_mb"), "timeout_minutes": res.get("job_timeout_minutes"),
                  "package_sources": [{"url": s["url"], "status": s.get("status")} for s in pf["package_sources"]],
                  "sdks": sdks, "runtimes": sorted(f"{n} {v}" for n, v in have),
                  "commercial_tools": "optional_only", "external_test_services": "explicit_contract"},
              "preflight": "preflight.json"}
    (out / "environment-capabilities.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                       encoding="utf-8")
    L = ["# Is this machine ready for the .NET 10 migration?", "",
         f"**{outcome.replace('_', ' ').capitalize()}.** "
         + ("Ready to run." if outcome == "READY" else "Ready, but the ⚠️ items limit what gets tested."
            if outcome == "READY_WITH_LIMITATIONS" else "Fix the ❌ items below first.") + "", "",
         f"Repository: `{repo}`", "", "| | Check | What to do |", "|---|---|---|"]
    order = {FAIL: 0, WARN: 1, OK: 2, INFO: 3}
    for c in sorted(checks, key=lambda c: order[c["result"]]):
        L.append(f"| {c['result']} | **{c['area']}**: {c['what']} | {c['fix']} |".replace("\n", " "))
    (out / "doctor-report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "preflight.json").write_text(json.dumps(pf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report
