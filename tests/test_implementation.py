"""Implementation mode: unit tests for the edits, plus the acceptance steps 6–10.

The integration tests need dotnet (SDK 10 + runtimes 8 and 10) and take a few minutes.
"""
import json
import subprocess
from pathlib import Path

import pytest

from dmig.analysis import RECIPES, run_analysis
from dmig.dotnet import Runner
from dmig.globalphase import run_global_phase
from dmig.implement import repair_cs14_field, repair_remove_package, run_implementation, set_target_frameworks

from conftest import CATALOG, FULL, dotnet_path, requires

GIT = ["git", "-c", "core.autocrlf=false", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
needs_dotnet = requires(*FULL)


# ----------------------------------------------------------------------------- unit
def test_single_to_multi_target_uses_plural_element(tmp_path):
    f = tmp_path / "A.csproj"
    f.write_text("<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>")
    set_target_frameworks(f, ["net8.0", "net10.0"])
    assert "<TargetFrameworks>net8.0;net10.0</TargetFrameworks>" in f.read_text()
    # The singular stays, empty: a value inherited from Directory.Build.props would otherwise win
    # (real repository: projects silently stayed net8.0-only).
    assert "<TargetFramework>net8.0</TargetFramework>" not in f.read_text()
    assert "<TargetFramework></TargetFramework>" in f.read_text()


def test_inherited_tfm_is_cleared_when_multi_targeting(tmp_path):
    # A singular TargetFramework inherited from Directory.Build.props would beat the plural one.
    f = tmp_path / "A.csproj"
    f.write_text('<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n  </PropertyGroup>\n</Project>')
    set_target_frameworks(f, ["net8.0", "net10.0"])
    text = f.read_text()
    assert "<TargetFramework></TargetFramework>" in text and "<TargetFrameworks>net8.0;net10.0</TargetFrameworks>" in text


def test_cs14_field_repair_touches_only_flagged_lines(tmp_path):
    src = tmp_path / "M.cs"
    src.write_text("class M {\n  private decimal field;\n  public decimal A { get { return field; } }\n  // field\n}\n")
    repair_cs14_field(tmp_path, [{"code": "CS9258", "file": "M.cs", "line": 3}])
    lines = src.read_text().split("\n")
    assert lines[1] == "  private decimal field;"
    assert lines[2] == "  public decimal A { get { return this.field; } }"
    assert lines[3] == "  // field"


def test_package_removed_for_net10_only_and_conditioned_when_multi_targeted(tmp_path):
    proj = tmp_path / "P.csproj"
    proj.write_text('<Project>\n  <PropertyGroup>\n    <TargetFramework>net10.0</TargetFramework>\n  </PropertyGroup>\n'
                    '  <ItemGroup>\n    <PackageReference Include="System.Linq.Async" />\n  </ItemGroup>\n</Project>\n')
    repair_remove_package(tmp_path, "P.csproj", "System.Linq.Async")
    assert "System.Linq.Async" not in proj.read_text() and "<ItemGroup>" not in proj.read_text()
    proj.write_text('<Project>\n  <PropertyGroup>\n    <TargetFrameworks>net8.0;net10.0</TargetFrameworks>\n  </PropertyGroup>\n'
                    '  <ItemGroup>\n    <PackageReference Include="System.Linq.Async" />\n  </ItemGroup>\n</Project>\n')
    repair_remove_package(tmp_path, "P.csproj", "System.Linq.Async")
    assert "Condition=\"$(TargetFramework.StartsWith('net8.0'))\"" in proj.read_text()


# ----------------------------------------------------------------------------- integration
def _analyze(repo: Path, out: Path) -> dict:
    run_analysis(repo, out, Runner(dotnet_path(), out / "logs"),
                 {"run_baseline": True, "allow_reversible_experiments": True, "package_catalog": str(CATALOG),
                  "adapters": ["aspnet_core"]})   # the sample's web app is migrated too
    return json.loads((out / "migration-plan.json").read_text(encoding="utf-8"))


def _implement(repo: Path, out: Path, plan: dict, gid: str, **flags) -> dict:
    group = next(g for g in plan["groups"] if g["group_id"] == gid)
    request = dict(group["implementation_request"], baseline_cache_dir=str(out.parent / "analysis" / "baseline-cache"), **flags)
    preflight = json.loads((out.parent / "analysis" / "preflight.json").read_text(encoding="utf-8"))
    return run_implementation(repo, out, Runner(dotnet_path(), out / "logs"), plan, request, RECIPES, preflight)


def _global(repo: Path, tmp: Path, plan: dict, phase: str) -> dict:
    analysis = tmp / "analysis"
    request = {"approved": True, "allow_newer_commit": True, "baseline_cache_dir": str(analysis / "baseline-cache"),
               "reference_baseline": json.loads((analysis / "baseline.json").read_text(encoding="utf-8")),
               "original_inventory": json.loads((analysis / "inventory.json").read_text(encoding="utf-8"))}
    preflight = json.loads((analysis / "preflight.json").read_text(encoding="utf-8"))
    out = tmp / phase
    res = run_global_phase(repo, out, Runner(dotnet_path(), out / "logs"), plan, phase, request, preflight)
    assert "schema_problems" not in res, res.get("schema_problems")
    assert res["rollback"]["verified"] and not res["changes_persisted"]
    if res.get("patch_path"):
        subprocess.run(["git", "apply", res["patch_path"]], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", phase],
                       cwd=repo, check=True)
    return res


def _status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


@needs_dotnet
def test_groups_migrate_in_order_and_patches_apply(sample_repo: Path, tmp_path: Path):
    plan = _analyze(sample_repo, tmp_path / "analysis")

    # Out of order is refused before anything is built.
    early = _implement(sample_repo, tmp_path / "early", plan, "app-appa-web")
    assert early["status"] == "BLOCKED_DEPENDENCY_ORDER"

    expected_repairs = {"shared-foundation": [["SRC-CS14-FIELD"]], "app-appb-worker": [["PKG-LINQ-ASYNC"]],
                        "app-appa-web": []}
    # PHASE_1 (repository-global, separately approved): CI moves to the SDK 10 image and keeps the
    # .NET 8 runtime while net8.0 targets remain; global.json already allows SDK 10.
    p1 = _global(sample_repo, tmp_path, plan, "PHASE_1_SDK_GLOBAL")
    assert p1["status"] == "SUCCESS" and p1["files_changed"] == [".gitlab-ci.yml"]
    ci = (sample_repo / ".gitlab-ci.yml").read_text()
    assert "sdk:10.0" in ci and "--channel 8.0 --runtime dotnet" in ci

    sources, scopes = {}, {}
    for gid, repairs in expected_repairs.items():
        before = _status(sample_repo)
        res = _implement(sample_repo, tmp_path / gid, plan, gid, allow_newer_commit=True)
        assert res["status"] == "SUCCESS", (gid, res["unresolved_items"])
        assert [a["selected_rules"] for a in res["repair_attempts"]] == repairs
        assert res["rollback"]["verified"] and not res["changes_persisted"]
        assert "schema_problems" not in res
        assert _status(sample_repo) == before  # the tool itself never touches the repository
        assert all(c["passed"] for c in res["final_gate"])
        assert not any(f in {"global.json", "Directory.Packages.props", ".gitlab-ci.yml"} for f in res["files_changed"])
        # The human (here: the test) applies and commits the reviewed patch.
        sources[gid] = res["baseline"]["source"]
        scopes[gid] = res["build_scope"]["projects"]
        # The human (here: the test) applies and commits the reviewed patch with normal git settings.
        subprocess.run(["git", "apply", res["patch_path"]], cwd=sample_repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam",
                        f"{gid} migrated"], cwd=sample_repo, check=True)

    # WinForms is report-only: listed, never migrated by the tool, no patch.
    legacy = _implement(sample_repo, tmp_path / "component-legacyadmin", plan, "component-legacyadmin",
                        allow_newer_commit=True)
    assert legacy["status"] == "SKIPPED_NOT_APPLICABLE" and not legacy.get("patch_path")
    assert legacy["stop_reason"].startswith("OUT_OF_SCOPE_REPORT_ONLY") and legacy["rollback"]["verified"]
    # ... so a person migrates it by hand (here: the test), before .NET 8 is retired.
    legacy_proj = sample_repo / "src/Tools/LegacyAdmin/LegacyAdmin.csproj"
    legacy_proj.write_bytes(legacy_proj.read_bytes().replace(b"net8.0-windows", b"net10.0-windows"))
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam",
                    "LegacyAdmin migrated by hand"], cwd=sample_repo, check=True)

    # Baseline cache: the analysis baseline serves the first group; the first group's final state
    # (same tree after the patch is committed) serves the next group whose scope it covers.
    assert sources["shared-foundation"].startswith("reused cached baseline")
    assert sources["app-appb-worker"].startswith("reused cached baseline")
    assert sources["app-appa-web"].startswith("built")  # its projects were outside the previous scope
    # Scoped builds: an application group builds only itself and its tests.
    assert scopes["app-appb-worker"] == ["src/AppB/AppB.Core/AppB.Core.csproj",
                                         "src/AppB/AppB.Worker/AppB.Worker.csproj", "tests/AppB.Tests/AppB.Tests.csproj"]
    assert len(scopes["shared-foundation"]) == 9  # a shared library's scope is every consumer

    shared = (sample_repo / "src/Shared/Shared.Core/Shared.Core.csproj").read_text()
    assert "<TargetFrameworks>net8.0;net10.0</TargetFrameworks>" in shared  # until PHASE_5 retires net8.0
    assert "this.field" in (sample_repo / "src/Shared/Shared.Core/Money.cs").read_text()

    # Remaining global phases, each its own approved patch.
    p5 = _global(sample_repo, tmp_path, plan, "PHASE_5_RETIRE_NET8")
    assert p5["status"] == "SUCCESS"
    assert "<TargetFramework>net10.0</TargetFramework>" in (sample_repo / "src/Shared/Shared.Core/Shared.Core.csproj").read_text()
    p6 = _global(sample_repo, tmp_path, plan, "PHASE_6_CONVERGE")
    assert p6["status"] == "SUCCESS"
    central = (sample_repo / "Directory.Packages.props").read_text()
    assert 'Include="Microsoft.Extensions.Hosting" Version="10.' in central
    assert "System.Linq.Async" not in central  # became unused through the migration
    p7 = _global(sample_repo, tmp_path, plan, "PHASE_7_REPO_VALIDATION")
    assert p7["status"] == "VALIDATION_PASSED"
    assert p7["validation"]["projects_still_on_net8"] == [] and p7["validation"]["regressions"] == []
    p8 = _global(sample_repo, tmp_path, plan, "PHASE_8_CUTOVER")
    assert p8["status"] == "SUCCESS"
    ci = (sample_repo / ".gitlab-ci.yml").read_text()
    assert "sdk:10.0" in ci and "dotnet-install" not in ci and "before_script" not in ci
    assert "System.Linq.Async" not in (sample_repo / "src/AppB/AppB.Core/AppB.Core.csproj").read_text()
    assert "aspnet:10.0" in (sample_repo / "src/AppA/AppA.Web/Dockerfile").read_text()


