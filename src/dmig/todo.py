"""The human to-do list: every change a person must make that the skill will not make by itself,
with exact places, ordered by migration group, written once by the analysis.

Why: on a real repository the migration stopped group after group on things the analysis had
already found (an obsolete certificate API, changed test expectations). An engineer fixes those in
one go before the runs; this list is that work, ready to tick off.

Output: human-todo.json (tools), human-todo.md (people); the Obsidian export adds a checklist note.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Rules the skill repairs by itself (group runs or global phases). Everything else that is confirmed
# or found in code is human work. Keep in step with implement.py / globalphase.py.
AUTOMATIC_RULES = {
    "PKG-LINQ-ASYNC", "PKG-INBOX-PRUNED", "PKG-PRUNED-ANY", "PKG-DOWNGRADE-PIN", "SRC-CS14-FIELD",
    "SDK-MSBUILD-SDK-TOO-OLD", "FILE-DOCKER-NET8", "FILE-CI-SDK8", "GLOBAL-JSON-SDK", "PKG-COORDINATED-FRAMEWORK",
}
CAP = 200  # places / tests listed per item


def _test_file(repo: Path, project: str, test: str) -> str | None:
    """The source file that declares the test's class (searched in the test project's folder)."""
    cls = test.split("(")[0].rsplit(".", 2)[-2] if test.count(".") >= 2 else None
    folder = repo / project.rsplit("/", 1)[0] if "/" in project else repo
    if not cls or not folder.is_dir():
        return None
    pattern = re.compile(r"\bclass\s+" + re.escape(cls) + r"\b")
    for f in folder.rglob("*.cs"):
        if any(part in {"bin", "obj"} for part in f.parts):
            continue
        try:
            if pattern.search(f.read_text(encoding="utf-8", errors="replace")):
                return f.relative_to(repo).as_posix()
        except OSError:
            continue
    return None


def build_todo(repo: Path, plan: dict, items: list[dict]) -> dict:
    """items: the analysis findings model (obsidian.build_items): one entry per rule or cause, with
    tier, errors, warnings, tests, code_hits, groups, repair. Returns the to-do document."""
    order = {g["group_id"]: g["order"] for g in plan["groups"]}
    todos = []
    for it in items:
        rule = it.get("rule") or it["key"]
        if rule in AUTOMATIC_RULES or it["tier"] == "possible":
            continue
        if it["tier"] != "confirmed" and not it.get("decision"):
            continue  # found in code but never failed: stays in "what will break", not a to-do
        places = []
        for d in it["errors"] + it["warnings"]:
            if d.get("file") and ":" not in d["file"] and not d["file"].endswith(("proj", ".props")):
                places.append(f"{d['file']}:{d.get('line') or ''}".rstrip(":"))
        for f in it["code_hits"]:
            ev = f.get("evidence") or ""
            if re.match(r"[\w./-]+\.\w+:\d", ev):
                places.append(ev)
        tests = []
        by_class: dict[str, dict] = {}
        for r in it["tests"]:
            key = r["test"].split("(")[0].rsplit(".", 1)[0]
            entry = by_class.setdefault(key, {"class": key, "project": r["project"], "tests": [], "file": None})
            entry["tests"].append(r["test"].split("(")[0].rsplit(".", 1)[-1])
        for entry in by_class.values():
            entry["file"] = _test_file(repo, entry["project"], entry["class"] + ".x")
            entry["tests"] = sorted(set(entry["tests"]))
            tests.append(entry)
        groups = sorted(it["groups"], key=lambda g: order.get(g, 999))
        kind = "test" if it["key"] == "TEST-EXPECTATIONS-CHANGED" else ("decision" if it.get("decision") else "code")
        todos.append({
            "id": rule, "kind": kind, "title": it["title"], "why": it.get("detail") or "",
            "how": (it.get("repair") or {}).get("action", "").replace("Human review: ", "").replace("Check: ", ""),
            "reference": it.get("reference"),
            "first_group": groups[0] if groups else None, "first_group_order": order.get(groups[0]) if groups else None,
            "groups": groups, "places": sorted(set(places))[:CAP],
            "tests": sorted(tests, key=lambda t: t["class"])[:CAP],
            "evidence": {"build_errors": len(it["errors"]), "warnings": len(it["warnings"]),
                         "failing_tests": len(it["tests"])},
            "multi_target_note": "If the fix uses an API that exists only on newer .NET while the project still "
                                 "builds for net8.0, guard it with #if NET10_0_OR_GREATER (or NET9_0_OR_GREATER) "
                                 "and keep the net8.0 branch."
            if kind == "code" else None,
        })
    todos.sort(key=lambda t: (t["first_group_order"] or 999, {"code": 0, "test": 1, "decision": 2}[t["kind"]], t["id"]))
    for i, t in enumerate(todos, 1):
        t["number"] = i
    return {"schema_version": "1.0", "plan_id": plan["plan_id"], "items": todos,
            "summary": {"code": sum(t["kind"] == "code" for t in todos), "test": sum(t["kind"] == "test" for t in todos),
                        "decision": sum(t["kind"] == "decision" for t in todos)}}


def render_md(doc: dict, repo_name: str) -> str:
    s = doc["summary"]
    L = [f"# {repo_name}: human to-do before the group runs", "",
         f"**{len(doc['items'])} item(s)**: {s['code']} code change(s), {s['test']} test-expectation set(s), "
         f"{s['decision']} decision(s). The skill does not make these changes. Fix each before the first group "
         "it names runs, then commit; group runs check their own results again.", ""]
    for t in doc["items"]:
        L += [f"## {t['number']}. [ ] {t['title']}", "",
              f"- **Kind:** {t['kind']} · **before group:** {t['first_group'] or 'any'} · "
              f"**evidence:** {t['evidence']['build_errors']} build error(s), {t['evidence']['failing_tests']} failing test(s)",
              f"- **Why:** {t['why']}" if t["why"] else "",
              f"- **How:** {t['how']}" if t["how"] else ""]
        if t.get("multi_target_note"):
            L.append(f"- **Note:** {t['multi_target_note']}")
        if t["places"]:
            L.append("- **Where:** " + ", ".join(f"`{p}`" for p in t["places"]))
        if t["kind"] == "test":  # the tests themselves change: list every one
            for c in t["tests"]:
                L.append(f"- **Tests** in `{c['file'] or c['class']}`: " + ", ".join(f"`{x}`" for x in c["tests"]))
        elif t["tests"]:         # the tests are only the evidence of a code problem: summarise
            n = sum(len(c["tests"]) for c in t["tests"])
            L.append(f"- **Shows up as:** {n} failing test(s) in {len(t['tests'])} test class(es), e.g. "
                     + ", ".join(f"`{c['class'].rsplit('.', 1)[-1]}`" for c in t["tests"][:3]))
        if t.get("reference"):
            L.append(f"- **Source:** {t['reference']}")
        L.append("")
    return "\n".join(line for line in L if line is not None) + "\n"


def write_todo(out: Path, repo: Path, plan: dict, items: list[dict]) -> dict:
    doc = build_todo(repo, plan, items)
    (out / "human-todo.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "human-todo.md").write_text(render_md(doc, repo.name), encoding="utf-8")
    return doc
