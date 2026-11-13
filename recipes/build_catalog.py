"""Builds recipes/rules/catalog.yaml from Microsoft's breaking-change lists in recipes/sources/.

Every official entry becomes one rule, of one of three kinds, chosen to keep false alarms low:
  diagnostic  an obsolete-API warning ID (SYSLIB…, EXTOBS…): exact — matched against the warnings the
              net10.0 experiment produces; also a generic rule for CS0618/CS0619/ASPDEPR*.
  source      a distinctive API name in the entry's title (e.g. BufferedStream): a code search whose hits
              are "check this usage", never an automatic failure.
  checklist   everything else: shown as a review item only when the repository uses that technology
              (EF Core entries only with EF Core, ASP.NET Core only with web projects, …).
Hand-written rules in net8-to-net10.yaml win: catalogue entries with the same Microsoft page are skipped.

Run: python recipes/build_catalog.py
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

import yaml

HERE = Path(__file__).parent
SRC = HERE / "sources"
OUT = HERE / "rules" / "catalog.yaml"

AREA_APPLIES = {
    "ASP.NET Core": "web", "Containers": "containers", "Entity Framework Core": "efcore",
    "Windows Forms": "winforms", "Windows Presentation Foundation (WPF)": "wpf", "Visual Basic": "vb",
}
CHANGE = {"behavioral change": "behavioral", "source incompatible": "source", "binary incompatible": "binary",
          "binary/source incompatible": "binary_source"}
STOP = {"PowerShell", "JavaScript", "NuGet", "MSBuild", "OpenApi", "OpenAPI", "WebAssembly", "TypeScript", "GitHub",
        "macOS", "IntelliSense", "NativeAOT", "JsonSerializer", "HttpClient", "ArgumentException", "ToString",
        "GetHashCode", "IEnumerable", "ReadOnlySpan", "CancellationToken", "DateTime", "TimeSpan", "StringBuilder",
        "ArgumentNullException", "ArgumentOutOfRangeException", "InvalidOperationException", "ExecuteAsync",
        "SecretKey", "DuplicateHandle", "RuntimeHelpers"}
# \x60 is the backtick, written as an escape so a copy that loses or converts backticks (e.g. through a
# Markdown viewer) cannot unbalance the parentheses.
CODE_TOKEN = re.compile(r"\x60([^\x60]+)\x60|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)(?:\.[A-Z]\w+)?\b")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _clean(md: str) -> str:
    md = re.sub(r"<xref:([^>?]+)(\?[^>]*)?>", lambda m: m.group(1).split(".")[-1] if "displayProperty" not in (m.group(2) or "")
                else ".".join(m.group(1).split(".")[-2:]), md)
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)
    return md.replace("\\|", "|").strip()


def _sections(text: str):
    """(section heading, table row) pairs."""
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("| [") and section:
            yield section, line


def _code_names(title: str) -> list[str]:
    names = []
    for m in CODE_TOKEN.finditer(title):
        tok = (m.group(1) or m.group(2) or "").split("(")[0].split("<")[0]
        first = tok.split(".")[0]
        if re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+", first) and first not in STOP and len(first) >= 8:
            names.append(first)
    return list(dict.fromkeys(names))


def core_rules(path: Path, version: str) -> list[dict]:
    out = []
    for section, row in _sections(path.read_text(encoding="utf-8")):
        m = re.match(r"\|\s*\[(.+?)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|", row)
        if not m:
            continue
        title, link, change = _clean(m.group(1)), m.group(2), m.group(3).strip().lower()
        if link.startswith("/"):  # links into other doc sets (e.g. ASP.NET Core overview)
            url = "https://learn.microsoft.com" + link.removesuffix(".md")
        else:
            url = "https://learn.microsoft.com/dotnet/core/compatibility/" + link.removesuffix(".md")
        out.append(_rule(f"CAT-NET{version.split('.')[0]}", title, section, CHANGE.get(change, change), version, url))
    return out


def aspnet_rules(path: Path, version: str) -> list[dict]:
    out = []
    for row in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*\[(.+?)\]\(([^)#]+)\.md\)\s*\|\s*([^|]+?)\s*\|", row)
        if m:
            url = f"https://learn.microsoft.com/aspnet/core/breaking-changes/{version.split('.')[0]}/{m.group(2)}"
            out.append(_rule("CAT-ASP" + version.split(".")[0], _clean(m.group(1)), "ASP.NET Core",
                             CHANGE.get(m.group(3).strip().lower(), m.group(3).strip().lower()), version, url))
    return out


def ef_rules(path: Path, version: str) -> list[dict]:
    out = []
    for row in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*\[(.+?)\]\(#([^)]+)\)\s*\|\s*([^|]+?)\s*\|", row)
        if m:
            url = f"https://learn.microsoft.com/ef/core/what-is-new/ef-core-{version}/breaking-changes#{m.group(2)}"
            r = _rule("CAT-EF" + version.split(".")[0], _clean(m.group(1)), "Entity Framework Core",
                      "impact_" + m.group(3).strip().lower(), version, url)
            out.append(r)
    return out


def obsoletion_rules(path: Path) -> list[dict]:
    out = []
    for row in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*\[((?:SYSLIB|EXTOBS)\d+)\]\(([^)]+)\.md\)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\|\s*$", row)
        if not m:
            continue
        diag, slug, level, desc = m.group(1), m.group(2), m.group(3), _clean(m.group(4))
        out.append({
            "id": f"DIAG-{diag}", "kind": "diagnostic", "title": f"{diag}: {desc[:160]}",
            "area": "Obsolete APIs", "change_type": "source", "introduced_in": "see reference",
            "expected_diagnostics": [diag], "effect": "obsolete_warning" if level.lower() == "warning" else "build_break",
            "failure_class": "analyzer_diagnostic", "applies_if": "any",
            "repair": {"id": f"obsolete-{diag.lower()}", "change_class": "PROJECT_LOCAL",
                       "action": f"Check: replace the obsolete API as described in Microsoft's {diag} page."},
            "reference": f"https://learn.microsoft.com/dotnet/fundamentals/syslib-diagnostics/{slug}",
            "source": "catalog"})
    out.append({
        "id": "DIAG-OBSOLETE-GENERIC", "kind": "diagnostic",
        "title": "Obsolete API used (generic obsoletion warning)", "area": "Obsolete APIs", "change_type": "source",
        "introduced_in": "see warning", "expected_diagnostics": ["CS0618", "CS0619"],
        "expected_diagnostic_prefixes": ["ASPDEPR", "EXTOBS", "SYSLIB"], "effect": "obsolete_warning",
        "failure_class": "analyzer_diagnostic", "applies_if": "any",
        "repair": {"id": "obsolete-generic", "change_class": "PROJECT_LOCAL",
                   "action": "Check: the warning text names the replacement API."},
        "reference": "https://learn.microsoft.com/dotnet/fundamentals/syslib-diagnostics/obsoletions-overview",
        "source": "catalog"})
    return out


def _norm(url: str) -> str:
    """Resolve ../ segments Microsoft's lists use for entries that live under another version."""
    head, path = url.split("://", 1)[0], url.split("://", 1)[1]
    host, _, rest = path.partition("/")
    anchor = ""
    if "#" in rest:
        rest, anchor = rest.split("#", 1)
        anchor = "#" + anchor
    return f"{head}://{host}/{posixpath.normpath(rest)}{anchor}"


