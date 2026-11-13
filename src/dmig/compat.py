"""Step A8: package, framework and source compatibility findings (recipe rules = data).

A package is never called compatible because restore succeeds. Each finding
says what was checked, the evidence, and how sure we are.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .catalog import PackageCatalog, tfm_supports
from .preflight import iter_repo_files

NUGET_FLAT = "https://api.nuget.org/v3-flatcontainer"


def load_recipe(recipes_dir: Path, recipe_id: str = "dotnet-8-to-10") -> dict:
    recipe = yaml.safe_load((recipes_dir / f"{recipe_id}.yaml").read_text(encoding="utf-8"))
    rules = []
    for path in sorted((recipes_dir / recipe["transformations"]["source"]["rules_directory"]).glob("*.yaml")):
        rules += yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]
    recipe["rules"] = rules
    return recipe


def _ver(v: str | None) -> tuple:
    if not v:
        return ()
    core = re.split(r"[-+]", v.strip("[]() "))[0]
    return tuple(int(x) for x in core.split(".") if x.isdigit())


class NuGetClient(PackageCatalog):
    """Read-only package metadata, cached on disk.

    With a runner (the normal case) versions come from `dotnet package search`, run in a neutral
    folder that holds a copy of the repository's NuGet.config: NuGet then combines it with the user
    and machine configuration exactly as restore does, so organisation feeds, credential providers,
    proxies and certificates all apply, and nothing talks to the internet that restore would not.
    A package's supported frameworks are read from the local global packages folder first (the
    baseline restored them), and from nuget.org only for packages that come from nuget.org.
    Without a runner it falls back to the public nuget.org flat container (tests, old SDKs).
    """

    kind = "public-nuget"

    def __init__(self, cache_dir: Path, online: bool = True, runner=None, repo: Path | None = None):
        self.cache_dir, self.online, self.runner = cache_dir, online, runner
        self.failures: list[dict] = []  # every lookup that failed, reported as an uncertainty
        self.sources: dict[str, list[str]] = {}  # package id (lower) -> feeds it was found on
        self.search_unavailable = False
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.source = "configured NuGet feeds via dotnet" if runner is not None else "nuget.org (HTTP)"
        self.query_dir = cache_dir / "query"
        if runner is not None:
            self.query_dir.mkdir(parents=True, exist_ok=True)
            config = next((repo / n for n in ("NuGet.config", "nuget.config", "NuGet.Config") if repo and (repo / n).exists()),
                          None)
            if config:
                (self.query_dir / "NuGet.config").write_text(config.read_text(encoding="utf-8-sig"), encoding="utf-8")

    def _search(self, package: str) -> list[str] | None:
        """Versions on every configured feed, via the SDK (respects feeds, auth, proxy, certificates)."""
        pid = package.lower()
        cache = self.cache_dir / f"{pid}.search.json"
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
        elif not self.online:
            self.failures.append({"package": package, "error": "offline mode"})
            return None
        else:
            r = self.runner.run(["package", "search", package, "--exact-match", "--prerelease", "--format", "json"],
                                self.query_dir, "package-search", timeout_s=120)
            start = r.stdout.find("{")
            try:
                data = json.loads(r.stdout[start:]) if start >= 0 else None
            except json.JSONDecodeError:
                data = None
            if data is None:
                if "package" in r.output and ("not found" in r.output.lower() or "could not" in r.output.lower()):
                    self.search_unavailable = True  # SDK without `dotnet package search`
                self.failures.append({"package": package, "error": "dotnet package search: " + r.tail(3)})
                return None
            cache.write_text(json.dumps(data), encoding="utf-8")
        versions, feeds = [], []
        for src in data.get("searchResult", []):
            found = [p["version"] for p in src.get("packages", []) if p.get("id", "").lower() == pid]
            if found:
                feeds.append(src.get("sourceName", "?"))
                versions += found
        self.sources[pid] = feeds
        problems = [str(x) for x in data.get("problems", [])] + [
            f"{s.get('sourceName')}: {s.get('problems')}" for s in data.get("searchResult", []) if s.get("problems")]
        if not versions and problems:
            self.failures.append({"package": package, "error": "; ".join(problems)[:300]})
            return None
        return sorted(set(versions), key=_ver)

    def is_internal(self, package: str) -> bool:
        """Found only on feeds other than nuget.org (a organisation package): no public metadata exists."""
        feeds = self.sources.get(package.lower())
        return bool(feeds) and not any("nuget.org" in f.lower() for f in feeds)

    def _get(self, url: str, cache_name: str) -> str | None:
        cache = self.cache_dir / cache_name
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        if not self.online:
            self.failures.append({"url": url, "error": "offline mode"})
            return None
        last_error = None
        for attempt in range(2):  # one retry: transient DNS failures are common
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "dmig/1.0"}),
                                            timeout=20) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                cache.write_text(text, encoding="utf-8")
                return text
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code == 404:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = repr(getattr(exc, "reason", exc))
            if attempt == 0:
                time.sleep(3)
        self.failures.append({"url": url, "error": last_error})
        return None

    def versions(self, package: str) -> list[str] | None:
        if self.runner is not None and not self.search_unavailable:
            found = self._search(package)
            if found is not None or not self.search_unavailable:
                return found
        text = self._get(f"{NUGET_FLAT}/{package.lower()}/index.json", f"{package.lower()}.versions.json")
        return json.loads(text)["versions"] if text else None

    # ---- PackageCatalog interface ----------------------------------------
    def list_versions(self, package: str) -> list[str] | None:
        return self.versions(package)

    def get_compatibility(self, package: str, version: str, target_framework: str) -> bool | None:
        return tfm_supports(self.dependency_tfms(package, version) or [], target_framework)

    def get_dependencies(self, package: str, version: str, target_framework: str) -> list[dict] | None:
        text = self._nuspec(package, version)
        if text is None:
            return None
        groups = re.findall(r'<group[^>]*targetFramework="([^"]+)"[^>]*>(.*?)</group>', text, re.S)
        best = [body for tfm, body in groups if tfm_supports([tfm], target_framework)]
        body = best[-1] if best else ""
        return [{"id": i, "range": r} for i, r in re.findall(r'<dependency\s+id="([^"]+)"\s+version="([^"]+)"', body)]

    def get_metadata(self, package: str, version: str) -> dict | None:
        return {"id": package, "version": version, "catalog": self.kind,
                "sources": self.sources.get(package.lower(), []),
                "target_frameworks": self.dependency_tfms(package, version)}

    def _nuspec(self, package: str, version: str) -> str | None:
        pid, ver = package.lower(), version.lower()
        local = _global_packages() / pid / ver / f"{pid}.nuspec"
        if local.exists():
            return local.read_text(encoding="utf-8", errors="replace")
        if self.is_internal(package):
            return None
        return self._get(f"{NUGET_FLAT}/{pid}/{ver}/{pid}.nuspec", f"{pid}.{ver}.nuspec")

    def dependency_tfms(self, package: str, version: str) -> list[str] | None:
        """Target frameworks the package's nuspec declares dependency groups for: from the local
        global packages folder when restore already fetched it, else from nuget.org (public
        packages only; a organisation-feed package without a local copy is simply unknown)."""
        text = self._nuspec(package, version)
        if text is None:
            return None
        return sorted(set(re.findall(r'<group[^>]*targetFramework="([^"]+)"', text)))


def _global_packages() -> Path:
    """NuGet's global packages folder (NUGET_PACKAGES, else ~/.nuget/packages)."""
    import os
    return Path(os.environ.get("NUGET_PACKAGES") or Path.home() / ".nuget" / "packages")


