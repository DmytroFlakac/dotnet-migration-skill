"""Builds the self-contained skill folders (and a .zip of each):

  dist/dotnet-8-to-10-migration/   SKILL.md, references/, scripts/dmig/, scripts/obsreport/,
                                   recipes/, schemas/, requirements.txt
  dist/obsidian-report/            SKILL.md, references/, scripts/obsreport/  (no dependencies)

The analyser finds recipes/ and schemas/ two levels above scripts/dmig/.
Run: python scripts/package_skill.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _fresh(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _zip(path: Path) -> str:
    return shutil.make_archive(str(path), "zip", path.parent, path.name)


def migration() -> Path:
    dist = _fresh(ROOT / "dist" / "dotnet-8-to-10-migration")
    shutil.copy2(ROOT / "skill" / "SKILL.md", dist / "SKILL.md")
    shutil.copytree(ROOT / "skill" / "references", dist / "references")
    shutil.copytree(ROOT / "src" / "dmig", dist / "scripts" / "dmig", ignore=IGNORE)
    shutil.copytree(ROOT / "src" / "obsreport", dist / "scripts" / "obsreport", ignore=IGNORE)
    shutil.copytree(ROOT / "recipes", dist / "recipes", ignore=IGNORE)
    shutil.copytree(ROOT / "fixtures", dist / "fixtures")  # the mock package catalogue
    shutil.copytree(ROOT / "schemas", dist / "schemas", ignore=shutil.ignore_patterns("make_schemas.py"))
    (dist / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs" / "integration-contract.md", dist / "docs" / "integration-contract.md")
    for name in ("requirements.txt", "requirements-dev.txt", "requirements.lock", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, dist / name)
    # `dmig` without installing anything: starter files next to SKILL.md (Windows and Linux/macOS)
    for name in ("dmig", "dmig.cmd"):
        shutil.copy2(ROOT / "skill" / "launchers" / name, dist / name)
    (dist / "dmig").chmod(0o755)
    return dist


def obsidian_report() -> Path:
    dist = _fresh(ROOT / "dist" / "obsidian-report")
    src = ROOT / "skills" / "obsidian-report"
    shutil.copy2(src / "SKILL.md", dist / "SKILL.md")
    shutil.copytree(src / "references", dist / "references")
    shutil.copytree(ROOT / "src" / "obsreport", dist / "scripts" / "obsreport", ignore=IGNORE)
    return dist


def main() -> None:
    for dist in (migration(), obsidian_report()):
        files = sum(1 for p in dist.rglob("*") if p.is_file())
        print(f"{dist} ({files} files)\n{_zip(dist)}")


if __name__ == "__main__":
    main()
