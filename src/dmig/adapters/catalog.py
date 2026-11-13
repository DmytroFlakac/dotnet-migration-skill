"""IPackageCatalog: package versions, availability and TFM compatibility.

The interface lives in dmig.catalog (the engine uses it directly). Mock: MockEnterprisePackageCatalog
reads fixtures/package-catalog.json, a JSON file standing in for the organisation's package mirror
(`availableVersions` / `approvedVersions` per package). It never proposes a version that is not in
`availableVersions`. The public catalogue (the configured feeds through `dotnet`) is used only with
--allow-public-nuget. A production adapter implements the same methods over the catalogue service.
"""
from __future__ import annotations

from pathlib import Path

from ..catalog import LocalCatalog, PackageCatalog
from ..compat import NuGetClient as PublicNuGetCatalog

IPackageCatalog = PackageCatalog
FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "package-catalog.json"


class MockEnterprisePackageCatalog(LocalCatalog):
    """The mock mirror: fixtures/package-catalog.json unless another file is given."""

    def __init__(self, path: Path | None = None):
        super().__init__(Path(path) if path else FIXTURE)


__all__ = ["IPackageCatalog", "MockEnterprisePackageCatalog", "PackageCatalog", "LocalCatalog", "PublicNuGetCatalog"]
