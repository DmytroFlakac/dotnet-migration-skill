"""Several .NET installations side by side.

A machine often has the .NET 10 SDK in one folder and the .NET 8 runtime in another (installed by a
different tool, unpacked by hand, or per user). `dotnet` itself only looks in its own folder, so the
net8.0 tests would not run although .NET 8 is on the machine.

This module finds the runtimes in every known dotnet folder (plus the ones the user names with
--dotnet-root or DMIG_DOTNET_ROOTS) and, when the active dotnet lacks a runtime another folder has,
builds a *combined root*: a small folder outside the repository whose `sdk`, `host` and other
entries point at the active installation and whose `shared/<framework>/<version>` entries point at
every installation that has them. Nothing is copied except the `dotnet` executable itself, and no
installation is changed. Links are directory junctions on Windows (no admin rights needed) and
symbolic links elsewhere.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

EXE = "dotnet.exe" if os.name == "nt" else "dotnet"
MERGED = ("shared", "packs")  # <entry>/<name>/<version>: combined from every installation


def candidate_roots(extra: list[str] | None = None) -> list[Path]:
    """Every folder that may hold a dotnet installation, the user's own first."""
    names = list(extra or [])
    names += [p for p in os.environ.get("DMIG_DOTNET_ROOTS", "").split(os.pathsep) if p]
    names += [os.environ.get(v, "") for v in ("DOTNET_ROOT", "DOTNET_ROOT_X64", "DOTNET_INSTALL_DIR")]
    home = Path.home()
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            if os.environ.get(var):
                names.append(str(Path(os.environ[var]) / "dotnet"))
        if os.environ.get("LOCALAPPDATA"):
            names.append(str(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "dotnet"))
        names.append(_registry_root() or "")
    else:
        names += ["/usr/share/dotnet", "/usr/lib/dotnet", "/usr/lib64/dotnet", "/opt/dotnet", "/usr/local/share/dotnet"]
    names.append(str(home / ".dotnet"))
    on_path = shutil.which("dotnet")
    if on_path:
        names.append(str(Path(on_path).resolve().parent))
    seen, roots = set(), []
    for n in names:
        if not n:
            continue
        p = Path(n).expanduser()
        if p.is_file():
            p = p.parent
        key = str(p).lower() if os.name == "nt" else str(p)
        if key not in seen and (p / EXE).exists():
            seen.add(key)
            roots.append(p)
    return roots


def _registry_root() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\dotnet\Setup\InstalledVersions\x64") as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return None


def runtimes(root: Path) -> dict[str, list[str]]:
    """{framework name: [versions]} installed in one root, e.g. {"Microsoft.NETCore.App": ["8.0.11"]}."""
    found: dict[str, list[str]] = {}
    shared = root / "shared"
    if shared.is_dir():
        for fw in sorted(shared.iterdir()):
            if fw.is_dir():
                found[fw.name] = sorted(v.name for v in fw.iterdir() if v.is_dir())
    return found


def has_runtime(root: Path, major: str, framework: str = "Microsoft.NETCore.App") -> bool:
    return any(v.split(".")[0] == major for v in runtimes(root).get(framework, []))


def _link(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def combined_root(active: Path, others: list[Path], cache_dir: Path) -> Path:
    """A root that runs `active`'s SDK with the runtimes of all roots (active's version wins a tie)."""
    roots = [active] + [r for r in others if r.resolve() != active.resolve()]
    key = hashlib.sha256("|".join(str(r.resolve()) for r in roots).encode()).hexdigest()[:12]
    dest = cache_dir / f"dotnet-combined-{key}"
    if (dest / EXE).exists():
        return dest
    tmp = cache_dir / f".building-{key}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for entry in active.iterdir():
        if entry.name in MERGED:
            continue
        if entry.is_dir():
            _link(entry.resolve(), tmp / entry.name)
        else:
            shutil.copy2(entry, tmp / entry.name)  # dotnet.exe and small text files
    for top in MERGED:
        for root in roots:
            base = root / top
            if not base.is_dir():
                continue
            for name in base.iterdir():
                if not name.is_dir():
                    continue
                for version in name.iterdir():
                    link = tmp / top / name.name / version.name
                    if version.is_dir() and not link.exists():
                        link.parent.mkdir(parents=True, exist_ok=True)
                        _link(version.resolve(), link)
    tmp.rename(dest)
    return dest


def resolve(dotnet: str | None, extra_roots: list[str] | None = None, cache_dir: Path | None = None,
            need: tuple[str, ...] = ("8", "10")) -> tuple[str, dict]:
    """The dotnet to use, and what was done. When the active installation lacks a needed runtime
    major that another installation has, a combined root is built (see the module docstring)."""
    active_exe = dotnet or os.environ.get("DMIG_DOTNET") or shutil.which("dotnet")
    info: dict = {"active": active_exe, "combined": None, "found_in": {}}
    if not active_exe or not Path(active_exe).exists():
        return dotnet or "dotnet", info
    active = Path(active_exe).resolve().parent
    missing = [m for m in need if not has_runtime(active, m)]
    if not missing:
        return active_exe, info
    others = [r for r in candidate_roots(extra_roots) if r.resolve() != active]
    helpers = [r for r in others if any(has_runtime(r, m) for m in missing)]
    for m in missing:
        holder = next((r for r in helpers if has_runtime(r, m)), None)
        if holder:
            info["found_in"][m] = str(holder)
    if not helpers:
        return active_exe, info
    cache = cache_dir or Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "dmig"
    try:
        root = combined_root(active, helpers, cache)
    except OSError as exc:  # e.g. links not allowed here: keep the active dotnet, report why
        info["error"] = f"could not combine installations: {exc}"
        return active_exe, info
    info["combined"] = str(root)
    return str(root / EXE), info
