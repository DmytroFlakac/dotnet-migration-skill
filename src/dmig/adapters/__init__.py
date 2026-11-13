"""Integration points: named interfaces the surrounding pipeline implements, each with a mock.

    IPackageCatalog   versions, availability, TFM compatibility   mock: MockEnterprisePackageCatalog
                                                                  (fixtures/package-catalog.json)
    ISourceControl    worktree, diff, patch                       mock: LocalGitSourceControl (local git)
    IWorkItemAdapter  ticket id                                   mock: MockWorkItemAdapter ("TEST-123")
    ISecretProvider   feed credentials                            mock: EnvSecretProvider (env vars, dummy values)
    ICiExecutor       where build/test run                        mock: LocalProcessExecutor (local process)
    ITelemetry        events                                      mock: JsonlTelemetry (JSON lines file)
    IDelivery         branch / merge request                      mock: LocalFileDelivery (writes
                                                                  migration.patch + migration-report.md only)

The migration core depends only on the interfaces; the concrete mocks are chosen here, in
`defaults()`, which only the command line (the composition root) calls. The core never calls
IDelivery or IWorkItemAdapter itself: it returns a patch and a result, and the orchestrator decides
the rest. Earlier names (PackageCatalog, SourceControl, WorkItems, Secrets, Telemetry, Runner and
their mocks) remain as aliases.
"""
from .catalog import IPackageCatalog, LocalCatalog, MockEnterprisePackageCatalog, PackageCatalog, PublicNuGetCatalog
from .delivery import IDelivery, LocalFileDelivery, LocalReviewPackage, SourceControl
from .runner import ICiExecutor, LocalProcessExecutor, LocalRunner, Runner
from .secrets import EnvSecretProvider, EnvSecrets, ISecretProvider, Secrets
from .source_control import ISourceControl, LocalGitSourceControl
from .telemetry import ITelemetry, JsonlTelemetry, Telemetry
from .work_items import FixedWorkItems, IWorkItemAdapter, MockWorkItemAdapter, WorkItems

__all__ = ["IPackageCatalog", "MockEnterprisePackageCatalog", "ISourceControl", "LocalGitSourceControl",
           "IWorkItemAdapter", "MockWorkItemAdapter", "ISecretProvider", "EnvSecretProvider", "ICiExecutor",
           "LocalProcessExecutor", "ITelemetry", "JsonlTelemetry", "IDelivery", "LocalFileDelivery", "defaults",
           # earlier names
           "PackageCatalog", "LocalCatalog", "PublicNuGetCatalog", "SourceControl", "LocalReviewPackage",
           "WorkItems", "FixedWorkItems", "Secrets", "EnvSecrets", "Telemetry", "Runner", "LocalRunner"]


def defaults(out_dir, catalog_path=None) -> dict:
    """The mock set used when nothing else is configured."""
    from pathlib import Path
    out = Path(out_dir)
    return {"package_catalog": MockEnterprisePackageCatalog(Path(catalog_path) if catalog_path else None),
            "source_control": LocalGitSourceControl(),
            "work_items": MockWorkItemAdapter(), "secrets": EnvSecretProvider(),
            "ci_executor": LocalProcessExecutor(),
            "telemetry": JsonlTelemetry(out / "telemetry.jsonl"),
            "delivery": LocalFileDelivery(out / "delivery")}
