"""Fast unit tests: no dotnet, no network."""
from pathlib import Path

from dmig.buildtest import classify, parse_diagnostics
from dmig.compat import _glob_match, _ver, load_recipe, source_and_file_findings
from dmig.discovery import categorize, parse_solution
from dmig.experiment import change_class, retarget
from dmig.graph import ProjectGraph, associate_tests
from dmig.grouping import build_groups, temporary_tfms
from dmig.preflight import _versions

from conftest import ROOT, SAMPLE


def proj(pid, kind="library", refs=(), tfms=("net8.0",), platform="ANY", frameworks=()):
    return {"id": pid, "name": pid.rsplit("/", 1)[-1].rsplit(".", 1)[0], "kind": kind,
            "project_references": list(refs), "target_frameworks": list(tfms), "platform": platform,
            "test_frameworks": list(frameworks), "evaluated": True, "unresolved_references": []}


def sample_projects():
    return [
        proj("s/Shared.csproj"),
        proj("a/A.Core.csproj", refs=["s/Shared.csproj"]),
        proj("a/A.App.csproj", kind="application", refs=["a/A.Core.csproj"]),
        proj("b/B.Core.csproj", refs=["s/Shared.csproj"]),
        proj("b/B.App.csproj", kind="application", refs=["b/B.Core.csproj"]),
        proj("t/Shared.Tests.csproj", kind="test", refs=["s/Shared.csproj"], frameworks=["xunit"]),
        proj("t/A.Tests.csproj", kind="test", refs=["a/A.Core.csproj", "s/Shared.csproj"]),
        proj("t/Orphan.Tests.csproj", kind="test"),
    ]


def test_shared_library_detected_and_grouped_first():
    g = ProjectGraph(sample_projects())
    res = build_groups(g, associate_tests(g))
    ids = [x["group_id"] for x in res["groups"]]
    assert ids[0] == "shared-foundation"
    assert set(ids) == {"shared-foundation", "app-a-app", "app-b-app"}
    shared = res["shared_projects"][0]
    assert shared["project_id"] == "s/Shared.csproj"
    assert shared["temporary_tfms"] == ["net8.0", "net10.0"]
    assert shared["consumer_groups"] == ["app-a-app", "app-b-app"]
    assert shared["shared_library_policy"] == "MULTITARGET_UNTIL_LAST_CONSUMER"


def test_tests_follow_their_references_not_their_names():
    g = ProjectGraph(sample_projects())
    res = build_groups(g, associate_tests(g))
    by_id = {x["group_id"]: x for x in res["groups"]}
    assert by_id["shared-foundation"]["test_project_ids"] == ["t/Shared.Tests.csproj"]
    # A.Tests references both A.Core and Shared: it belongs with the application group.
    assert by_id["app-a-app"]["test_project_ids"] == ["t/A.Tests.csproj"]
    assert res["unassigned_projects"][0]["project"] == "t/Orphan.Tests.csproj"


def test_groups_depend_on_shared_foundation():
    g = ProjectGraph(sample_projects())
    res = build_groups(g, associate_tests(g))
    for grp in res["groups"][1:]:
        assert grp["depends_on_group_ids"] == ["shared-foundation"]
        assert grp["shared_project_ids"] == ["s/Shared.csproj"]


def test_windows_platform_propagates_to_group():
    projects = sample_projects() + [proj("w/Win.csproj", refs=["s/Shared.csproj"], tfms=["net8.0-windows"], platform="WINDOWS")]
    g = ProjectGraph(projects)
    res = build_groups(g, associate_tests(g))
    win = next(x for x in res["groups"] if x["group_id"] == "component-win")
    assert win["platform"] == "WINDOWS" and win["group_kind"] == "STANDALONE_LIBRARY"


def test_cycle_is_reported_not_hung():
    g = ProjectGraph([proj("x/X.csproj", refs=["y/Y.csproj"]), proj("y/Y.csproj", refs=["x/X.csproj"])])
    assert g.cycles()
    assert len(g.topo_order()) == 2