def _latest_stable(versions: list[str], major: int) -> str | None:
    stable = [v for v in versions if "-" not in v and _ver(v)[:1] == (major,)]
    return max(stable, key=_ver) if stable else None


def _covers_net8(tfms: list[str]) -> bool:
    low = [t.lower() for t in tfms]
    return any(t in {"net8.0", ".netcoreapp8.0", "netstandard2.0", ".netstandard2.0", "netstandard2.1",
                     ".netstandard2.1"} for t in low)


def package_findings(inventory: dict, recipe: dict, nuget: PackageCatalog) -> tuple[list[dict], list[dict]]:
    """Returns (findings, central package strategies). Every version decision comes from the
    package catalogue (`nuget`): only approved versions are ever proposed."""
    prefixes = recipe["transformations"]["packages"]["coordinated_framework_prefixes"]
    rules = [r for r in recipe["rules"] if r["kind"] == "package"]
    usage: dict[tuple[str, str], list[str]] = {}
    for p in inventory["projects"]:
        for pkg in p["packages"]:
            usage.setdefault((pkg["id"], pkg["version"] or "?"), []).append(p["id"])
    central = bool(inventory["central_package_files"])
    findings, strategies = [], []
    restricted = getattr(nuget, "kind", "") == "fixture"   # an enterprise-mirror catalogue
    for (pid, version), projects in sorted(usage.items()):
        if restricted and version != "?":
            present = nuget.contains(pid)
            if present is False:
                findings.append(_catalog_finding("PKG-NOT-IN-CATALOGUE", pid, version, projects,
                    f"{pid} is not in the configured package catalogue ({nuget.source}): restore will fail there.",
                    "Stop, or use a recipe-approved alternative package; ask the catalogue owners to add it."))
            elif present and not nuget.is_approved(pid, version):
                approved = nuget.newest_allowed(pid)
                findings.append(_catalog_finding("PKG-VERSION-NOT-APPROVED", pid, version, projects,
                    f"{pid} {version} is not an approved version in the configured catalogue."
                    + (f" Newest approved: {approved}." if approved else " No version is approved."),
                    f"Human review: move to an approved version{(' (' + approved + ')') if approved else ''} or get "
                    f"{version} approved."))
        for rule in rules:
            hit = (rule.get("package", "").lower() == pid.lower()
                   and (not rule.get("version_below") or _ver(version) < _ver(rule["version_below"]))) \
                or pid.lower() in {x.lower() for x in rule.get("packages_any", [])}
            if hit:
                findings.append(_finding(rule, projects, evidence=f"{pid} {version}", package=pid))
        if any(pid.startswith(pref) for pref in prefixes) and _ver(version)[:1] == (8,):
            target = nuget.newest_allowed(pid, 10)
            tfms = (nuget.get_metadata(pid, target) or {}).get("target_frameworks") if target else None
            both = nuget.get_compatibility(pid, target, "net8.0") if target else None
            if target is None:
                strategy, why = "UNKNOWN", ("No approved 10.x version in the package catalogue, or the catalogue "
                                            "was not reachable; the version for net10.0 must be confirmed.")
            elif both:
                strategy, why = "ONE_VERSION_BOTH_TFMS", f"{pid} {target} declares assets for net8.0/netstandard as well as newer TFMs."
            else:
                strategy, why = "CONDITIONAL_PER_TFM", f"{pid} {target} does not declare net8.0 support; keep {version} for net8.0."
            strategies.append({"package": pid, "current_version": version, "net10_version": target,
                               "nuspec_tfms": tfms, "strategy": strategy, "reason": why,
                               "change_class": "REPOSITORY_GLOBAL" if central else "PROJECT_LOCAL",
                               "projects": projects})
            findings.append({
                "rule_id": "PKG-COORDINATED-FRAMEWORK", "title": f"{pid} is versioned with the framework",
                "kind": "package", "package": pid, "projects": projects, "evidence": f"{pid} {version}",
                "effect": "version_alignment", "failure_class": None,
                "detail": "8.x packages still run on net10.0; aligning them with the framework is a "
                          "convergence step (PHASE_6), required earlier only if the experiment shows a failure.",
                "repair": {"id": "coordinated-package-alignment", "change_class": strategies[-1]["change_class"],
                           "action": f"PHASE_6 alignment, strategy {strategy}: {why}"},
                "confidence": "high" if target else "low", "reference": None})
        elif not any(f.get("package") == pid for f in findings):
            versions_tfms = (nuget.get_metadata(pid, version) or {}).get("target_frameworks") if version != "?" else None
            findings.append({
                "rule_id": "PKG-NOT-PROVEN", "title": f"{pid} {version}: no known net10.0 issue",
                "kind": "package", "package": pid, "projects": projects, "evidence": f"nuspec TFMs: {versions_tfms}",
                "effect": "none_known", "failure_class": None, "repair": None, "confidence": "medium",
                "note": "Not proven compatible: confirmed only when the net10.0 experiment builds and tests pass.",
                "reference": None})
    return findings, strategies


