"""Framework add-ons are off by default (review §3); out-of-scope projects are listed, never dropped."""
import pytest

from dmig.analysis import scoped_recipe
from dmig.compat import load_recipe
from dmig.scope import annotate, parse, project_type, rule_enabled

from conftest import ROOT


def P(**kw):
    base = {"id": "x.csproj", "kind": "library", "sdk_style": True, "is_web": False, "properties": {}, "packages": []}
    return {**base, **kw}


def test_project_types():
    assert project_type(P())[0] == "class_library"
    assert project_type(P(kind="application"))[0] == "console_application"
    assert project_type(P(kind="application", properties={"UsingMicrosoftNETSdkWorker": "true"}))[0] == "worker_service"
    assert project_type(P(kind="test"))[0] == "test_project"
    assert project_type(P(kind="application", is_web=True)) == ("aspnet_core", "aspnet_core")
    assert project_type(P(properties={"UseWPF": "true"})) == ("wpf", None)
    assert project_type(P(packages=[{"id": "Microsoft.Extensions.Hosting.WindowsServices"}]))[1] == "windows_service"
    assert project_type(P(sdk_style=False))[0] == "legacy_dotnet_framework"
    assert project_type(P(packages=[{"id": "Microsoft.Azure.Functions.Worker"}]))[0] == "azure_functions"


def test_out_of_scope_projects_are_listed():
    inv = {"projects": [P(id="a.csproj"), P(id="web.csproj", kind="application", is_web=True),
                        P(id="old.csproj", sdk_style=False)]}
    out = annotate(inv, set())
    assert {(o["project"], o["status"]) for o in out} == {("web.csproj", "OUT_OF_SCOPE_ADAPTER_DISABLED"),
                                                         ("old.csproj", "OUT_OF_SCOPE_REPORT_ONLY")}
    assert [p["scope"] for p in inv["projects"]][0] == "IN_SCOPE"
    assert annotate(inv, {"aspnet_core"}) == [{"project": "old.csproj", "type": "legacy_dotnet_framework",
                                               "adapter": None, "status": "OUT_OF_SCOPE_REPORT_ONLY"}]


def test_add_on_rules_are_off_by_default():
    recipe = load_recipe(ROOT / "recipes")
    default = scoped_recipe(recipe, set())
    assert not [r for r in default["rules"] if r.get("applies_if") in {"web", "efcore", "wpf", "winforms"}]
    assert not [r for r in default["rules"] if r["id"].startswith("SRC-ASPNET-")]
    assert "Microsoft.AspNetCore." not in default["transformations"]["packages"]["coordinated_framework_prefixes"]
    on = scoped_recipe(recipe, {"aspnet_core", "efcore"})
    assert any(r.get("applies_if") == "web" for r in on["rules"]) and any(r.get("applies_if") == "efcore" for r in on["rules"])
    assert rule_enabled({"id": "R", "applies_if": "any"}, set())


def test_unknown_adapter_is_refused():
    with pytest.raises(ValueError):
        parse("aspnet_core,django")

def test_wpf_and_winforms_are_report_only():
    """.NET Framework, WPF, WinForms, MAUI, Xamarin, Azure Functions are listed, never migrated."""
    inv = {"projects": [P(id="ui.csproj", kind="application", properties={"UseWPF": "true"}),
                        P(id="forms.csproj", kind="application", properties={"UseWindowsForms": "true"})]}
    assert {o["status"] for o in annotate(inv, {"aspnet_core", "efcore", "windows_service"})} == {"OUT_OF_SCOPE_REPORT_ONLY"}
    with pytest.raises(ValueError, match="report-only"):
        parse("wpf")
