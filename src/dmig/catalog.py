"""Package catalogue: the only place the migration engine learns which package versions exist,
which are approved, and which target frameworks they support.

    PackageCatalog                 the interface (list_versions, is_available, is_approved,
                                   get_compatibility, get_dependencies, get_metadata)
    PublicNuGetCatalog             the configured NuGet feeds, queried through `dotnet` (compat.NuGetClient):
                                   every available version counts as approved
    FixturePackageCatalog          a JSON file that emulates an enterprise mirror, with approved versions

The engine never invents or forces a version the configured catalogue does not expose:
`newest_allowed()` chooses only among approved (or, without an approval list, available) versions.
An enterprise adapter later implements the same interface over the organisation's catalogue service.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def ver_key(v: str) -> tuple:
    core = re.split(r"[-+]", v.strip("[]() "))[0]
    return tuple(int(x) for x in core.split(".") if x.isdigit())


def tfm_supports(declared: list[str], target: str) -> bool | None:
    """Does a package that declares these target frameworks run on `target` (e.g. net8.0)?
    None when the package declares none (no dependency groups: the nuspec cannot tell)."""
    if not declared:
        return None
    want = ver_key(target.replace("net", "", 1)) if target.startswith("net") and not target.startswith("netstandard") else ()
    for t in (x.lower().lstrip(".") for x in declared):
        if t.startswith("netstandard2"):
            return True
        m = re.match(r"net(?:coreapp)?(\d+\.\d+)$", t)
        if m and want and ver_key(m.group(1)) <= want:
            return True
    return False


class PackageCatalog:
    """Interface. `None` from a query means "unknown" (lookup failed), never "no"."""
    kind = "abstract"
    source = ""
    failures: list[dict]

    def list_versions(self, package: str) -> list[str] | None:
        raise NotImplementedError

    def is_available(self, package: str, version: str) -> bool | None:
        versions = self.list_versions(package)
        return None if versions is None else version.lower() in {v.lower() for v in versions}

    def is_approved(self, package: str, version: str) -> bool | None:
        return self.is_available(package, version)

    def get_compatibility(self, package: str, version: str, target_framework: str) -> bool | None:
        raise NotImplementedError

    def get_dependencies(self, package: str, version: str, target_framework: str) -> list[dict] | None:
        return None

    def get_metadata(self, package: str, version: str) -> dict | None:
        return {"id": package, "version": version, "catalog": self.kind}

    def contains(self, package: str) -> bool | None:
        """Is the package in this catalogue at all? None when unknown (lookup failed)."""
        versions = self.list_versions(package)
        return None if versions is None else bool(versions)

    def newest_allowed(self, package: str, major: int | None = None, prerelease: bool = False) -> str | None:
        """The newest approved version (of `major`, when given). Never a version the catalogue lacks."""
        versions = self.list_versions(package) or []
        allowed = [v for v in versions if self.is_approved(package, v)
                   and (prerelease or "-" not in v) and (major is None or ver_key(v)[:1] == (major,))]
        return max(allowed, key=ver_key) if allowed else None

    def describe(self) -> dict:
        return {"kind": self.kind, "source": self.source}


class FixturePackageCatalog(PackageCatalog):
    """A JSON file emulating an enterprise mirror:
    {"sources": [...], "packages": {"Id": {"availableVersions": [...], "approvedVersions": [...],
     "compatibility": {"1.0.0": ["net8.0", "net10.0"]}, "dependencies": {"1.0.0": {"net10.0": [{"id", "range"}]}},
     "metadata": {...}}}}. A package absent from the file is absent from the mirror."""
    kind = "fixture"

    def __init__(self, path: Path):
        self.source = str(path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.sources = data.get("sources", [])
        self.packages = {k.lower(): v for k, v in data.get("packages", {}).items()}
        self.failures = []

    def _p(self, package: str) -> dict | None:
        return self.packages.get(package.lower())

    def list_versions(self, package: str) -> list[str] | None:
        p = self._p(package)
        return sorted(p.get("availableVersions", []), key=ver_key) if p else []

    def is_approved(self, package: str, version: str) -> bool | None:
        p = self._p(package)
        if not p:
            return False
        approved = p.get("approvedVersions")
        if approved is None:  # no approval list for this package: available = approved
            return self.is_available(package, version)
        return version.lower() in {v.lower() for v in approved}

    def get_compatibility(self, package: str, version: str, target_framework: str) -> bool | None:
        p = self._p(package)
        listed = (p or {}).get("compatibility", {}).get(version)
        return None if listed is None else target_framework in listed

    def get_dependencies(self, package: str, version: str, target_framework: str) -> list[dict] | None:
        p = self._p(package)
        return ((p or {}).get("dependencies", {}).get(version) or {}).get(target_framework)

    def get_metadata(self, package: str, version: str) -> dict | None:
        p = self._p(package)
        if not p:
            return None
        return {"id": package, "version": version, "catalog": self.kind, "sources": self.sources,
                "approved": self.is_approved(package, version), **(p.get("metadata") or {})}


def load_catalog(spec: str | None, cache_dir: Path, online: bool = True, runner=None,
                 repo: Path | None = None) -> PackageCatalog:
    """spec: a fixture JSON path, or None/"public" for the configured NuGet feeds via dotnet."""
    if spec and spec != "public":
        return FixturePackageCatalog(Path(spec))
    from .compat import NuGetClient  # PublicNuGetCatalog
    return NuGetClient(cache_dir, online=online, runner=runner, repo=repo)
