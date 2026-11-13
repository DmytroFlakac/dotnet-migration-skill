"""Repository-global phases: fast unit tests (no dotnet). The full lifecycle, with real builds,
runs inside tests/test_implementation.py::test_groups_migrate_in_order_and_patches_apply."""
from dmig.globalphase import _github_add_sdk10, phase1_edits, phase8_edits, run_global_phase, yaml_ok

CI = "image: mcr.microsoft.com/dotnet/sdk:8.0\n\nstages: [build]\n\nbuild:\n  script:\n    - dotnet build\n"


def test_gitlab_ci_phase1_and_phase8_round_trip(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text(CI)
    (tmp_path / "global.json").write_text('{"sdk": {"version": "8.0.400"}}')
    inv8 = {"files": {"global_json": ["global.json"]}, "ci_entrypoints": [".gitlab-ci.yml"],
            "projects": [{"id": "a", "target_frameworks": ["net8.0"]}]}
    phase1_edits(tmp_path, inv8, [{"version": "10.0.102", "root": "x"}])
    text = (tmp_path / ".gitlab-ci.yml").read_text()
    assert "sdk:10.0" in text and "--channel 8.0 --runtime dotnet" in text and yaml_ok(text) is None
    gj = (tmp_path / "global.json").read_text()
    assert '"version": "10.0.100"' in gj and '"rollForward": "latestFeature"' in gj
    # Once nothing targets net8.0, PHASE_8 removes exactly what PHASE_1 added.
    inv10 = dict(inv8, projects=[{"id": "a", "target_frameworks": ["net10.0"]}])
    phase8_edits(tmp_path, inv10)
    assert (tmp_path / ".gitlab-ci.yml").read_text() == CI.replace("sdk:8.0", "sdk:10.0")


def test_phase1_without_net8_targets_adds_no_runtime(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text(CI)
    inv = {"files": {"global_json": []}, "ci_entrypoints": [".gitlab-ci.yml"],
           "projects": [{"id": "a", "target_frameworks": ["net10.0"]}]}
    phase1_edits(tmp_path, inv, [{"version": "10.0.102", "root": "x"}])
    assert (tmp_path / ".gitlab-ci.yml").read_text() == CI.replace("sdk:8.0", "sdk:10.0")


def test_phase8_refuses_while_net8_remains(tmp_path):
    changes, notes = phase8_edits(tmp_path, {"ci_entrypoints": [], "projects": [{"id": "x", "target_frameworks": ["net8.0"]}]})
    assert changes == [] and "net8.0 still targeted" in notes[0]


def test_github_actions_keeps_8_while_needed():
    yml = "steps:\n  - uses: actions/setup-dotnet@v4\n    with:\n      dotnet-version: '8.0.x'\n"
    out = _github_add_sdk10(yml, keep8=True)
    assert "dotnet-version: |\n        8.0.x\n        10.0.x\n" in out
    assert yaml_ok(out) is None


def test_gitlab_custom_tags_do_not_break_validation():
    assert yaml_ok("job:\n  script:\n    - !reference [.setup, script]\n") is None


def test_global_phase_needs_explicit_approval(tmp_path):
    res = run_global_phase(tmp_path, tmp_path / "out", None, {"plan_id": "p", "repository_commit": "c"},
                           "PHASE_1_SDK_GLOBAL", {"approved": False}, None)
    assert res["status"] == "REJECTED" and "approval" in res["unresolved_items"][0]


def test_phase1_confirms_when_no_job_overrides_before_script(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text(CI)
    inv = {"files": {"global_json": []}, "ci_entrypoints": [".gitlab-ci.yml"],
           "projects": [{"id": "a", "target_frameworks": ["net8.0"]}]}
    _, notes = phase1_edits(tmp_path, inv, [{"version": "10.0.102", "root": "x"}])
    assert any("no job defines its own before_script" in n for n in notes)


def test_phase8_folds_target_frameworks_back_into_the_central_file(tmp_path):
    (tmp_path / "Directory.Build.props").write_text(
        "<Project>\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n")
    projects = {
        "a/A.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net10.0</TargetFramework>\n'
                      "  </PropertyGroup>\n</Project>\n",
        "b/B.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework></TargetFramework>\n'
                      "    <TargetFramework>net10.0</TargetFramework>\n    <OutputType>Exe</OutputType>\n  </PropertyGroup>\n</Project>\n",
        "w/W.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net10.0-windows</TargetFramework>\n'
                      "  </PropertyGroup>\n</Project>\n",
    }
    for rel, text in projects.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    inv = {"ci_entrypoints": [], "files": {"projects": list(projects)},
           "projects": [{"id": rel, "target_frameworks": ["net10.0-windows" if "W" in rel else "net10.0"]} for rel in projects]}
    changes, _ = phase8_edits(tmp_path, inv)
    assert "<TargetFramework>net10.0</TargetFramework>" in (tmp_path / "Directory.Build.props").read_text()
    a = (tmp_path / "a/A.csproj").read_text()
    assert "TargetFramework" not in a and "PropertyGroup" not in a  # emptied group removed too
    b = (tmp_path / "b/B.csproj").read_text()
    assert "TargetFramework" not in b and "<OutputType>Exe</OutputType>" in b
    assert "net10.0-windows" in (tmp_path / "w/W.csproj").read_text()
    assert {c["kind"] for c in changes} == {"central_target_framework", "remove_redundant_target_framework"}


def test_phase1_lists_msbuild_sdk_pins(tmp_path):
    (tmp_path / "global.json").write_text('{"sdk": {"version": "8.0.100"}, "msbuild-sdks": {"Microsoft.Build.Sql": "1.0.0"}}')
    inv = {"files": {"global_json": ["global.json"]}, "ci_entrypoints": [], "projects": []}
    _, notes = phase1_edits(tmp_path, inv, [{"version": "10.0.102", "root": "x"}])
    assert any("Microsoft.Build.Sql 1.0.0" in n for n in notes)