def _rule(prefix: str, title: str, area: str, change: str, version: str, url: str) -> dict:
    applies = AREA_APPLIES.get(area, "any")
    rule = {"id": f"{prefix}-{_slug(title)}", "title": title, "area": area, "change_type": change,
            "introduced_in": f"net{version}", "applies_if": applies, "reference": _norm(url), "source": "catalog",
            "repair": {"id": "review", "change_class": "PROJECT_LOCAL",
                       "action": "Check: read Microsoft's page and decide whether this repository is affected."}}
    names = _code_names(title)
    if names and change in {"behavioral", "source", "binary", "binary_source"} and area not in {"SDK and MSBuild", "Install tool"}:
        rule.update(kind="source", glob="**/*.cs", pattern=r"\b(?:" + "|".join(map(re.escape, names)) + r")\b",
                    effect="review_api_usage", failure_class=None,
                    detail=f"The code uses {', '.join(names)}, which this {change} change concerns. Not necessarily broken: check it.")
    else:
        rule.update(kind="checklist", effect="review")
    return rule


def main() -> None:
    hand = yaml.safe_load((HERE / "rules" / "net8-to-net10.yaml").read_text(encoding="utf-8"))["rules"]
    covered = {r.get("reference", "").rstrip("/") for r in hand}
    covered_pages = {c.rsplit("/", 1)[-1] for c in covered if c}  # same page under another doc path
    rules = (core_rules(SRC / "bc-9.0.md", "9.0") + core_rules(SRC / "bc-10.md", "10.0")
             + aspnet_rules(SRC / "asp-9.md", "9.0") + aspnet_rules(SRC / "asp-10.md", "10.0")
             + ef_rules(SRC / "ef-9.md", "9.0") + ef_rules(SRC / "ef-10.md", "10.0") + obsoletion_rules(SRC / "syslib.md"))
    seen, unique = set(), []
    for r in rules:
        key = r["reference"].rstrip("/")
        if key in seen or key in covered or key.rsplit("/", 1)[-1] in covered_pages or r["reference"].endswith("/overview"):
            continue
        seen.add(key)
        unique.append(r)
    ids = {}
    for r in unique:  # keep ids unique
        n = ids.get(r["id"], 0)
        ids[r["id"]] = n + 1
        if n:
            r["id"] = f"{r['id']}-{n + 1}"
    header = ("# GENERATED by recipes/build_catalog.py from Microsoft's breaking-change lists (recipes/sources/).\n"
              "# Do not edit by hand: change the generator or add hand-written rules to net8-to-net10.yaml.\n")
    OUT.write_text(header + yaml.safe_dump({"rules": unique}, sort_keys=False, allow_unicode=True, width=120),
                   encoding="utf-8")
    kinds = {}
    for r in unique:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"{len(unique)} catalogue rules → {OUT.name}: {kinds}")


if __name__ == "__main__":
    main()