def _catalog_finding(rule_id: str, pid: str, version: str, projects: list[str], detail: str, action: str) -> dict:
    return {"rule_id": rule_id, "title": detail.split(":")[0] if rule_id == "PKG-NOT-IN-CATALOGUE"
            else f"{pid} {version} is not approved in the package catalogue",
            "kind": "package", "package": pid, "projects": projects, "evidence": f"{pid} {version}",
            "effect": "build_break" if rule_id == "PKG-NOT-IN-CATALOGUE" else "decision",
            "failure_class": "unavailable_package_version", "detail": detail,
            "repair": {"id": "catalogue-decision", "change_class": "PROJECT_LOCAL",
                       "action": action if action.startswith("Human review") else "Human review: " + action},
            "confidence": "high", "reference": None}


def _finding(rule: dict, projects: list[str], evidence: str, **extra) -> dict:
    return {"rule_id": rule["id"], "title": rule["title"], "kind": rule["kind"], "projects": projects,
            "evidence": evidence, "effect": rule["effect"], "failure_class": rule.get("failure_class"),
            "expected_diagnostics": rule.get("expected_diagnostics", []), "detail": rule.get("detail"),
            "repair": rule.get("repair"), "confidence": "high", "reference": rule.get("reference"), **extra}


def _glob_match(rel: str, glob: str) -> bool:
    """fnmatch with `**/` meaning "at any depth, including the root" and `{a,b}` alternatives."""
    brace = re.search(r"\{([^{}]*)\}", glob)
    if brace:
        return any(_glob_match(rel, glob[:brace.start()] + alt + glob[brace.end():])
                   for alt in brace.group(1).split(","))
    if glob.startswith("**/"):
        tail = glob[3:]
        return fnmatch.fnmatch(rel, tail) or fnmatch.fnmatch(rel, "*/" + tail)
    return fnmatch.fnmatch(rel, glob)


