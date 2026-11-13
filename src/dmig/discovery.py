"""Steps A2–A3: inventory every relevant file and evaluate MSBuild properties per project.

Properties come from `dotnet msbuild -getProperty/-getItem` (evaluated, inherited from
Directory.Build.props and central package management). If evaluation fails, the raw XML
is read instead and the project is marked `evaluated: false` (lower grouping confidence).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import SCHEMA_VERSION
from .dotnet import Runner
from .preflight import iter_repo_files

PROJECT_EXT = {".csproj", ".fsproj", ".vbproj"}
PROPERTIES = [
    "TargetFramework", "TargetFrameworks", "RuntimeIdentifier", "RuntimeIdentifiers",
    "LangVersion", "Nullable", "OutputType", "IsTestProject", "IsPackable", "UseWindowsForms",
    "UseWPF", "UsingMicrosoftNETSdkWeb", "UsingMicrosoftNETSdkWorker", "ManagePackageVersionsCentrally",
    "TreatWarningsAsErrors", "DirectoryBuildPropsPath", "DirectoryBuildTargetsPath",
    "DirectoryPackagesPropsPath", "EnableWindowsTargeting", "PublishAot", "PublishTrimmed",
    "IsAotCompatible", "UseMauiEssentials", "UseMaui",
]
ITEMS = ["ProjectReference", "PackageReference", "PackageVersion", "Analyzer"]
TEST_PACKAGES = {
    "microsoft.net.test.sdk": "vstest-host",
    "xunit": "xunit", "xunit.v3": "xunit.v3", "nunit": "nunit", "mstest.testframework": "mstest",
    "mstest": "mstest", "tunit": "tunit",
}
CI_NAMES = {".gitlab-ci.yml", "azure-pipelines.yml", "jenkinsfile", "build.cake", "build.ps1",
            "build.sh", "bitbucket-pipelines.yml", "appveyor.yml", ".travis.yml"}


def categorize(repo: Path) -> dict[str, list[str]]:
    """Every file the A2 list asks for, as repo-relative POSIX paths."""
    cats: dict[str, list[str]] = {k: [] for k in (
        "solutions", "solution_filters", "projects", "global_json", "directory_build_props",
        "directory_build_targets", "directory_packages_props", "nuget_config", "lock_files",
        "custom_props_targets", "ci_files", "dockerfiles", "publish_profiles", "test_config")}
    for path in iter_repo_files(repo):
        rel = path.relative_to(repo).as_posix()
        name, low = path.name, path.name.lower()
        suffix = path.suffix.lower()
        if suffix in {".sln", ".slnx"}:
            cats["solutions"].append(rel)
        elif suffix == ".slnf":
            cats["solution_filters"].append(rel)
        elif suffix in PROJECT_EXT:
            cats["projects"].append(rel)
        elif low == "global.json":
            cats["global_json"].append(rel)
        elif low == "directory.build.props":
            cats["directory_build_props"].append(rel)
        elif low == "directory.build.targets":
            cats["directory_build_targets"].append(rel)
        elif low == "directory.packages.props":
            cats["directory_packages_props"].append(rel)
        elif low == "nuget.config":
            cats["nuget_config"].append(rel)
        elif low == "packages.lock.json":
            cats["lock_files"].append(rel)
        elif suffix in {".props", ".targets"}:
            cats["custom_props_targets"].append(rel)
        elif low in CI_NAMES or (rel.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}):
            cats["ci_files"].append(rel)
        elif low.startswith("dockerfile") or low.endswith(".dockerfile"):
            cats["dockerfiles"].append(rel)
        elif suffix == ".pubxml":
            cats["publish_profiles"].append(rel)
        elif low.endswith(".runsettings") or low in {"xunit.runner.json", "testconfig.json"}:
            cats["test_config"].append(rel)
    return {k: sorted(v) for k, v in cats.items()}


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def read_raw_project(path: Path) -> dict:
    """Static facts from the project XML itself (SDK style, imports, raw properties)."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return {"parse_error": str(exc)}
    _strip_ns(root)
    sdk = root.get("Sdk") or next((el.get("Name") for el in root.iter("Sdk")), None)
    imports = [el.get("Project") for el in root.iter("Import") if el.get("Project")]
    raw_props = {}
    for group in root.iter("PropertyGroup"):
        for prop in group:
            if isinstance(prop.tag, str) and prop.text:
                raw_props.setdefault(prop.tag, prop.text.strip())
    items = {kind: [{"Identity": el.get("Include"), **({"Version": el.get("Version")} if el.get("Version") else {})}
                    for el in root.iter(kind) if el.get("Include")] for kind in ITEMS}
    return {"sdk": sdk, "sdk_style": sdk is not None, "imports": imports, "raw_properties": raw_props,
            "raw_items": items, "legacy_packages_config": (path.parent / "packages.config").exists()}


