"""Several .NET installations side by side: runtimes from another dotnet folder
are combined with the active SDK without changing either installation. Fake installations, no dotnet."""
from pathlib import Path

from dmig.dotnet_roots import EXE, has_runtime, resolve


def _install(root: Path, runtimes: list[str], sdk: str | None = None) -> Path:
    (root / EXE).parent.mkdir(parents=True, exist_ok=True)
    (root / EXE).write_bytes(b"fake")
    for v in runtimes:
        (root / "shared" / "Microsoft.NETCore.App" / v).mkdir(parents=True)
    if sdk:
        (root / "sdk" / sdk).mkdir(parents=True)
    return root


def test_runtime_8_from_another_folder_is_combined_with_the_active_sdk(tmp_path: Path):
    active = _install(tmp_path / "sdk10", ["10.0.2"], sdk="10.0.102")
    other = _install(tmp_path / "rt8", ["8.0.11"])
    exe, info = resolve(str(active / EXE), [str(other)], tmp_path / "cache")
    combined = Path(exe).parent
    assert info["found_in"] == {"8": str(other)} and info["combined"] == str(combined)
    assert has_runtime(combined, "8") and has_runtime(combined, "10")
    assert (combined / "sdk" / "10.0.102").is_dir()                   # the active SDK, linked
    assert sorted(p.name for p in (active / "shared" / "Microsoft.NETCore.App").iterdir()) == ["10.0.2"]  # unchanged
    assert resolve(str(active / EXE), [str(other)], tmp_path / "cache")[0] == exe  # built once, reused


def test_nothing_is_combined_when_the_active_dotnet_has_everything(tmp_path: Path):
    active = _install(tmp_path / "full", ["8.0.11", "10.0.2"], sdk="10.0.102")
    exe, info = resolve(str(active / EXE), [], tmp_path / "cache")
    assert exe == str(active / EXE) and info["combined"] is None


def test_a_missing_runtime_nowhere_on_the_machine_is_reported_not_invented(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DMIG_DOTNET_ROOTS", "")
    monkeypatch.setattr("dmig.dotnet_roots.candidate_roots", lambda extra=None: [])
    active = _install(tmp_path / "sdk10", ["10.0.2"], sdk="10.0.102")
    exe, info = resolve(str(active / EXE), [], tmp_path / "cache")
    assert exe == str(active / EXE) and info["found_in"] == {} and info["combined"] is None
