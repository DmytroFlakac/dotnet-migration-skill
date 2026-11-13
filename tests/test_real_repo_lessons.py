"""Lessons from the Bitwarden runs, pinned as fast unit tests (no dotnet needed).

Each test names the real failure it guards against.
"""
import json
from pathlib import Path

from dmig.analysis import audit_drift
from dmig.buildtest import MISSING_FRAMEWORK
from dmig.cli import _runner
from dmig.experiment import compare_tests, compile_wall, package_wall, raise_downgrade_pins
from dmig.globalphase import bump_msbuild_sdk_pins


def _err(code, message="", file="", project="p.csproj"):
    return {"code": code, "message": message, "file": file, "line": 1, "project": project, "severity": "error"}


def test_advisory_drift_only_when_restore_fails_on_audit_codes_alone():
    # Vulnerabilities published after the commit broke the unchanged .NET 8 restore.
    b = {"restore_succeeded": False, "errors": [
        _err("NU1902", "Warning As Error: Package 'MessagePack' 2.5.192 has a known moderate severity vulnerability"),
        _err("MSB3073", "cargo build failed")]}
    assert audit_drift(b) == ["MessagePack 2.5.192"]
    b["errors"].append(_err("NU1605", "downgrade"))      # a real package problem: not drift
    assert audit_drift(b) == []
    assert audit_drift({"restore_succeeded": True, "errors": []}) == []


def test_implementation_runs_keep_the_analysis_audit_decision(tmp_path: Path):
    pre = {"limitations": [{"code": "BASELINE_NUGET_AUDIT_DRIFT"}]}
    assert _runner("dotnet", tmp_path, pre).env.get("NuGetAudit") == "false"
    assert "NuGetAudit" not in _runner("dotnet", tmp_path, {"limitations": []}).env or \
        _runner("dotnet", tmp_path, {"limitations": []}).env.get("NuGetAudit") != "false"


def test_walls_are_recognised():
    # Layer 1 stopped at restore on NU1510/NU1605; layer 2 on SYSLIB0057; layer 3 on ASPDEPR005.
    restore = {"restore_succeeded": False, "errors": [_err("NU1510", "Warning As Error: will not be pruned"),
                                                      _err("NU1605", "Warning As Error: Detected package downgrade")]}
    assert package_wall(restore) == ["NU1510", "NU1605"]
    assert package_wall({"restore_succeeded": False, "errors": [_err("NU1101", "Unable to find package")]}) == []
    build = {"errors": [_err("SYSLIB0057"), _err("ASPDEPR005"), _err("CS0246")]}
    assert compile_wall(build) == ["ASPDEPR005", "SYSLIB0057"]   # CS0246 is a real error, never relaxed


def test_downgrade_pin_is_raised_in_the_copy(tmp_path: Path):
    (tmp_path / "Core.csproj").write_text(
        '<PackageReference Include="Microsoft.Extensions.Caching.Memory" Version="8.0.1" />', encoding="utf-8")
    errors = [_err("NU1605", "Warning As Error: Detected package downgrade: Microsoft.Extensions.Caching.Memory "
                             "from 9.0.13 to 8.0.1. Reference the package directly")]
    fixes = raise_downgrade_pins(tmp_path, errors)
    assert fixes[0]["before"] == "8.0.1" and fixes[0]["after"] == "9.0.13"
    assert 'Version="9.0.13"' in (tmp_path / "Core.csproj").read_text(encoding="utf-8")


def test_data_driven_test_names_are_compared_by_method():
    # AutoFixture prints random arguments: names differ on every run.
    base = {"tests": {"results": [{"project": "T", "test": "A.B.M(x: 1)", "outcome": "passed"},
                                  {"project": "T", "test": "A.B.M(x: 2)", "outcome": "passed"}]}}
    now = {"tests": {"results": [{"project": "T", "test": "A.B.M(x: 7)", "outcome": "failed", "message": "boom"}]}}
    c = compare_tests(base, now)
    assert len(c["regressions"]) == 1 and c["regressions"][0]["test"] == "A.B.M"
    assert not c["not_run_in_experiment"] and not c["new_tests"]


def test_missing_shared_framework_is_detected_in_test_output():
    out = "Framework: 'Microsoft.AspNetCore.App', version '8.0.0' (x64)\n.NET location: X"
    assert MISSING_FRAMEWORK.findall(out) == [("Microsoft.AspNetCore.App", "8.0.0")]


from dmig.catalog import PackageCatalog  # noqa: E402


class _FakeNuGet(PackageCatalog):
    """A public catalogue: every listed version is available and approved."""
    def list_versions(self, package):
        return {"microsoft.build.sql": ["1.0.0", "2.1.0", "2.3.0", "3.0.0-preview1"]}.get(package.lower(), [])


def test_msbuild_sdk_pin_is_raised_only_for_the_sdk_that_broke(tmp_path: Path):
    (tmp_path / "global.json").write_text(json.dumps({"sdk": {"version": "10.0.100"}, "msbuild-sdks": {
        "Microsoft.Build.Traversal": "4.1.0", "Microsoft.Build.Sql": "1.0.0"}}), encoding="utf-8")
    errors = [_err("MSB4019", "imported project not found", file="C:/nuget/microsoft.build.sql/1.0.0/Sdk/Sdk.targets")]
    bumps = bump_msbuild_sdk_pins(tmp_path, {"files": {"global_json": ["global.json"]}}, errors, _FakeNuGet())
    assert bumps == [{"file": "global.json", "kind": "msbuild_sdk_pin", "package": "Microsoft.Build.Sql",
                      "before": "1.0.0", "after": "2.3.0", "rule": "SDK-MSBUILD-SDK-TOO-OLD"}]  # newest stable
    pins = json.loads((tmp_path / "global.json").read_text(encoding="utf-8"))["msbuild-sdks"]
    assert pins == {"Microsoft.Build.Traversal": "4.1.0", "Microsoft.Build.Sql": "2.3.0"}