def evaluate(path: Path, repo: Path, runner: Runner) -> dict:
    args = ["msbuild", str(path), *[f"-getProperty:{p}" for p in PROPERTIES],
            *[f"-getItem:{i}" for i in ITEMS], "-nologo"]
    res = runner.run(args, repo, "eval-" + path.stem, timeout_s=300)
    if not res.ok:
        return {"evaluated": False, "evaluation_error": res.tail(5)}
    text = res.output
    start = text.find("{")
    try:
        data = json.loads(text[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        return {"evaluated": False, "evaluation_error": "unparseable msbuild output"}
    return {"evaluated": True, "properties": data.get("Properties", {}), "items": data.get("Items", {})}


def _tfms(props: dict) -> list[str]:
    """The frameworks MSBuild really builds: a non-empty TargetFramework wins over TargetFrameworks
    (the SDK then builds that single framework), e.g. one inherited from Directory.Build.props."""
    value = props.get("TargetFramework") or props.get("TargetFrameworks") or ""
    return [t.strip() for t in value.split(";") if t.strip()]


def _resolve_ref(project: Path, include: str, repo: Path) -> str | None:
    target = (project.parent / include.replace("\\", "/")).resolve()
    try:
        return target.relative_to(repo).as_posix()
    except ValueError:
        return None


def build_project_record(rel: str, repo: Path, evaluation: dict, raw: dict) -> dict:
    path = repo / rel
    props = evaluation.get("properties") if evaluation.get("evaluated") else raw.get("raw_properties", {})
    items = evaluation.get("items") if evaluation.get("evaluated") else raw.get("raw_items", {})
    props = props or {}
    items = items or {}
    tfms = _tfms(props)
    refs, unresolved = [], []
    for item in items.get("ProjectReference", []):
        resolved = _resolve_ref(path, item["Identity"], repo)
        (refs if resolved and (repo / resolved).exists() else unresolved).append(resolved or item["Identity"])
    central = {i["Identity"].lower(): i.get("Version") for i in items.get("PackageVersion", [])}
    packages = []
    for item in items.get("PackageReference", []):
        name = item["Identity"]
        version = item.get("VersionOverride") or item.get("Version") or central.get(name.lower())
        source = ("VersionOverride" if item.get("VersionOverride") else
                  "project" if item.get("Version") and name.lower() not in central else
                  "central" if name.lower() in central else "unknown")
        packages.append({"id": name, "version": version, "version_source": source})
    sdk = raw.get("sdk") or ""
    is_web = props.get("UsingMicrosoftNETSdkWeb", "").lower() == "true" or sdk.endswith(".Web")
    pkg_ids = {p["id"].lower() for p in packages}
    test_frameworks = sorted({v for k, v in TEST_PACKAGES.items() if k in pkg_ids})
    is_test = props.get("IsTestProject", "").lower() == "true" or "microsoft.net.test.sdk" in pkg_ids \
        or bool({"xunit.v3", "tunit", "mstest"} & pkg_ids)
    output_type = props.get("OutputType") or ("Exe" if is_web else "Library")
    windows = any(t.endswith("-windows") or "-windows" in t for t in tfms) \
        or props.get("UseWindowsForms", "").lower() == "true" or props.get("UseWPF", "").lower() == "true"
    if is_test:
        kind = "test"
    elif is_web or output_type.lower() in {"exe", "winexe"}:
        kind = "application"
    else:
        kind = "library"
    shared_files = {k: props.get(k) for k in ("DirectoryBuildPropsPath", "DirectoryBuildTargetsPath",
                                              "DirectoryPackagesPropsPath") if props.get(k)}
    shared_files = {k: _rel_or_abs(v, repo) for k, v in shared_files.items()}
    return {
        "id": rel,
        "name": path.stem,
        "language": {".csproj": "C#", ".fsproj": "F#", ".vbproj": "VB"}[path.suffix.lower()],
        "sdk": raw.get("sdk"),
        "sdk_style": raw.get("sdk_style", False),
        "legacy_packages_config": raw.get("legacy_packages_config", False),
        "evaluated": bool(evaluation.get("evaluated")),
        "evaluation_error": evaluation.get("evaluation_error"),
        "target_frameworks": tfms,
        "multi_targeted": len(tfms) > 1,
        "kind": kind,
        "output_type": output_type,
        "is_web": is_web,
        "platform": "WINDOWS" if windows else "ANY",
        "test_frameworks": test_frameworks,
        "properties": {k: props.get(k) for k in PROPERTIES if props.get(k)},
        "project_references": sorted(set(refs)),
        "unresolved_references": unresolved,
        "packages": packages,
        "raw_imports": raw.get("imports", []),
        "shared_build_files": shared_files,
    }


def _rel_or_abs(value: str, repo: Path) -> str:
    try:
        return Path(value).resolve().relative_to(repo).as_posix()
    except ValueError:
        return value


def parse_solution(path: Path, repo: Path) -> list[str]:
    """Projects listed in .sln, .slnx or .slnf files (repo-relative)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: list[str] = []
    if path.suffix.lower() == ".sln":
        entries = re.findall(r'^Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"([^"]+\.(?:cs|fs|vb)proj)"', text, re.M)
        base = path.parent
    elif path.suffix.lower() == ".slnx":
        entries = re.findall(r'<Project\s+Path="([^"]+\.(?:cs|fs|vb)proj)"', text)
        base = path.parent
    else:  # .slnf
        data = json.loads(text)
        entries = data.get("solution", {}).get("projects", [])
        base = (path.parent / data.get("solution", {}).get("path", "")).parent
    out = []
    for entry in entries:
        resolved = (base / entry.replace("\\", "/")).resolve()
        try:
            out.append(resolved.relative_to(repo).as_posix())
        except ValueError:
            out.append(entry)
    return sorted(set(out))


def run_inventory(repo: Path, runner: Runner, commit: str | None, evaluate_msbuild: bool = True,
                  workers: int = 4) -> dict:
    cats = categorize(repo)
    raws = {rel: read_raw_project(repo / rel) for rel in cats["projects"]}
    if evaluate_msbuild:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            evals = dict(zip(cats["projects"], pool.map(lambda r: evaluate(repo / r, repo, runner), cats["projects"])))
    else:
        evals = {rel: {"evaluated": False, "evaluation_error": "MSBuild evaluation not available"} for rel in cats["projects"]}
    projects = [build_project_record(rel, repo, evals[rel], raws[rel]) for rel in cats["projects"]]
    solutions = [{"path": s, "projects": parse_solution(repo / s, repo)}
                 for s in cats["solutions"] + cats["solution_filters"]]
    edges = [{"from": p["id"], "to": ref, "kind": "ProjectReference"}
             for p in projects for ref in p["project_references"]]
    unresolved_imports = []
    for p in projects:
        for imp in p["raw_imports"]:
            if "$(" not in imp and not (repo / p["id"]).parent.joinpath(imp).exists():
                unresolved_imports.append({"project": p["id"], "import": imp})
    return {
        "schema_version": SCHEMA_VERSION,
        "repository_commit": commit,
        "files": cats,
        "solutions": solutions,
        "projects": projects,
        "shared_build_files": cats["directory_build_props"] + cats["directory_build_targets"]
        + cats["custom_props_targets"] + cats["global_json"] + cats["nuget_config"],
        "central_package_files": cats["directory_packages_props"],
        "ci_entrypoints": cats["ci_files"],
        "project_reference_edges": edges,
        "unresolved_imports": unresolved_imports,
        "unresolved_project_references": [{"project": p["id"], "reference": r}
                                          for p in projects for r in p["unresolved_references"]],
    }
