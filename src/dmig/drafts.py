"""Rule drafts: turn a failure no rule explains into a draft rule a person completes.

Learning stays human-reviewed on purpose: a wrong automatic rule could "repair" code incorrectly in
every future run. The draft carries everything known — diagnostic code, where it was seen, the
compiler/Microsoft page if the code is known — so completing it takes minutes. To activate a draft,
fill in the TODOs and move it into recipes/rules/ (a reviewed change to the skill).
"""
from __future__ import annotations

from pathlib import Path

import yaml

HEADER = """# RULE DRAFTS — written by dmig for failures no recipe rule explains. NOT ACTIVE.
# To activate one: replace every TODO (title, effect, the exact repair action), check it against the
# reference page, then move the rule into recipes/rules/ in the skill and add a test. Never activate a
# draft whose fix you have not verified on the code it was seen in.
"""


def reference_for(code: str, recipe: dict | None) -> str | None:
    for r in (recipe or {}).get("rules", []):
        if code in r.get("expected_diagnostics", []):
            return r.get("reference")
    c = code.upper()
    if c.startswith("CS"):
        return f"https://learn.microsoft.com/dotnet/csharp/language-reference/compiler-messages/{c.lower()}"
    if c.startswith("NU"):
        return f"https://learn.microsoft.com/nuget/reference/errors-and-warnings/{c.lower()}"
    if c.startswith("NETSDK"):
        return f"https://learn.microsoft.com/dotnet/core/tools/sdk-errors/{c.lower()}"
    if c.startswith(("SYSLIB", "EXTOBS")):
        return f"https://learn.microsoft.com/dotnet/fundamentals/syslib-diagnostics/{c.lower()}"
    return None


def build_drafts(failures: list[dict], recipe: dict | None) -> list[dict]:
    drafts: dict[str, dict] = {}
    for f in failures:
        if f.get("code"):
            key = f"DRAFT-{f['code']}"
            d = drafts.setdefault(key, {
                "id": key, "status": "draft", "kind": "TODO: package | source | diagnostic",
                "title": f"TODO: what changed in .NET 9/10 that causes {f['code']}",
                "expected_diagnostics": [f["code"]],
                "effect": "TODO: build_break | silent_behavior_change | runtime_failure | obsolete_warning",
                "failure_class": f.get("failure_class") or "TODO",
                "reference": reference_for(f["code"], recipe) or "TODO: link to the Microsoft page",
                "repair": {"id": "TODO", "change_class": "PROJECT_LOCAL",
                           "action": "TODO: the narrowest fix, or 'Human review: …' if it needs a decision"},
                "seen_in": []})
            d["seen_in"].append({k: f.get(k) for k in ("file", "line", "project", "reason") if f.get(k)})
        elif f.get("test"):
            key = f"DRAFT-TEST-{len(drafts) + 1}"
            drafts[key] = {
                "id": key, "status": "draft", "kind": "source",
                "title": f"TODO: behaviour change that makes {f['test']} fail on net10.0",
                "effect": "silent_behavior_change", "failure_class": "test_regression",
                "glob": "**/*.cs", "pattern": "TODO: a regex that finds the affected code",
                "reference": "TODO: link to the Microsoft breaking-change page",
                "repair": {"id": "TODO", "change_class": "PROJECT_LOCAL", "action": "TODO"},
                "seen_in": [{k: f.get(k) for k in ("test", "tfm", "reason") if f.get(k)}]}
    for d in drafts.values():
        d["seen_in"] = d["seen_in"][:10]
    return list(drafts.values())


def write_drafts(out: Path, failures: list[dict], recipe: dict | None) -> str | None:
    drafts = build_drafts(failures, recipe)
    if not drafts:
        return None
    path = out / "rule-drafts.yaml"
    path.write_text(HEADER + yaml.safe_dump({"rules": drafts}, sort_keys=False, allow_unicode=True, width=110),
                    encoding="utf-8")
    return str(path)