def test_temporary_tfms_keep_existing_targets():
    assert temporary_tfms({"target_frameworks": ["net8.0", "netstandard2.0"]}) == ["net8.0", "net10.0", "netstandard2.0"]
    assert temporary_tfms({"target_frameworks": ["net8.0-windows"]}) == ["net8.0-windows", "net10.0-windows"]


def test_parse_diagnostics_and_classes(tmp_path):
    out = (f"{tmp_path}\\src\\B\\P.cs(19,27): error CS0121: The call is ambiguous [{tmp_path}\\src\\B\\B.csproj]\n"
           f"{tmp_path}\\src\\S\\M.cs(18,22): warning CS9258: In language version 14.0 [{tmp_path}\\src\\S\\S.csproj]\n"
           f"{tmp_path}\\src\\S\\M.cs(18,22): warning CS9258: In language version 14.0 [{tmp_path}\\src\\S\\S.csproj]\n"
           f"{tmp_path}\\x.csproj : error NU1101: Unable to find package Foo [{tmp_path}\\x.csproj]\n")
    diags = parse_diagnostics(out, tmp_path)
    assert [(d["code"], d["severity"]) for d in diags] == [("CS0121", "error"), ("CS9258", "warning"), ("NU1101", "error")]
    assert diags[0]["file"] == "src/B/P.cs" and diags[0]["line"] == 19 and diags[0]["project"] == "src/B/B.csproj"
    assert classify("CS0121", "error") == "compile_time_api_break"
    assert classify("NU1101", "error") == "unavailable_package_version"
    assert classify("NETSDK1147", "error") == "workload_missing"
    assert classify("CS1234", "error", "obj/Debug/x.g.cs") == "generated_code_failure"


def test_retarget_only_touches_target_framework_elements(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "A.csproj").write_text(
        "<Project><PropertyGroup><TargetFrameworks>net8.0;netstandard2.0</TargetFrameworks>"
        "<Description>net8.0 library</Description></PropertyGroup></Project>", encoding="utf-8")
    inv = {"files": {"projects": ["a/A.csproj"], "directory_build_props": [], "directory_build_targets": [],
                     "custom_props_targets": []}}
    changes = retarget(tmp_path, inv)
    text = (tmp_path / "a" / "A.csproj").read_text(encoding="utf-8")
    assert "<TargetFrameworks>net10.0;netstandard2.0</TargetFrameworks>" in text
    assert "<Description>net8.0 library</Description>" in text
    assert changes[0]["change_class"] == "PROJECT_LOCAL"


def test_change_classes():
    assert change_class("global.json") == "REPOSITORY_GLOBAL"
    assert change_class("Directory.Packages.props") == "REPOSITORY_GLOBAL"
    assert change_class("src/Directory.Build.props") == "GROUP_SHARED"
    assert change_class("src/A/A.csproj") == "PROJECT_LOCAL"


def test_versions_and_globs():
    assert _ver("6.0.1") < _ver("7.0.0")
    assert _ver("[8.0.1]") == (8, 0, 1)
    assert _glob_match("Program.cs", "**/*.cs") and _glob_match("src/a/b.cs", "**/*.cs")
    assert _glob_match("src/AppA/AppA.Web/Dockerfile", "**/Dockerfile")
    assert _glob_match(".gitlab-ci.yml", ".gitlab-ci.yml")
    assert not _glob_match("src/x.csproj", "**/*.cs")


def test_sdk_list_parsing():
    out = "10.0.102 [C:\\dotnet\\sdk]\nMicrosoft.NETCore.App 8.0.34 [C:\\dotnet\\shared\\Microsoft.NETCore.App]\n"
    items = _versions(out)
    assert items[0] == {"version": "10.0.102", "root": "C:\\dotnet\\sdk"}
    assert items[1]["name"] == "Microsoft.NETCore.App" and items[1]["version"] == "8.0.34"


