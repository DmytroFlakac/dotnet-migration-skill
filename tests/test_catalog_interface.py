"""Package catalogue: every version decision goes through it; a organisation mirror's
approval list is respected; packages missing from the mirror are reported. No dotnet needed."""
import json
from pathlib import Path

from dmig.catalog import FixturePackageCatalog, PackageCatalog, load_catalog, tfm_supports
from dmig.compat import load_recipe, package_findings

from conftest import ROOT

FIXTURE = ROOT / "samples" / "package-catalog.fixture.json"


def test_fixture_catalog_answers_the_contract_questions():
    c = FixturePackageCatalog(FIXTURE)
    assert c.list_versions("microsoft.extensions.hosting") == ["8.0.0", "8.0.1", "10.0.0", "10.0.3"]
    assert c.is_available("Microsoft.Extensions.Hosting", "10.0.3") is True
    assert c.is_approved("Microsoft.Extensions.Hosting", "10.0.3") is False      # mirrored, not approved
    assert c.newest_allowed("Microsoft.Extensions.Hosting", 10) == "10.0.0"     # never the unapproved 10.0.3
    assert c.get_compatibility("Microsoft.Extensions.Hosting", "10.0.0", "net8.0") is True
    assert c.get_compatibility("Microsoft.Extensions.Hosting", "9.9.9", "net8.0") is None   # unknown, not "no"
    assert c.contains("System.Linq.Async") is False and c.newest_allowed("System.Linq.Async") is None
    assert c.get_metadata("xunit", "2.9.3")["approved"] is True


def test_tfm_support_from_declared_frameworks():
    assert tfm_supports(["net8.0", "net10.0"], "net8.0") is True
    assert tfm_supports(["netstandard2.0"], "net10.0") is True
    assert tfm_supports(["net10.0"], "net8.0") is False
    assert tfm_supports([], "net8.0") is None


def test_load_catalog_chooses_by_spec(tmp_path: Path):
    assert load_catalog(str(FIXTURE), tmp_path).kind == "fixture"
    assert load_catalog(None, tmp_path, online=False).kind == "public-nuget"
    assert isinstance(load_catalog("public", tmp_path, online=False), PackageCatalog)


def _inventory(packages: list[tuple[str, str]]) -> dict:
    return {"central_package_files": ["Directory.Packages.props"], "projects": [
        {"id": "src/A/A.csproj", "packages": [{"id": i, "version": v} for i, v in packages]}]}


def test_findings_use_only_approved_versions_and_report_missing_packages():
    recipe = load_recipe(ROOT / "recipes")
    inv = _inventory([("Microsoft.Extensions.Hosting", "8.0.1"), ("System.Linq.Async", "6.0.1"),
                      ("xunit", "2.9.2")])
    findings, strategies = package_findings(inv, recipe, FixturePackageCatalog(FIXTURE))
    ids = {(f["rule_id"], f["package"]) for f in findings if f.get("package")}
    assert ("PKG-NOT-IN-CATALOGUE", "System.Linq.Async") in ids          # public, but not mirrored
    assert ("PKG-VERSION-NOT-APPROVED", "xunit") in ids                  # 2.9.2 is not the approved 2.9.3
    hosting = next(s for s in strategies if s["package"] == "Microsoft.Extensions.Hosting")
    assert hosting["net10_version"] == "10.0.0" and hosting["strategy"] == "ONE_VERSION_BOTH_TFMS"


def test_msbuild_sdk_pin_and_phase6_use_the_catalogue(tmp_path: Path):
    from dmig.globalphase import bump_msbuild_sdk_pins
    fixture = tmp_path / "mirror.json"
    fixture.write_text(json.dumps({"packages": {"Microsoft.Build.Sql": {
        "availableVersions": ["1.0.0", "2.1.0", "2.3.0"], "approvedVersions": ["1.0.0", "2.1.0"]}}}), encoding="utf-8")
    (tmp_path / "global.json").write_text('{\n  "msbuild-sdks": {\n    "Microsoft.Build.Sql": "1.0.0"\n  }\n}\n',
                                          encoding="utf-8")
    err = {"code": "MSB4019", "message": "", "file": "C:/nuget/microsoft.build.sql/1.0.0/Sdk/Sdk.targets"}
    bumps = bump_msbuild_sdk_pins(tmp_path, {"files": {"global_json": ["global.json"]}}, [err],
                                  FixturePackageCatalog(fixture))
    assert bumps[0]["after"] == "2.1.0"                                    # newest *approved*, not 2.3.0
