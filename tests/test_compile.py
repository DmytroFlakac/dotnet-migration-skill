"""Every Python file in the repository compiles (review: acceptance test 1)."""
import compileall
import re
import sys

from conftest import ROOT


def test_every_python_file_compiles():
    ok = compileall.compile_dir(str(ROOT), quiet=1, force=True, rx=re.compile(r"[\\/](\.git|dist|\.venv|venv)[\\/]"))
    assert ok, "a .py file does not compile (see the output above)"


def test_every_python_file_compiles_with_warnings_as_errors():
    """Python 3.12 turns invalid escape sequences into SyntaxWarning (later SyntaxError): none allowed."""
    import warnings
    skip = re.compile(r"[\\/](\.git|dist|\.venv|venv|__pycache__)[\\/]")
    bad = []
    for path in ROOT.rglob("*.py"):
        if skip.search(str(path)):
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (SyntaxError, SyntaxWarning) as exc:
                bad.append(f"{path.relative_to(ROOT)}: {exc}")
    assert bad == []


def test_python_version_matches_requirements():
    assert sys.version_info >= (3, 11)
