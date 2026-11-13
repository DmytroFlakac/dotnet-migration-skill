"""Breaking-change catalogue, rule drafts and the third automatic repair (NU1510)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from dmig.buildtest import parse_diagnostics
from dmig.compat import load_recipe, review_checklist, technologies
from dmig.drafts import build_drafts, write_drafts

from conftest import ROOT, SAMPLE, dotnet_path

RECIPE = load_recipe(ROOT / "recipes")
GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def test_catalogue_is_complete_and_well_formed():
    catalogue = [r for r in RECIPE["rules"] if r.get("source") == "catalog"]
    kinds = {k: sum(r["kind"] == k for r in catalogue) for k in ("diagnostic", "source", "checklist")}
    assert len(catalogue) >= 250 and kinds["diagnostic"] >= 60 and kinds["source"] >= 40 and kinds["checklist"] >= 100
    ids = [r["id"] for r in RECIPE["rules"]]
    assert len(ids) == len(set(ids))  # hand-written and generated rules never clash
    for r in catalogue:
        assert r["reference"].startswith("https://learn.microsoft.com/"), r["id"]
        assert r["applies_if"] in {"any", "web", "efcore", "winforms", "wpf", "containers", "vb"}
        if r["kind"] != "diagnostic":  # catalogue knowledge is advisory, never an automatic edit
            assert r["repair"]["action"].startswith("Check:")


def test_catalogue_is_regenerated_from_the_committed_sources(tmp_path):
    before = (ROOT / "recipes" / "rules" / "catalog.yaml").read_text(encoding="utf-8")
    subprocess.run(["python", str(ROOT / "recipes" / "build_catalog.py")], check=True, capture_output=True)
    assert (ROOT / "recipes" / "rules" / "catalog.yaml").read_text(encoding="utf-8") == before


def test_checklist_follows_the_technologies_in_use():
    base = {"files": {"dockerfiles": []}, "projects": []}
    plain = {r["area"] for r in review_checklist(base, RECIPE)}
    assert "Entity Framework Core" not in plain and "Windows Forms" not in plain
    ef = dict(base, projects=[{"id": "a.csproj", "packages": [{"id": "Microsoft.EntityFrameworkCore.SqlServer"}],
                               "properties": {}, "is_web": False}])
    assert "efcore" in technologies(ef)
    assert "Entity Framework Core" in {r["area"] for r in review_checklist(ef, RECIPE)}


def test_nuget_errors_are_attributed_to_the_project_not_the_solution(tmp_path):
    out = (f"{tmp_path}\\src\\B\\B.csproj : error NU1510: Warning As Error: PackageReference System.Text.Json "
           f"will not be pruned. [{tmp_path}\\All.sln]\n")
    d = parse_diagnostics(out, tmp_path)[0]
    assert d["code"] == "NU1510" and d["project"] == "src/B/B.csproj"


def test_rule_drafts_carry_everything_known(tmp_path):
    failures = [{"code": "CS1061", "file": "src/A/X.cs", "line": 4, "project": "src/A/A.csproj",
                 "failure_class": "compile_time_api_break", "reason": "no recipe rule"},
                {"code": "CS1061", "file": "src/A/Y.cs", "line": 9, "project": "src/A/A.csproj"},
                {"test": "A.Tests.T.Works", "tfm": "net10.0", "reason": "regression with no rule"}]
    drafts = build_drafts(failures, RECIPE)
    assert [d["id"] for d in drafts] == ["DRAFT-CS1061", "DRAFT-TEST-2"]
    assert len(drafts[0]["seen_in"]) == 2
    assert drafts[0]["reference"].endswith("/compiler-messages/cs1061")
    text = Path(write_drafts(tmp_path, failures, RECIPE)).read_text(encoding="utf-8")
    assert text.startswith("# RULE DRAFTS") and "NOT ACTIVE" in text
    assert yaml.safe_load(text)["rules"][0]["status"] == "draft"


@pytest.mark.skipif(dotnet_path() is None, reason="dotnet not available")
def test_obsolete_api_and_pruned_package_fixture(tmp_path: Path):
    """Sample + System.Text.Json reference (NU1510 as error) + an API obsolete in .NET 10 (SYSLIB0060)."""
    from dmig.analysis import RECIPES, run_analysis
    from dmig.dotnet import Runner
    from dmig.implement import run_implementation

    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("bin", "obj"))
    props = repo / "Directory.Packages.props"
    props.write_text(props.read_text().replace(
        '<PackageVersion Include="System.Linq.Async" Version="6.0.1" />',
        '<PackageVersion Include="System.Linq.Async" Version="6.0.1" />\n    '
        '<PackageVersion Include="System.Text.Json" Version="8.0.5" />'))
    proj = repo / "src/AppB/AppB.Core/AppB.Core.csproj"
    proj.write_text(proj.read_text()
                    .replace("<TargetFramework>net8.0</TargetFramework>",
                             "<TargetFramework>net8.0</TargetFramework>\n    <WarningsAsErrors>NU1510</WarningsAsErrors>")
                    .replace('<PackageReference Include="System.Linq.Async" />',
                             '<PackageReference Include="System.Linq.Async" />\n    <PackageReference Include="System.Text.Json" />'))
    (repo / "src/AppB/AppB.Core/Hashing.cs").write_text(
        "using System.Security.Cryptography;\n\nnamespace AppB.Core;\n\npublic static class Hashing\n{\n"
        "    public static byte[] Derive(string p, byte[] salt)\n    {\n"
        "        using var kdf = new Rfc2898DeriveBytes(p, salt, 100_000, HashAlgorithmName.SHA256);\n"
        "        return kdf.GetBytes(32);\n    }\n}\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run(GIT + args, cwd=repo, check=True, capture_output=True)

    out = tmp_path / "analysis"
    run_analysis(repo, out, Runner(dotnet_path(), out / "logs"), {"run_baseline": True, "probe_network": True})
    plan = json.loads((out / "migration-plan.json").read_text(encoding="utf-8"))
    groups = {g["group_id"]: g for g in plan["groups"]}
    # The pruned-package error is attributed to AppB and explained; behind that restore wall the
    # layered experiment also reaches the System.Linq.Async clash. Other groups stay verified.
    assert sorted({(e["code"], e["matched_rule"]) for e in groups["app-appb-worker"]["experiment"]["new_errors"]}) \
        == [("CS0121", "PKG-LINQ-ASYNC"), ("NU1510", "PKG-INBOX-PRUNED")]
    assert groups["component-legacyadmin"]["readiness"] == "READY"

    preflight = json.loads((out / "preflight.json").read_text(encoding="utf-8"))
    runner = lambda o: Runner(dotnet_path(), o / "logs")  # noqa: E731
    shared = run_implementation(repo, tmp_path / "s", runner(tmp_path / "s"), plan,
                                dict(groups["shared-foundation"]["implementation_request"]), RECIPES, preflight)
    subprocess.run(["git", "apply", shared["patch_path"]], cwd=repo, check=True)
    subprocess.run(GIT + ["commit", "-qam", "shared"], cwd=repo, check=True)
    appb = run_implementation(repo, tmp_path / "b", runner(tmp_path / "b"), plan,
                              dict(groups["app-appb-worker"]["implementation_request"], allow_newer_commit=True),
                              RECIPES, preflight)
    assert appb["status"] == "SUCCESS"
    # NU1510 repair first (restore then succeeds and reveals CS0121), then the System.Linq.Async repair.
    assert [a["selected_rules"] for a in appb["repair_attempts"]] == [["PKG-INBOX-PRUNED"], ["PKG-LINQ-ASYNC"]]
    assert [(w["code"], w["rule_id"]) for w in appb["warnings_to_review"]] == [("SYSLIB0060", "DIAG-SYSLIB0060")]
    text = (repo / "src/AppB/AppB.Core/AppB.Core.csproj").read_text()
    assert "System.Text.Json" in text  # the repository itself is untouched until the patch is applied
    patch = Path(appb["patch_path"]).read_text(encoding="utf-8")
    assert '-    <PackageReference Include="System.Text.Json" />' in patch
