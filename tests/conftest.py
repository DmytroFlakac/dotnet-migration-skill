import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SAMPLE = ROOT / "samples" / "synthetic-monorepo"
CATALOG = ROOT / "fixtures" / "package-catalog.json"


class NetworkUsed(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The whole suite runs with Python networking disabled (the tool makes no network call of its
    own; package traffic goes through dotnet and the configured feeds). dotnet child processes are not
    affected; tests that need a feed are skipped unless DMIG_TEST_NETWORK=1 or a local DMIG_NUGET_SOURCE."""
    import socket

    def refuse(*args, **kwargs):
        raise NetworkUsed(f"the tool has no network client of its own, yet a connection was attempted: {args[1:] or kwargs}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


_DOTNET: list = []


def dotnet_path() -> str | None:
    """dotnet with SDK 10 (+ runtime 8 for net8.0 tests): $DMIG_DOTNET, else PATH; runtimes found in
    other dotnet folders (or $DMIG_DOTNET_ROOTS) are combined with it, as the tool does."""
    if not _DOTNET:
        from dmig.dotnet_roots import resolve
        found = os.environ.get("DMIG_DOTNET") or shutil.which("dotnet")
        _DOTNET.append(resolve(found)[0] if found else None)
    return _DOTNET[0]


# Git hooks and fsmonitor scripts off for every git command the tests start (Git for Windows runs
# them through sh, which locked-down machines cannot start). The tool itself does the same (dotnet.NO_HOOKS).
from dmig.dotnet import _EMPTY_HOOKS  # noqa: E402
os.environ.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath",
                   "GIT_CONFIG_VALUE_0": str(_EMPTY_HOOKS), "GIT_CONFIG_KEY_1": "core.fsmonitor",
                   "GIT_CONFIG_VALUE_1": "false"})

NUGET_SOURCE = os.environ.get("DMIG_NUGET_SOURCE")  # one feed (URL or folder) for the sample, else machine feeds
NETWORK_TESTS = os.environ.get("DMIG_TEST_NETWORK") == "1"  # tests may contact package feeds


def sample_nuget_config() -> str | None:
    """The sample's NuGet.config when DMIG_NUGET_SOURCE is set: only that feed."""
    if not NUGET_SOURCE:
        return None
    return ('<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
            f'    <add key="dmig-test-feed" value="{NUGET_SOURCE}" />\n  </packageSources>\n</configuration>\n')


_CAPS: dict | None = None


def capabilities() -> dict:
    """What this machine can do, probed once. Tests skip on what is missing."""
    global _CAPS
    if _CAPS is not None:
        return _CAPS
    import tempfile
    from dmig.dotnet import Runner
    from dmig.preflight import probe_sources_with_dotnet
    caps = {"sdk10": False, "runtime8": False, "reachable configured feed": False, "git worktree": False}
    dn = dotnet_path()
    if dn:
        r = Runner(dn, None)
        caps["sdk10"] = any(l.startswith("10.") for l in r.run(["--list-sdks"], ROOT, "sdks", 60).output.splitlines())
        caps["runtime8"] = "Microsoft.NETCore.App 8." in r.run(["--list-runtimes"], ROOT, "runtimes", 60).output
        # Tests never touch the network unless asked (a blocked feed makes restore hang for minutes):
        # a local feed folder in DMIG_NUGET_SOURCE needs no network; a URL or the machine's own feeds
        # are used only with DMIG_TEST_NETWORK=1.
        if NUGET_SOURCE and Path(NUGET_SOURCE).is_dir():
            caps["reachable configured feed"] = True
        elif NETWORK_TESTS:
            with tempfile.TemporaryDirectory() as q:
                cfg = sample_nuget_config() or (SAMPLE / "NuGet.config").read_text(encoding="utf-8")
                (Path(q) / "NuGet.config").write_text(cfg, encoding="utf-8")
                feeds = probe_sources_with_dotnet(r, Path(q)) or []
                caps["reachable configured feed"] = any(s.get("status") in {"OK", "LOCAL_PATH"} for s in feeds)
    if shutil.which("git"):
        with tempfile.TemporaryDirectory() as d:
            g = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
            ok = all(subprocess.run(g + a, cwd=d, capture_output=True).returncode == 0 for a in
                     (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"],
                      ["worktree", "add", "-q", "--detach", str(Path(d) / "wt"), "HEAD"]))
            caps["git worktree"] = ok
            subprocess.run(["git", "worktree", "remove", "--force", str(Path(d) / "wt")], cwd=d, capture_output=True)
    _CAPS = caps
    return caps


def requires(*names: str):
    """Skip unless every named capability is present; the reason names the missing ones.
    Names: sdk10, runtime8, "reachable configured feed", "git worktree"."""
    missing = [n for n in names if not capabilities().get(n)]
    hints = {"reachable configured feed": "reachable configured feed (network tests are off: set DMIG_TEST_NETWORK=1, "
                                          "or DMIG_NUGET_SOURCE to a local feed folder)",
             "runtime8": "runtime8 (no .NET 8 runtime found; pass its folder with DMIG_DOTNET_ROOTS)"}
    return pytest.mark.skipif(bool(missing), reason="missing capability: " + ", ".join(hints.get(m, m) for m in missing))


FULL = ("sdk10", "runtime8", "reachable configured feed", "git worktree")  # build + test the sample


def _tree_state(folder: Path) -> list[tuple]:
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


@pytest.fixture(scope="session", autouse=True)
def samples_stay_untouched():
    """Tests work on temporary copies only: samples/ is never changed (no git init, no outputs inside)."""
    before = _tree_state(ROOT / "samples")
    yield
    after = _tree_state(ROOT / "samples")
    assert after == before, "a test changed samples/: " + ", ".join(
        sorted({x[0] for x in set(after) ^ set(before)})[:10])


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A fresh git repository containing the synthetic monorepo, committed once."""
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("bin", "obj", "TestResults"))
    if sample_nuget_config():
        (repo / "NuGet.config").write_text(sample_nuget_config(), encoding="utf-8")
    git = ["git", "-c", "core.autocrlf=false", "-c", "user.email=test@example.invalid", "-c", "user.name=test"]
    subprocess.run(git + ["init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "synthetic monorepo"], cwd=repo, check=True)
    return repo


def pytest_addoption(parser):
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="rewrite tests/golden/*.json from the current outputs")
