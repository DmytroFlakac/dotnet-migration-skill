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
import re
import shutil
import sys
from pathlib import Path

from .dotnet import Runner
from .preflight import iter_repo_files, run_preflight
from .secrets import redact

# Plain words, never emoji: Windows consoles use cp1252.
OK, WARN, FAIL, INFO = "PASS", "WARN", "BLOCK", "INFO"
LABEL = {OK: OK, WARN: WARN, FAIL: FAIL, INFO: INFO}


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


GLOBAL_FILES = ("global.json", "Directory.Build.props", "Directory.Build.targets", "Directory.Packages.props",
                "NuGet.config", "nuget.config", ".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile")
WINDOWS_ONLY = re.compile(r"<TargetFrameworks?>[^<]*net\d+\.\d+-windows|<UseWindowsForms>\s*true|<UseWPF>\s*true", re.I)


def _windows_only_projects(repo: Path, limit: int = 2000) -> list[str]:
    found = []
    for i, path in enumerate(iter_repo_files(repo)):
        if i > 200000:
            break
        if path.suffix.lower() in {".csproj", ".vbproj", ".fsproj"}:
            try:
                if WINDOWS_ONLY.search(path.read_text(encoding="utf-8", errors="replace")):
                    found.append(path.relative_to(repo).as_posix())
            except OSError:
                pass
            if len(found) >= limit:
                break
    return found


def _longest_path(repo: Path) -> int:
    """Longest path (characters) below the repository, relative to it."""
    longest = 0
    for path in iter_repo_files(repo):
        longest = max(longest, len(str(path.relative_to(repo))))
    return longest


def _workloads(runner: Runner, repo: Path) -> tuple[bool, list[str]]:
    r = runner.run(["workload", "list"], repo.parent if repo.parent.exists() else repo, "workload-list", 120)
    names = [m.group(1) for line in r.output.splitlines() if (m := re.match(r"^\s*([a-z][\w-]+)\s+\d+\.\d", line))]
    return r.ok, names


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