@needs_dotnet
def test_unrepairable_failure_rolls_back_without_a_patch(sample_repo: Path, tmp_path: Path):
    # A behaviour change no recipe rule knows: passes on net8.0, fails on net10.0.
    test_file = sample_repo / "tests/AppB.Tests/RuntimeTests.cs"
    test_file.write_text("namespace AppB.Tests;\n\npublic class RuntimeTests\n{\n    [Fact]\n"
                         "    public void RunsOnDotNet8() => Assert.Equal(8, Environment.Version.Major);\n}\n")
    subprocess.run(GIT + ["add", "-A"], cwd=sample_repo, check=True)
    subprocess.run(GIT + ["commit", "-qm", "runtime check"], cwd=sample_repo, check=True)
    plan = _analyze(sample_repo, tmp_path / "analysis")
    before = _status(sample_repo)
    res = _implement(sample_repo, tmp_path / "appb", plan, "app-appb-worker", allow_out_of_order=True)
    assert res["status"] == "ROLLED_BACK"
    assert res["rollback"] == {"performed": True, "verified": True}
    assert res.get("patch_path") is None and not list((tmp_path / "appb").glob("*.patch"))
    assert res["repair_attempts"][-1]["next_action"] == "stop_for_human_review"
    assert any("RunsOnDotNet8" in u for u in res["unresolved_items"])
    assert _status(sample_repo) == before
