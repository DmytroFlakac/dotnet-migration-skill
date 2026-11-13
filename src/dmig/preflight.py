"""Mandatory environment preflight.

Detects SDKs and runtimes, OS, resources, package-source reachability, git/worktree
support, external test services and licensed tools, then returns READY,
READY_WITH_LIMITATIONS or BLOCKED. Unknowns become explicit outputs, never assumptions.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from . import SCHEMA_VERSION, TARGET_SDK_MAJOR
from .dotnet import Runner

MIN_FREE_DISK_MB = 2048
SKIP_DIRS = {".git", "bin", "obj", "node_modules", ".vs", "TestResults"}


def _versions(output: str) -> list[dict]:
    """Parse `dotnet --list-sdks` / `--list-runtimes` lines."""
    items = []
    for line in output.splitlines():
        m = re.match(r"^(?:(\S+) )?(\d+\.\d+\.\d+\S*) \[(.+)\]$", line.strip())
        if m:
            name, version, root = m.groups()
            item = {"version": version, "root": root}
            if name:
                item["name"] = name
            items.append(item)
    return items


def _memory_mb() -> int | None:
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys / 1024 / 1024)
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001 - resource probes must never crash preflight
        return None
    return None


def iter_repo_files(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def package_sources(repo: Path) -> list[dict]:
    """Package sources declared in repository NuGet.config files (machine config is the adapter's)."""
    sources: dict[str, dict] = {}
    for path in iter_repo_files(repo):
        if path.name.lower() != "nuget.config":
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        section = root.find("packageSources")
        if section is None:
            continue
        for child in section:
            if child.tag == "clear":
                continue
            key, value = child.get("key"), child.get("value")
            if child.tag == "add" and key and value:
                sources[key] = {"name": key, "url": value,
                                "declared_in": path.relative_to(repo).as_posix()}
    return list(sources.values())


def _quick_platform_scan(repo: Path) -> bool:
    """True when any project needs Windows (TFM *-windows, WinForms, WPF)."""
    for path in iter_repo_files(repo):
        if path.suffix.lower() in {".csproj", ".fsproj", ".vbproj", ".props", ".targets"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"net\d+\.\d+-windows|<UseWindowsForms>\s*true|<UseWPF>\s*true", text, re.I):
                return True
    return False


PROBE_PACKAGE = "Microsoft.Extensions.Logging.Abstractions"  # on nuget.org and on most organisation mirrors


def neutral_query_dir(repo: Path, out_dir: Path) -> Path:
    """A folder outside the repository (no global.json pin) holding a copy of the repository's
    NuGet.config: NuGet combines it with the user and machine configuration, as restore does."""
    q = out_dir / "nuget-query"
    q.mkdir(parents=True, exist_ok=True)
    for name in ("NuGet.config", "nuget.config", "NuGet.Config"):
        if (repo / name).exists():
            (q / "NuGet.config").write_text((repo / name).read_text(encoding="utf-8-sig"), encoding="utf-8")
            break
    return q


def probe_sources_with_dotnet(runner: Runner, query_dir: Path, probe: bool = True) -> list[dict] | None:
    """Every enabled package source as NuGet itself sees it (repository + user + machine config),
    each probed through the SDK, so organisation credential providers, proxies and certificates apply.
    None when the SDK cannot list sources (then the HTTP probe is used)."""
    listed = runner.run(["nuget", "list", "source", "--format", "short"], query_dir, "nuget-sources", 120)
    if not listed.ok:
        return None
    sources = []
    for line in listed.output.splitlines():
        m = re.match(r"^\s*([ED])\s+(\S+)", line)
        if not m or m.group(1) != "E":
            continue
        url = m.group(2)
        src = {"name": url, "url": url, "origin": "nuget-configuration"}
        if not probe and url.lower().startswith("http"):
            src.update(status="NOT_PROBED", detail=f"not probed (--offline): would check {url} with "
                                                   f"`dotnet package search {PROBE_PACKAGE}`")
            sources.append(src)
            continue
        if not url.lower().startswith("http"):
            src.update(status="LOCAL_PATH" if Path(url).exists() else "FEED_UNREACHABLE",
                       detail="local folder" if Path(url).exists() else "local folder does not exist")
            sources.append(src)
            continue
        r = runner.run(["package", "search", PROBE_PACKAGE, "--exact-match", "--source", url, "--format", "json"],
                       query_dir, "feed-probe", 120)
        start = r.stdout.find("{")
        try:
            data = json.loads(r.stdout[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            data = {}
        problems = [str(p.get("text", p)) for p in data.get("problems", [])]
        for s in data.get("searchResult", []):
            problems += [str(p.get("text", p)) for p in (s.get("problems") or [])]
        text = " ".join(problems) or ("" if data else r.tail(3))
        src.update(classify_feed_problem(text))
        sources.append(src)
    return sources


def classify_feed_problem(text: str) -> dict:
    """What `dotnet package search --source <feed>` said, as one status per feed:
    401/403 (login or access), 404 (wrong URL), DNS (name not resolvable), timeout, or other network failure."""
    t = text[:200]
    if not text:
        return {"status": "OK", "detail": "reachable through the SDK (credentials and proxy as for restore)"}
    if re.search(r"\b401\b|unauthori[sz]ed|credential", text, re.I):
        return {"status": "AUTH_REQUIRED", "detail": "the SDK could not log in (401): " + t}
    if re.search(r"\b403\b|forbidden", text, re.I):
        return {"status": "FORBIDDEN", "detail": "access denied (403): " + t}
    if re.search(r"\b404\b|not ?found", text, re.I):
        return {"status": "NOT_FOUND", "detail": "the feed URL does not exist (404): " + t}
    if re.search(r"no such host|name or service not known|could not resolve|nodename nor servname|"
                 r"name resolution|host is not known|\bDNS\b", text, re.I):
        return {"status": "DNS_FAILURE", "detail": "the feed's host name cannot be resolved (DNS): " + t}
    if re.search(r"time(d)?[ -]?out|timeout", text, re.I):
        return {"status": "TIMEOUT", "detail": "no answer in time (timeout): " + t}
    return {"status": "FEED_UNREACHABLE", "detail": t}


def run_preflight(repo: Path, out_dir: Path, runner: Runner, probe_network: bool = True,
                  environment_contract: dict | None = None) -> dict:
    limitations: list[dict] = []
    blocking: list[dict] = []

    def limit(code: str, detail: str, affects: str) -> None:
        limitations.append({"code": code, "detail": detail, "affects": affects})

    def block(code: str, detail: str) -> None:
        blocking.append({"code": code, "detail": detail})

    # 1. SDKs and runtimes ------------------------------------------------------------
    sdks_run = runner.run(["--list-sdks"], repo, "list-sdks", 120)
    runtimes_run = runner.run(["--list-runtimes"], repo, "list-runtimes", 120)
    info_run = runner.run(["--info"], repo, "dotnet-info", 120)
    version_run = runner.run(["--version"], repo, "dotnet-version-in-repo", 120)
    sdks = _versions(sdks_run.output) if sdks_run.ok else []
    runtimes = _versions(runtimes_run.output) if runtimes_run.ok else []
    selected_sdk = version_run.output.strip().splitlines()[-1] if version_run.ok and version_run.output.strip() else None

    sdk10 = [s for s in sdks if s["version"].split(".")[0] == str(TARGET_SDK_MAJOR)]
    core_runtimes = [r for r in runtimes if r.get("name") == "Microsoft.NETCore.App"]
    has_rt = {major: any(r["version"].startswith(f"{major}.") for r in core_runtimes) for major in (8, 10)}
    # Web and most test hosts also need the ASP.NET Core shared framework of the same major
    # (seen on a real monorepo: .NET 8 tests aborted with only Microsoft.NETCore.App 8 installed).
    asp_runtimes = [r for r in runtimes if r.get("name") == "Microsoft.AspNetCore.App"]
    has_asp = {major: any(r["version"].startswith(f"{major}.") for r in asp_runtimes) for major in (8, 10)}

    if not sdks_run.ok:
        block("DOTNET_NOT_FOUND", f"`{runner.dotnet}` could not list SDKs (exit {sdks_run.exit_code}).")
    elif not sdk10:
        block("SDK10_UNAVAILABLE", "No .NET 10 SDK found; the migration cannot be validated.")
    if sdks_run.ok and not version_run.ok:
        block("GLOBAL_JSON_UNSATISFIED",
              "The SDK pinned by global.json is not installed; record it as a REPOSITORY_GLOBAL "
              "SDK-phase change instead of editing it. " + version_run.tail(3))
    elif selected_sdk and not selected_sdk.startswith(f"{TARGET_SDK_MAJOR}."):
        limit("REPO_SELECTS_OLDER_SDK", f"In the repository `dotnet --version` selects {selected_sdk}; "
              "the SDK-first phase (REPOSITORY_GLOBAL) must move global.json before any experiment.",
              "baseline, experiment")
    if not has_rt[8]:
        limit("RUNTIME8_UNAVAILABLE", "No .NET 8 runtime: net8.0 tests cannot run (baseline and "
              "multi-target validation are NOT_EXECUTED_ENVIRONMENT_BLOCKED).", "baseline tests")
    if not has_rt[10]:
        limit("RUNTIME10_UNAVAILABLE", "No .NET 10 runtime: net10.0 tests cannot run.", "experiment tests")

    # 2. OS and execution pools ---------------------------------------------------------
    os_name = platform.system()
    needs_windows = _quick_platform_scan(repo)
    platforms = [{"os": os_name, "release": platform.release(), "arch": platform.machine(), "local": True}]
    if needs_windows and os_name != "Windows":
        limit("WINDOWS_REQUIRED", "The repository has Windows-specific projects; this host is "
              f"{os_name}. They are labelled WINDOWS and not validated here.", "windows projects")

    # 3. Resources ------------------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    disk_repo = shutil.disk_usage(repo)
    disk_out = shutil.disk_usage(out_dir)
    resources = {
        "cpu_cores": os.cpu_count(),
        "memory_mb": _memory_mb(),
        "disk_free_mb": int(disk_repo.free / 1024 / 1024),
        "output_disk_free_mb": int(disk_out.free / 1024 / 1024),
        "disk_quota_mb": None,
        "job_timeout_minutes": None,
        "process_limit": None,
        "network_egress_policy": "unknown",
        "notes": "Quotas and egress policy are not observable locally; the adapter must supply them.",
    }
    if resources["disk_free_mb"] < MIN_FREE_DISK_MB:
        block("INSUFFICIENT_DISK", f"Only {resources['disk_free_mb']} MB free next to the repository; "
              f"at least {MIN_FREE_DISK_MB} MB is required for builds and the disposable worktree.")

    # 4. Package feeds ----------------------------------------------------------------------
    # The configured feeds are the allowed ones (restore uses them anyway), so each is probed by default
    # through the SDK; --offline skips it. No other address is ever contacted here.
    via_sdk = probe_sources_with_dotnet(runner, neutral_query_dir(repo, out_dir), probe=probe_network)
    # Without the SDK the feeds are only read from NuGet.config, never contacted: the tool has no HTTP
    # client of its own (all package traffic goes through dotnet and the configured feeds).
    sources = via_sdk if via_sdk is not None else [
        dict(s, status="NOT_PROBED", detail="the SDK could not list the feeds, so they were not checked")
        for s in package_sources(repo)]
    reachable = any(s["status"] in {"OK", "LOCAL_PATH"} for s in sources)
    probed = [s for s in sources if s["status"] != "NOT_PROBED"]
    for src in sources:
        if src["status"] in {"OK", "LOCAL_PATH", "NOT_PROBED"}:
            continue
        code = "FEED_AUTH_UNAVAILABLE" if src["status"] in {"AUTH_REQUIRED", "FORBIDDEN"} else "FEED_UNREACHABLE"
        text = f"Package source `{src['name']}`: {src.get('detail', src['status'])}."
        if reachable:  # another feed works: restore may still succeed, so this is a limitation
            limit(code, text, "restore of packages only this feed has")
        else:
            block(code, text)
    if probed and not reachable:
        limit("NO_REACHABLE_FEED", "No configured package feed answered: restore will fail here.", "restore, build, tests")
    if not sources and via_sdk is None:
        limit("NO_REPO_FEEDS", "No NuGet.config in the repository; restore relies on machine/user "
              "configuration supplied by the environment.", "restore")

    # 5. Git and disposable workspace ----------------------------------------------------------
    git_version = runner.git(["--version"], repo, "git-version")
    in_repo = runner.git(["rev-parse", "--is-inside-work-tree"], repo, "git-rev-parse")
    worktree = runner.git(["worktree", "list"], repo, "git-worktree-list") if in_repo.ok else None
    disposable = {
        "git_available": git_version.ok,
        "git_version": git_version.output.strip() if git_version.ok else None,
        "is_git_repository": in_repo.ok and in_repo.output.strip() == "true",
        "worktree_supported": bool(worktree and worktree.ok),
        "preferred_method": "git-worktree" if worktree and worktree.ok else "disposable-copy",
    }
    if not disposable["is_git_repository"]:
        limit("NOT_A_GIT_REPOSITORY", "No git repository: reversible experiments use a disposable "
              "copy and rollback is verified by file hashes.", "experiment")

    # 6. External test services and 7. licensed tools (from the environment contract only) -----
    contract = environment_contract or {}
    services = contract.get("external_test_services", [])
    licensed = contract.get("licensed_tools", [])
    if not services:
        limit("TEST_SERVICES_UNKNOWN", "No environment contract for databases, queues or other "
              "services: tests that need them are reported NOT_EXECUTED_ENVIRONMENT_BLOCKED.",
              "integration tests")

    for major in (8, 10):
        if has_rt[major] and not has_asp[major]:
            limit(f"ASPNETCORE_RUNTIME_{major}_MISSING",
                  f"Microsoft.NETCore.App {major} is installed but Microsoft.AspNetCore.App {major} is not: "
                  f"web projects and their tests cannot run on net{major}.0.", f"net{major}.0 web tests")

    status = "BLOCKED" if blocking else ("READY_WITH_LIMITATIONS" if limitations else "READY")
    capabilities = {
        "can_evaluate_msbuild": sdks_run.ok and version_run.ok,
        "can_build": sdks_run.ok and version_run.ok and not any(
            b["code"] in {"FEED_AUTH_UNAVAILABLE", "FEED_UNREACHABLE", "INSUFFICIENT_DISK"} for b in blocking),
        "can_test_net8": has_rt[8],
        "can_test_net10": has_rt[10] and bool(sdk10),
        "can_test_aspnet_net8": has_rt[8] and has_asp[8],
        "can_test_aspnet_net10": has_rt[10] and has_asp[10] and bool(sdk10),
        "can_build_windows_projects": os_name == "Windows",
        "can_experiment": bool(sdk10) and disposable["git_available"] and not any(
            b["code"] == "INSUFFICIENT_DISK" for b in blocking),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "dotnet_executable": runner.dotnet,
        "selected_sdk_in_repository": selected_sdk,
        "sdks": sdks,
        "runtimes": runtimes,
        "platforms": platforms,
        "repository_needs_windows": needs_windows,
        "package_sources": sources,
        "resources": resources,
        "disposable_workspace": disposable,
        "external_test_services": services,
        "licensed_tools": licensed,
        "capabilities": capabilities,
        "limitations": limitations,
        "blocking_items": blocking,
        "commands": [r.to_record() for r in (sdks_run, runtimes_run, info_run, version_run)],
    }
