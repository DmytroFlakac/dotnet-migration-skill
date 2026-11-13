"""What is in scope: project types and optional framework add-ons ("adapters").

Default scope: SDK-style class libraries, console applications, worker services and test projects.
Framework add-ons are off unless enabled (`--adapters aspnet_core,efcore,...`):

    aspnet_core      web projects (Microsoft.NET.Sdk.Web) and the ASP.NET Core breaking-change rules
    efcore           the EF Core breaking-change rules (EF projects themselves are libraries)
    windows_service  Windows service hosts (worker services that need Windows)

Report-only types are never migrated: legacy .NET Framework (non-SDK) projects,
WPF, WinForms, MAUI, Xamarin, Azure Functions. Projects outside the scope are always listed, never dropped silently:
OUT_OF_SCOPE_ADAPTER_DISABLED (an add-on would cover it) or OUT_OF_SCOPE_REPORT_ONLY.
"""
from __future__ import annotations

ADAPTERS = ("aspnet_core", "efcore", "windows_service")
REPORT_ONLY = ("legacy_dotnet_framework", "wpf", "winforms", "maui", "xamarin", "azure_functions")
DEFAULT_TYPES = ("class_library", "console_application", "worker_service", "test_project")
# WPF/WinForms rules belong to report-only project types: they never apply to a migration.
RULE_AREA_ADAPTER = {"web": "aspnet_core", "efcore": "efcore", "wpf": "report_only", "winforms": "report_only"}


def project_type(p: dict) -> tuple[str, str | None]:
    """(type, adapter that covers it or None)."""
    props = {k.lower(): str(v).lower() for k, v in (p.get("properties") or {}).items()}
    sdk = (p.get("sdk") or "").lower()
    pkgs = {pk["id"].lower() for pk in p.get("packages", [])}
    if not p.get("sdk_style", True) or p.get("legacy_packages_config"):
        return "legacy_dotnet_framework", None
    if props.get("usemaui") == "true" or "maui" in sdk or any(x.startswith("microsoft.maui") for x in pkgs):
        return "maui", None
    if any(x.startswith("xamarin.") for x in pkgs):
        return "xamarin", None
    if sdk.endswith(".functions") or {"microsoft.net.sdk.functions", "microsoft.azure.functions.worker"} & pkgs:
        return "azure_functions", None
    if p.get("kind") == "test":
        return "test_project", None
    if props.get("usewpf") == "true":
        return "wpf", None
    if props.get("usewindowsforms") == "true":
        return "winforms", None
    if p.get("is_web"):
        return "aspnet_core", "aspnet_core"
    if "microsoft.extensions.hosting.windowsservices" in pkgs or props.get("usewindowsservice") == "true":
        return "windows_service", "windows_service"
    if props.get("usingmicrosoftnetsdkworker") == "true" or sdk.endswith(".worker"):
        return "worker_service", None
    if p.get("kind") == "application":
        return "console_application", None
    return "class_library", None


def scope_status(p: dict, enabled: set[str]) -> str:
    ptype, adapter = project_type(p)
    if ptype in REPORT_ONLY:
        return "OUT_OF_SCOPE_REPORT_ONLY"
    if adapter and adapter not in enabled:
        return "OUT_OF_SCOPE_ADAPTER_DISABLED"
    return "IN_SCOPE"


def annotate(inventory: dict, enabled: set[str]) -> list[dict]:
    """Writes project_type, adapter and scope into every project; returns the out-of-scope list."""
    out = []
    for p in inventory["projects"]:
        p["project_type"], p["adapter"] = project_type(p)
        p["scope"] = scope_status(p, enabled)
        if p["scope"] != "IN_SCOPE":
            out.append({"project": p["id"], "type": p["project_type"], "adapter": p["adapter"], "status": p["scope"]})
    return out


def rule_enabled(rule: dict, enabled: set[str]) -> bool:
    """A rule that belongs to a framework add-on applies only when that add-on is enabled."""
    adapter = RULE_AREA_ADAPTER.get(rule.get("applies_if", "any"))
    if rule.get("id", "").startswith("SRC-ASPNET-"):
        adapter = "aspnet_core"
    return adapter is None or adapter in enabled


def parse(value: str | list | None) -> set[str]:
    items = value if isinstance(value, list) else [x for x in (value or "").replace(" ", "").split(",") if x]
    report_only = [x for x in items if x in {"wpf", "winforms"}]
    if report_only:
        raise ValueError(f"{', '.join(report_only)}: WPF and WinForms projects are report-only and cannot be "
                         f"enabled; they are listed as OUT_OF_SCOPE_REPORT_ONLY. Add-ons: {', '.join(ADAPTERS)}")
    unknown = [x for x in items if x not in ADAPTERS]
    if unknown:
        raise ValueError(f"unknown adapter(s) {unknown}; choose from {', '.join(ADAPTERS)}")
    return set(items)