def _owner_project(file_rel: str, projects: list[dict]) -> str | None:
    best = None
    for p in projects:
        folder = p["id"].rsplit("/", 1)[0] + "/" if "/" in p["id"] else ""
        if file_rel.startswith(folder) and (best is None or len(folder) > len(best[0])):
            best = (folder, p["id"])
    return best[1] if best else None


def source_and_file_findings(repo: Path, inventory: dict, recipe: dict) -> list[dict]:
    rules = [r for r in recipe["rules"] if r["kind"] in {"source", "file"}]
    compiled = [(r, re.compile(r["pattern"], re.M)) for r in rules]
    findings = []
    for path in iter_repo_files(repo):
        rel = path.relative_to(repo).as_posix()
        for rule, rx in compiled:
            globs = [rule["glob"]] if rule["kind"] == "source" else rule["files"]
            if not any(_glob_match(rel, g) for g in globs):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [text.count("\n", 0, m.start()) + 1 for m in rx.finditer(text)]
            if lines:
                owner = _owner_project(rel, inventory["projects"])
                findings.append(_finding(rule, [owner] if owner else [], evidence=f"{rel}:{','.join(map(str, lines[:10]))}",
                                         file=rel, lines=lines))
    return findings


def global_json_findings(repo: Path, inventory: dict, selected_sdk: str | None) -> list[dict]:
    out = []
    for rel in inventory["files"]["global_json"]:
        try:
            data = json.loads((repo / rel).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append({"rule_id": "GLOBAL-JSON-INVALID", "title": f"{rel} cannot be read: {exc}", "kind": "file",
                        "projects": [], "effect": "blocker", "failure_class": "sdk_not_found", "repair": None,
                        "confidence": "high", "evidence": rel, "reference": None})
            continue
        sdk = data.get("sdk", {})
        version = sdk.get("version")
        major = _ver(version)[:1]
        ok = major == (10,) or (major and major > (10,))
        out.append({
            "rule_id": "GLOBAL-JSON-SDK", "kind": "file", "file": rel, "projects": [],
            "title": f"{rel} pins SDK {version or '(none)'} with rollForward {sdk.get('rollForward', '(default: latestPatch)')}",
            "evidence": f"selected SDK in repository: {selected_sdk}",
            "effect": "none" if ok else "sdk_phase_required",
            "failure_class": None if ok else "sdk_not_found",
            "repair": None if ok else {"id": "sdk-global-phase", "change_class": "REPOSITORY_GLOBAL",
                                       "action": "PHASE_1: move global.json to a .NET 10 SDK with an explicit rollForward policy, then prove the unchanged net8.0 repository builds (PHASE_2)."},
            "confidence": "high", "reference": "https://learn.microsoft.com/dotnet/core/tools/global-json"})
    return out


def technologies(inventory: dict) -> set[str]:
    """Which technology areas the repository uses (decides which checklist entries apply)."""
    used = {"any"}
    for p in inventory["projects"]:
        props = p.get("properties", {})
        pkgs = {pk["id"].lower() for pk in p["packages"]}
        if p.get("is_web") or any(x.startswith("microsoft.aspnetcore") for x in pkgs):
            used.add("web")
        if any(x.startswith("microsoft.entityframeworkcore") for x in pkgs):
            used.add("efcore")
        if str(props.get("UseWindowsForms", "")).lower() == "true":
            used.add("winforms")
        if str(props.get("UseWPF", "")).lower() == "true":
            used.add("wpf")
        if p["id"].lower().endswith(".vbproj"):
            used.add("vb")
    if inventory["files"].get("dockerfiles"):
        used.add("containers")
    return used


def review_checklist(inventory: dict, recipe: dict) -> list[dict]:
    """Catalogue entries that cannot be detected mechanically but apply to what this repository uses."""
    used = technologies(inventory)
    return [{"rule_id": r["id"], "title": r["title"], "area": r["area"], "change_type": r.get("change_type"),
             "introduced_in": r.get("introduced_in"), "reference": r["reference"]}
            for r in recipe["rules"] if r["kind"] == "checklist" and r.get("applies_if", "any") in used]


def run_compatibility(repo: Path, inventory: dict, recipe: dict, nuget: NuGetClient,
                      selected_sdk: str | None) -> dict:
    pkg, strategies = package_findings(inventory, recipe, nuget)
    findings = pkg + source_and_file_findings(repo, inventory, recipe) + global_json_findings(repo, inventory, selected_sdk)
    legacy = [p["id"] for p in inventory["projects"] if not p["sdk_style"] or p["legacy_packages_config"]]
    for pid in legacy:
        findings.append({"rule_id": "PROJECT-LEGACY-STYLE", "title": "Legacy (non-SDK) project", "kind": "project",
                         "projects": [pid], "effect": "blocker", "failure_class": "custom_msbuild_failure",
                         "repair": None, "confidence": "high", "evidence": pid, "reference": None,
                         "detail": "SDK-style conversion is out of scope for this recipe."})
    analyzers = sorted({pk["id"] for p in inventory["projects"] for pk in p["packages"]
                        if re.search(r"analyzers?$|generator|stylecop|roslynator|sonar", pk["id"], re.I)})
    checklist = review_checklist(inventory, recipe)
    return {"findings": findings, "central_package_strategies": strategies, "review_checklist": checklist,
            "nuget_lookup_failures": nuget.failures,
            "package_catalog": nuget.describe(),
            "analyzer_and_generator_packages": analyzers,
            "automatic_vs_human": {
                "detected_automatically": sorted({f["rule_id"] for f in findings if f.get("effect") not in {"none", "none_known"}}),
                "needs_human_review": sorted({f["rule_id"] for f in findings
                                              if (f.get("repair") or {}).get("action", "").startswith("Human review")}),
            }}


PublicNuGetCatalog = NuGetClient  # the contract's name for the public implementation
