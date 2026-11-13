"""No network by default: the tool contacts only the configured
feeds (through dotnet; --offline skips even that) and never the public catalogue unless
--allow-public-nuget is given; a package missing from the local catalogue is PACKAGE_UNAVAILABLE,
with no fallback to public NuGet. The autouse fixture in conftest.py refuses every socket."""
from pathlib import Path

import pytest

from dmig.catalog import LocalCatalog, load_catalog
from dmig.compat import NuGetClient

from conftest import CATALOG, NETWORK_TESTS, NetworkUsed, dotnet_path, requires


def test_default_catalogue_is_local_and_never_public(tmp_path: Path):
    assert isinstance(load_catalog(None, tmp_path), LocalCatalog)
    assert isinstance(load_catalog(str(CATALOG), tmp_path), LocalCatalog)
    assert isinstance(load_catalog("public", tmp_path), LocalCatalog)          # public only with allow_network
    assert isinstance(load_catalog("public", tmp_path, allow_network=True), NuGetClient)


def test_without_a_catalogue_versions_are_unknown_not_guessed(tmp_path: Path):
    c = load_catalog(None, tmp_path)
    assert c.list_versions("Anything") is None and c.newest_allowed("Anything", 10) is None
    assert c.describe()["network"] is False


def test_missing_package_has_no_public_fallback(tmp_path: Path):
    c = LocalCatalog(CATALOG)
    assert c.contains("System.Linq.Async") is False          # public on nuget.org, absent from the mirror
    assert c.newest_allowed("System.Linq.Async") is None      # nothing invented, nothing fetched


def test_the_guard_catches_a_network_call(tmp_path: Path):
    import socket
    with pytest.raises(NetworkUsed):
        socket.create_connection(("example.invalid", 443))


def test_without_dotnet_versions_are_unknown_and_nothing_is_fetched(tmp_path: Path):
    client = NuGetClient(tmp_path, online=True)               # no dotnet: no other way to ask
    assert client.list_versions("Newtonsoft.Json") is None
    assert "METADATA_UNAVAILABLE" in client.failures[0]["error"]


def test_the_tool_has_no_http_client_of_its_own():
    """All package traffic goes through dotnet and the configured feeds."""
    import re
    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [f"{p.name}: {m}" for p in src.rglob("*.py")
                 for m in re.findall(r"^\s*(?:import|from)\s+(urllib|http\.client|socket|ssl|requests)",
                                     p.read_text(encoding="utf-8"), re.M)]
    assert offenders == []


@requires("sdk10", "git worktree")
def test_doctor_and_analysis_make_no_network_call(sample_repo: Path, tmp_path: Path):
    from dmig.analysis import run_analysis
    from dmig.doctor import run_doctor
    from dmig.dotnet import Runner
    if NETWORK_TESTS:  # default doctor: only the configured feeds are probed, through dotnet
        report = run_doctor(sample_repo, tmp_path / "doc", Runner(dotnet_path(), tmp_path / "doc" / "logs"),
                            catalog_spec=str(CATALOG))
        assert report["execution_environment"]["network_allowed"] is False  # no public catalogue
        configured = {s["url"] for s in report["execution_environment"]["package_sources"]}
        for log in (tmp_path / "doc" / "logs").glob("*feed-probe*.log"):
            cmd = log.read_text(encoding="utf-8").splitlines()[0]
            assert cmd.split("--source ")[1].split()[0] in configured            # only a configured feed
    # --offline: no feed contacted at all, and never the public catalogue.
    report = run_doctor(sample_repo, tmp_path / "doc2", Runner(dotnet_path(), tmp_path / "doc2" / "logs"),
                        catalog_spec=str(CATALOG), probe_feeds=False)
    assert report["execution_environment"]["network_allowed"] is False
    out = tmp_path / "a"
    run_analysis(sample_repo, out, Runner(dotnet_path(), out / "logs"),
                 {"run_baseline": False, "allow_reversible_experiments": False, "probe_feeds": False})
    logs = [p.name for p in (tmp_path / "doc2" / "logs").iterdir()] + [p.name for p in (out / "logs").iterdir()]
    assert not [n for n in logs if "feed-probe" in n or "package-search" in n]