def test_credentials_never_reach_logs_or_patches(tmp_path: Path):
    from dmig.dotnet import Runner
    from dmig.secrets import added_lines, find_secrets, redact
    s = "Server=db;User Id=sa;Password=Sup3rS3cret!;TrustServerCertificate=True"
    assert "Sup3rS3cret" not in redact(s) and "Server=db" in redact(s)
    assert "abc123token" not in redact("restore https://user:abc123token@feed.example/v3/index.json")
    assert "ghp_" not in redact("token ghp_" + "a" * 36)
    assert "x" * 20 not in redact('<add key="ClearTextPassword" value="' + "x" * 20 + '" />')
    patch = "+++ b/NuGet.config\n+<add key=\"ClearTextPassword\" value=\"" + "y" * 20 + "\" />\n-old"
    assert find_secrets(added_lines(patch)) == ["nuget cleartext password"]
    assert find_secrets(added_lines("+<TargetFramework>net10.0</TargetFramework>")) == []
    r = Runner("python", tmp_path / "logs").run(["-c", "print('Password=Sup3rS3cret!')"], tmp_path, "echo")
    assert "Sup3rS3cret" not in Path(r.log_path).read_text(encoding="utf-8") and "Sup3rS3cret" not in r.output


def test_theory_rows_fold_and_environment_failures_are_recognised():
    from dmig.experiment import INFRA_FAILURE, _method
    assert _method("Ns.C.M [SqlServer]") == "Ns.C.M" and _method("Ns.C.M(x: 1)") == "Ns.C.M"
    assert INFRA_FAILURE.search("Microsoft.Data.SqlClient.SqlException : Execution Timeout Expired.")
    assert INFRA_FAILURE.search("Transaction (Process ID 63) was deadlocked on lock resources")
    assert not INFRA_FAILURE.search("Assert.Equal() Failure: Values differ")


def test_time_budget_stops_dotnet_but_never_git_cleanup(tmp_path: Path):
    from dmig.dotnet import Runner
    r = Runner("python", tmp_path / "logs")
    r.set_budget(0.0001)                                   # already exhausted
    res = r.run(["-c", "print(1)"], tmp_path, "slow")
    assert not res.ok and r.budget_exhausted and "time budget" in res.output
    git = r.run(["--version"], tmp_path, "git", program="git")
    assert git.ok                                          # cleanup commands still run


def test_edits_keep_the_files_style(tmp_path: Path):
    # A two-value global.json edit became a whole-file diff (LF file rewritten with CRLF on Windows).
    from dmig.edits import read, write_like
    from dmig.globalphase import bump_msbuild_sdk_pins
    g = tmp_path / "global.json"
    g.write_bytes(b'{\n  "sdk": {\n    "version": "10.0.100"\n  },\n  "msbuild-sdks": {\n    "Microsoft.Build.Sql": "1.0.0"\n  }\n}\n')
    errors = [_err("MSB4019", "x", file="C:/nuget/microsoft.build.sql/1.0.0/Sdk/Sdk.targets")]
    bump_msbuild_sdk_pins(tmp_path, {"files": {"global_json": ["global.json"]}}, errors, _FakeNuGet())
    assert g.read_bytes() == b'{\n  "sdk": {\n    "version": "10.0.100"\n  },\n  "msbuild-sdks": {\n    "Microsoft.Build.Sql": "2.3.0"\n  }\n}\n'
    crlf = tmp_path / "a.props"
    crlf.write_bytes("\ufeff<A>\r\n  <B>1</B>\r\n</A>".encode("utf-8"))   # BOM, CRLF, no final newline
    original = read(crlf)
    write_like(crlf, original.replace("1", "2"), original)
    assert crlf.read_bytes() == "\ufeff<A>\r\n  <B>2</B>\r\n</A>".encode("utf-8")


def test_inbox_package_kept_for_net8_silences_nu1510_only_there(tmp_path: Path):
    # In a net8.0;net10.0 project the SDK still raised NU1510 for the net8.0-only reference.
    from dmig.implement import repair_remove_package
    proj = tmp_path / "Core.csproj"
    proj.write_bytes(b'<Project>\r\n  <PropertyGroup>\r\n    <TargetFrameworks>net8.0;net10.0</TargetFrameworks>\r\n'
                     b'  </PropertyGroup>\r\n  <ItemGroup>\r\n    <PackageReference Include="System.Text.Json" Version="8.0.5" />\r\n'
                     b'  </ItemGroup>\r\n</Project>\r\n')
    assert repair_remove_package(tmp_path, "Core.csproj", "System.Text.Json") == ["Core.csproj"]
    text = proj.read_bytes().decode()
    assert 'Version="8.0.5" Condition="$(TargetFramework.StartsWith(\'net8.0\'))" NoWarn="NU1510" />' in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")   # CRLF kept everywhere