def run_doctor(repo: Path, out: Path, runner: Runner, allow_network: bool = False,
               catalog_spec: str | None = None, probe_feeds: bool = True, dotnet_info: dict | None = None) -> dict:
    """allow_network: the public nuget.org catalogue may be used (--allow-public-nuget).
    probe_feeds: check each configured feed through the SDK (default; --offline skips it)."""
    out.mkdir(parents=True, exist_ok=True)
    pf = run_preflight(repo, out, runner, probe_network=probe_feeds)
    checks: list[dict] = []

    def add(area: str, mark: str, what: str, fix: str = "") -> None:
        checks.append({"area": area, "result": mark, "status": LABEL[mark], "what": what, "fix": fix})

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
            # A capability, not a blocker: without runtime 8 the net8.0 tests are
            # reported NOT_EXECUTED_ENVIRONMENT_BLOCKED, never passed.
            add(".NET", OK if ok else WARN, f"{label} {major}: {'installed' if ok else 'missing'}",
                "" if ok else f"net{major}.0 tests{' of web projects' if 'Asp' in name else ''} cannot run here; they "
                              f"are reported as not run (NOT_EXECUTED_ENVIRONMENT_BLOCKED), never as passed. If it is "
                              f"installed in another dotnet folder, pass --dotnet-root <folder> (or set "
                              f"DMIG_DOTNET_ROOTS); otherwise ask IT for the {label} {major}.")
    if (dotnet_info or {}).get("combined"):
        add(".NET", INFO, "Runtimes from other dotnet folders: " + ", ".join(
            f".NET {m} from `{where}`" for m, where in dotnet_info["found_in"].items()),
            f"Used through a combined folder `{dotnet_info['combined']}` that links them with the SDK of "
            f"`{dotnet_info['active']}`; no installation is changed.")
    if any(b["code"] == "GLOBAL_JSON_UNSATISFIED" for b in pf["blocking_items"]):
        add(".NET", INFO, "The repository's global.json asks for an SDK that is not installed.",
            "Normal before the migration: the analysis uses a throwaway copy moved to SDK 10.")

    # Package catalogue and network ---------------------------------------------------------------------
    from .catalog import load_catalog
    cat = load_catalog(catalog_spec, out / "nuget-cache", allow_network=allow_network).describe()
    add("Packages", OK if cat["kind"] == "local" and catalog_spec else (WARN if cat["kind"] == "local" else INFO),
        f"Package catalogue: {cat['kind']} ({cat['source']})",
        "" if catalog_spec else "Give the mirror's catalogue with --package-catalog <file.json>; without it, package "
                                "versions are reported as unknown, never guessed.")
    add("Network", INFO if not allow_network else WARN,
        "Public nuget.org catalogue: " + ("not used (default)" if not allow_network
                                          else "allowed (--allow-public-nuget)"),
        "The tool contacts only the configured feeds, through `dotnet`; restore/build use the same feeds.")
    would = [s["url"] for s in pf["package_sources"] if s.get("status") == "NOT_PROBED"]
    if would:
        add("Network", INFO, "Feeds not checked (--offline): " + ", ".join(f"`{u}`" for u in would),
            "Run without --offline to check that these feeds are reachable and the login works.")

    # Package feeds ---------------------------------------------------------------------------------
    reachable_feed = any(s.get("status") in {"OK", "LOCAL_PATH"} for s in pf["package_sources"])
    if not pf["package_sources"]:
        add("Packages", WARN, "No package feed found in the NuGet configuration.",
            "Restore will fail. Ask how this machine is meant to get NuGet packages.")
    for s in pf["package_sources"]:
        st = s.get("status")
        fix = {"AUTH_REQUIRED": "The SDK could not log in to this feed. Install/sign in to the organisation's NuGet "
                                "credential provider (the same login `dotnet restore` needs).",
               "FORBIDDEN": "Your account has no access to this feed. Ask the feed owner for read access.",
               "NOT_FOUND": "The feed address is wrong or the feed was removed: check the URL in NuGet.config.",
               "DNS_FAILURE": "This machine cannot resolve the feed's host name. Check VPN/proxy, or ask IT whether "
                              "the feed is meant to be used from this machine.",
               "TIMEOUT": "The feed did not answer in time. Check VPN/proxy, or retry later.",
               "FEED_UNREACHABLE": "The machine cannot reach this feed. Check VPN/proxy, or ask IT whether it "
                                   "is allowed from this machine."}.get(st, "")
        if st == "NOT_PROBED":
            continue  # listed above as "not checked"
        # One unreachable feed among working ones is a limitation; no reachable feed at all blocks restore.
        add("Packages", OK if st in {"OK", "LOCAL_PATH"} else (WARN if reachable_feed else FAIL),
            f"Feed `{s['url']}`: {s.get('detail', st)}", fix)
    proxies = {k: redact(v) for k, v in os.environ.items() if k.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}}
    add("Network", INFO, "Proxy settings: " + (", ".join(f"{k}={v}" for k, v in proxies.items()) or "none set"),
        "The skill reaches feeds only through `dotnet`, which uses the same proxy and certificates as restore.")

    # git -----------------------------------------------------------------------------------------
    git_ok = pf["disposable_workspace"]["git_available"]
    in_git = pf["disposable_workspace"]["is_git_repository"]
    add("git", OK if git_ok else FAIL, pf["disposable_workspace"].get("git_version") or "git not found",
        "" if git_ok else "Install git (2.20+); the skill makes throwaway copies with git worktrees.")
    if git_ok and not in_git:
        add("git", FAIL, f"`{repo}` is not a git repository, so no throwaway copy (git worktree) can be made.",
            "Point dmig at a git clone of the repository. For a plain folder (e.g. samples/synthetic-monorepo), make "
            "a git copy first: copy the folder somewhere short (e.g. C:\\w\\sample), then run there "
            "`git init`, `git add -A` and `git -c user.name=dmig -c user.email=dmig@example.invalid commit -m base`. "
            "The test suite does this by itself.")
    elif git_ok:
        add("git", OK if pf["disposable_workspace"]["worktree_supported"] else FAIL, "git worktrees: "
            + ("supported" if pf["disposable_workspace"]["worktree_supported"] else "not available here"),
            "" if pf["disposable_workspace"]["worktree_supported"] else
            "git could not create a worktree here; check folder permissions and the git version (2.20+).")
        dirty = runner.git(["status", "--porcelain", "--untracked-files=no"], repo, "git-status").output.strip()
        add("git", OK if not dirty else WARN, "Working tree: " + ("clean" if not dirty else
                                                                  f"{len(dirty.splitlines())} uncommitted change(s)"),
            "" if not dirty else "The analysis uses the last commit, not uncommitted edits. Commit or stash them "
                                 "first if they should be part of the migration.")
        crlf = runner.git(["config", "--get", "core.autocrlf"], repo, "git-autocrlf").output.strip() or "not set"
        add("git", INFO, f"core.autocrlf = {crlf}", "Patches use this same setting, so they apply cleanly here.")
        if os.name == "nt":
            lp = runner.git(["config", "--get", "core.longpaths"], repo, "git-longpaths").output.strip()
            add("git", OK if lp == "true" else WARN, f"core.longpaths = {lp or 'not set'}",
                "" if lp == "true" else "Deep folders can exceed 260 characters: run `git config --global core.longpaths true`.")
    on_windows = platform.system() == "Windows"
    add("Windows", OK if on_windows else WARN, "Windows: " + ("available" if on_windows else "not available"),
        "" if on_windows else "Groups that need Windows (net*-windows, WPF/WinForms, COM, Windows services) are "
                              "analysed but reported BLOCKED_PLATFORM instead of being validated.")
    if os.name == "nt":
        lpe = _long_paths_enabled()
        add("Windows", OK if lpe else WARN, f"Windows long paths: {'enabled' if lpe else 'disabled'}",
            "" if lpe else "Builds in deep folders may fail with 'path too long'. Ask IT to enable LongPathsEnabled, "
                           "or keep the repository and output folders short (e.g. C:\\w\\repo).")
        if not lpe:
            # The throwaway copy lives in <out>/worktree; build output adds ~60 characters (obj\Debug\net10.0\...).
            deepest = _longest_path(repo) + len(str(out / "worktree")) + 1 + 60
            add("Windows", WARN if deepest >= 260 else OK,
                f"Path-length risk: deepest expected path about {deepest} characters (limit 260 without long paths)",
                "" if deepest < 260 else "Use a shorter output folder (e.g. --out C:\\w\\o) or a shorter clone path.")
    win_only = _windows_only_projects(repo)
    if win_only:
        add("Windows", OK if on_windows else WARN,
            f"{len(win_only)} Windows-only project(s) (net*-windows, WPF, WinForms): "
            + ", ".join(f"`{p}`" for p in win_only[:5]) + (" ..." if len(win_only) > 5 else ""),
            "" if on_windows else "They are analysed but reported BLOCKED_PLATFORM: validate them on a Windows agent.")
    ok_wl, workloads = _workloads(runner, repo)
    add(".NET", INFO, "Workloads: " + (", ".join(workloads) if workloads else ("none installed" if ok_wl else "could not list")),
        "Projects that need a workload (MAUI, Android, wasm-tools...) are report-only; nothing is installed.")
    wanted = {f.lower() for f in GLOBAL_FILES}  # real names only: Windows matches any case
    present = sorted(n for n in os.listdir(repo) if n.lower() in wanted) if repo.is_dir() else []
    present += [p.relative_to(repo).as_posix() for p in sorted((repo / ".github" / "workflows").glob("*.y*ml"))]
    add("Repository", INFO, "Shared/global files: " + (", ".join(f"`{f}`" for f in present) if present else "none at the root"),
        "These change only in an approved global phase (dmig global --approve-global), never in a group run.")

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
    # Estimate: a worktree copy of the sources plus build output (~10x the tracked sources) and the restore.
    work_gb = round(max(2.0, (size[1] if size else 500) * 11 / 1024), 1)
    for label, path, need in (("output drive (worktree + build)", out, work_gb),
                              ("NuGet packages (restore output)", nuget_dir, 3), ("temp folder", temp_dir, 1)):
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
    # Named capabilities: tests and results use these to say what is missing.
    capabilities = {"sdk10": any(v.startswith("10.") for v in sdks),
                    "runtime8": ("Microsoft.NETCore.App", "8") in have,
                    "aspnetcore_runtime8": ("Microsoft.AspNetCore.App", "8") in have,
                    "reachable_configured_feed": reachable_feed,
                    "git_repository": in_git,
                    "git_worktree": bool(in_git and pf["disposable_workspace"]["worktree_supported"]),
                    "windows": on_windows}
    report = {"schema_version": "1.0", "status": outcome, "ready": ready, "repository": str(repo),
              "capabilities": capabilities,
              "missing_capabilities": sorted(k for k, v in capabilities.items() if not v),
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
                  "package_catalog": cat, "network_allowed": allow_network,
                  "sdks": sdks, "runtimes": sorted(f"{n} {v}" for n, v in have),
                  "commercial_tools": "optional_only", "external_test_services": "explicit_contract"},
              "preflight": "preflight.json"}
    (out / "environment-capabilities.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                       encoding="utf-8")
    L = ["# Is this machine ready for the .NET 10 migration?", "",
         f"**{outcome.replace('_', ' ').capitalize()}.** "
         + ("Ready to run." if outcome == "READY" else "Ready, but the WARN items limit what gets tested."
            if outcome == "READY_WITH_LIMITATIONS" else "Fix the BLOCK items below first.") + "", "",
         f"Repository: `{repo}`", "", "| Result | Check | What to do |", "|---|---|---|"]
    order = {FAIL: 0, WARN: 1, OK: 2, INFO: 3}
    for c in sorted(checks, key=lambda c: order[c["result"]]):
        L.append(f"| {c['status']} | **{c['area']}**: {c['what']} | {c['fix']} |".replace("\n", " "))
    (out / "doctor-report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "preflight.json").write_text(json.dumps(pf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report