def test_sample_inventory_and_rules_are_static_facts():
    cats = categorize(SAMPLE)
    assert len(cats["projects"]) == 9
    assert cats["global_json"] == ["global.json"]
    assert cats["directory_packages_props"] == ["Directory.Packages.props"]
    assert cats["ci_files"] == [".gitlab-ci.yml"]
    assert cats["dockerfiles"] == ["src/AppA/AppA.Web/Dockerfile"]
    slnf = parse_solution(SAMPLE / "AppA.slnf", SAMPLE)
    assert "src/Shared/Shared.Core/Shared.Core.csproj" in slnf and len(slnf) == 4
    recipe = load_recipe(ROOT / "recipes")
    inventory = {"projects": [{"id": p} for p in cats["projects"]]}
    found = {f["rule_id"]: f for f in source_and_file_findings(SAMPLE, inventory, recipe)}
    assert set(found) == {"SRC-CS14-FIELD", "FILE-CI-SDK8", "FILE-DOCKER-NET8"}
    assert found["SRC-CS14-FIELD"]["projects"] == ["src/Shared/Shared.Core/Shared.Core.csproj"]
    assert found["FILE-DOCKER-NET8"]["projects"] == ["src/AppA/AppA.Web/AppA.Web.csproj"]


def test_recipe_has_every_required_field():
    recipe = load_recipe(ROOT / "recipes")
    for key in ("recipe_id", "display_name", "chain_group", "chain_order", "source", "target", "prerequisites",
                "applicability", "transformations", "post_actions", "compatibility_checks", "validation",
                "repairs", "rollback", "references", "instruction_keywords", "phases"):
        assert key in recipe, key
    assert recipe["repairs"]["max_attempts"] == 3
    assert [p["id"] for p in recipe["phases"]][0] == "PHASE_0_ANALYSIS"
    assert all({"id", "kind", "title", "repair"} <= set(r) for r in recipe["rules"])


def test_baseline_cache_requires_same_toolchain_and_covering_scope(tmp_path):
    from dmig import cache
    fp = {"dotnet": "d", "selected_sdk": "10.0.102", "runtimes": ["10.0.2", "8.0.34"]}
    bt = {"restore_succeeded": True, "tests": {}}
    cache.save(tmp_path, "abc", fp, bt, {"a", "b"})
    assert cache.load(tmp_path, "abc", fp, {"a"})[0] == bt
    assert cache.load(tmp_path, "abc", fp, {"a", "c"})[0] is None          # scope not covered
    assert cache.load(tmp_path, "abc", dict(fp, selected_sdk="10.0.200"), {"a"})[0] is None  # other SDK
    assert cache.load(tmp_path, "zzz", fp, {"a"})[0] is None               # other code
    cache.save(tmp_path, "whole", fp, bt, None)
    assert cache.load(tmp_path, "whole", fp, {"x", "y"})[0] == bt           # whole-repo baseline covers all


def test_scope_filter_lists_scope_and_its_dependencies(tmp_path):
    import json as _json
    from dmig.buildtest import SCOPE_FILTER, write_scope_filter
    inv = {"solutions": [{"path": "All.sln", "projects": ["a/A.csproj", "s/S.csproj", "b/B.csproj"]}],
           "projects": [{"id": "a/A.csproj", "project_references": ["s/S.csproj"]},
                        {"id": "s/S.csproj", "project_references": []},
                        {"id": "b/B.csproj", "project_references": ["s/S.csproj"]}]}
    entry = write_scope_filter(tmp_path, inv, {"a/A.csproj"})
    assert entry == [SCOPE_FILTER]
    data = _json.loads((tmp_path / SCOPE_FILTER).read_text())
    assert data["solution"]["path"] == "All.sln"
    assert sorted(data["solution"]["projects"]) == [r"a\A.csproj", r"s\S.csproj"]
