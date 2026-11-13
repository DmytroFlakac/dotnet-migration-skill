"""Obsidian export: the analysis as a linked note set, built with the reusable obsreport package.

Overview first, zoom and filter, details on demand:

  <R> - 00 Start here        one screen: verdict, key numbers, top fixes, where to go
  <R> - 01 What will break   everything that must change, in evidence tiers
                              (confirmed by build/test → found in code → possible)
  <R> - 02 Route             the phases as five vertical stages with checklists
  <R> - 03 Map               layers of groups (C4 level 1) + Map.canvas for the full board
  <R> - 04 Decisions         index of decision records (ADR), one note each in Decisions/
  <R> - 05 Reviewer FAQ      the questions a reviewer asks, answered from the data
  Groups/    one note per group, same template, local diagram (needs / needed by)
  Rules/     one note per rule hit: what, where, fix, source
  Libraries/ one note per shared library (multi-targeted during the transition)
  Evidence/  environment, baseline, build walls per layer, regressions by cause
  Background/ the catalogue checklist, folded by area
Codes stay in properties; headings and labels are plain words.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from obsreport import (TIERS, Vault, callout, flow, front, layers, link, more_line, neighbourhood, safe, table,
                       write_canvas)

from . import limits
from .report import PLAIN, RAG

CALLOUT = {"READY": "success", "WAITS_FOR_DEPENDENCY_REPAIR": "info", "NEEDS_KNOWN_REPAIR": "warning",
           "NEEDS_HUMAN_REVIEW": "warning", "VALIDATION_REQUIRES_WINDOWS": "warning",
           "BLOCKED_NEEDS_INVESTIGATION": "danger", "NOT_VERIFIED_NO_EXPERIMENT": "question",
           "NOT_VERIFIED_NO_BASELINE": "question"}
CANVAS_COLOR = {"READY": "green", "WAITS_FOR_DEPENDENCY_REPAIR": "cyan", "NEEDS_KNOWN_REPAIR": "yellow",
                "NEEDS_HUMAN_REVIEW": "orange", "VALIDATION_REQUIRES_WINDOWS": "orange",
                "BLOCKED_NEEDS_INVESTIGATION": "red"}
PHASE_LABEL = {
    "PHASE_0_ANALYSIS": "Analyse the repository",
    "PHASE_1_SDK_GLOBAL": "Move the whole repository to SDK 10 (projects stay on .NET 8)",
    "PHASE_2_SDK_BASELINE": "Prove SDK 10 builds and tests the unchanged code",
    "PHASE_3_MULTITARGET_SHARED": "Shared libraries build for both .NET 8 and .NET 10",
    "PHASE_4_GROUP_MIGRATION": "Move the groups to .NET 10, one by one",
    "PHASE_5_RETIRE_NET8": "Remove .NET 8 from the shared libraries",
    "PHASE_6_CONVERGE": "Tidy package versions",
    "PHASE_7_REPO_VALIDATION": "Build and test the whole repository",
    "PHASE_8_CUTOVER": "Cut over: central settings, CI, container images",
}
STAGES = [("Analysis", ["PHASE_0_ANALYSIS"]),
          ("SDK 10 for the whole repository", ["PHASE_1_SDK_GLOBAL", "PHASE_2_SDK_BASELINE"]),
          ("Shared libraries build for 8 and 10", ["PHASE_3_MULTITARGET_SHARED"]),
          ("Groups move one by one", ["PHASE_4_GROUP_MIGRATION"]),
          ("Finish: remove .NET 8, tidy, validate, cut over",
           ["PHASE_5_RETIRE_NET8", "PHASE_6_CONVERGE", "PHASE_7_REPO_VALIDATION", "PHASE_8_CUTOVER"])]
DONE = {"DONE", "ALREADY_SATISFIED", "NOT_NEEDED"}
START_MAX_LINES = 70
TEST_EXPECTATIONS = "TEST-EXPECTATIONS-CHANGED"


def _short(pid: str) -> str:
    return pid.rsplit("/", 1)[-1].rsplit(".", 1)[0]


class Names:
    def __init__(self, repo_name: str):
        self.r = safe(repo_name)

    def start(self): return f"{self.r} - 00 Start here"
    def breaks(self): return f"{self.r} - 01 What will break"
    def route(self): return f"{self.r} - 02 Route"
    def map(self): return f"{self.r} - 03 Map"
    def decisions(self): return f"{self.r} - 04 Decisions"
    def faq(self): return f"{self.r} - 05 Reviewer FAQ"
    def todo(self): return f"{self.r} - 06 Human to-do"
    def evidence(self): return f"{self.r} - Evidence"
    def background(self): return f"{self.r} - Background checklist"
    def group(self, g): return f"{self.r} - G{g['order']:02d} {safe(g['group_id'])}"
    def library(self, s): return f"{self.r} - Lib {safe(s['name'])}"
    def rule(self, rule_id): return f"{self.r} - Rule {safe(rule_id)}"
    def decision(self, i, title): return f"{self.r} - D{i:02d} {safe(title)[:60]}"


# --------------------------------------------------------------------------- the findings model
def _layer_of(experiment: dict | None) -> dict[str, int]:
    per = (((experiment or {}).get("build_test") or {}).get("layers") or {}).get("per_layer", [])
    out: dict[str, int] = {}
    for layer in per:
        for code in layer.get("stopped_on", []):
            out.setdefault(code, layer["layer"])
    return out


def _is_decision(repair: dict | None) -> bool:
    # "Check:" catalogue items are manual code fixes; only policy/behaviour choices are decisions.
    return (repair or {}).get("action", "").startswith("Human review")


def build_items(plan: dict, compat: dict, experiment: dict | None, rules: dict,
                baseline: dict | None = None) -> list[dict]:
    """Everything that must change, one item per rule (or per unexplained code), with its evidence."""
    layer_of = _layer_of(experiment)
    relaxed = set(layer_of)
    by_rule_finding: dict[str, list[dict]] = {}
    for f in compat["findings"]:
        if f.get("effect") in {"none", "none_known", "review_api_usage"} or f["rule_id"] == "PKG-CONFIRMED-BY-EXPERIMENT":
            continue
        by_rule_finding.setdefault(f["rule_id"], []).append(f)
    items: dict[str, dict] = {}

    def item(key: str, code: str | None = None) -> dict:
        if key not in items:
            rule = rules.get(key) or {}
            f0 = (by_rule_finding.get(key) or [{}])[0]
            items[key] = {"key": key, "rule": key if (rule or f0) else None,
                          "title": rule.get("title") or f0.get("title") or f"Build error {code or key}",
                          "detail": rule.get("detail") or f0.get("detail") or "",
                          "repair": rule.get("repair") or f0.get("repair") or {},
                          "reference": rule.get("reference") or f0.get("reference"),
                          "tier": "found", "errors": [], "warnings": [], "tests": [], "code_hits": [],
                          "groups": set(), "codes": set(), "layer": None}
        return items[key]

    for g in plan["groups"]:
        ex = g["experiment"]
        for e in ex["new_errors"]:
            it = item(e["matched_rule"] or f"CODE-{e['code']}", e["code"])
            it["tier"] = "confirmed"
            it["errors"].append(e)
            it["groups"].add(g["group_id"])
            it["codes"].add(e["code"])
            if e["code"] in layer_of:
                it["layer"] = min(it["layer"] or 99, layer_of[e["code"]])
        for w in ex["new_rule_warnings"]:
            it = item(w["matched_rule"], w["code"])
            it["warnings"].append(w)
            it["groups"].add(g["group_id"])
            it["codes"].add(w["code"])
            if w["code"] in relaxed:  # would have been an error: the experiment relaxed it to see further
                it["tier"] = "confirmed"
                it["layer"] = min(it["layer"] or 99, layer_of[w["code"]])
        for r in ex["test_regressions"]:
            for cause in (r.get("probable_cause") or [TEST_EXPECTATIONS]):
                if cause == TEST_EXPECTATIONS and TEST_EXPECTATIONS not in items:
                    items[TEST_EXPECTATIONS] = {
                        "key": TEST_EXPECTATIONS, "rule": None, "title": "Test expectations changed on .NET 10",
                        "detail": "Tests that passed on .NET 8 fail on .NET 10 with assertion differences (counts, "
                                  "messages, mock calls) that no rule explains. A person decides per test whether the "
                                  "new behaviour is acceptable and the expectation should change, or the code must.",
                        "repair": {"action": "Human review: compare old and new behaviour for each test; update the "
                                             "expectation only when the new .NET 10 behaviour is correct.",
                                   "change_class": "PROJECT_LOCAL"},
                        "reference": None, "tier": "confirmed", "errors": [], "warnings": [], "tests": [],
                        "code_hits": [], "groups": set(), "codes": set(), "layer": None}
                it = items[cause] if cause in items else item(cause)
                it["tier"] = "confirmed"
                it["tests"].append(r)
                it["groups"].add(g["group_id"])
    # A baseline measured with SDK 10 (the SDK stage preview) fails *because of* SDK 10: those errors
    # are migration work for the SDK stage, not pre-existing failures.
    if baseline and "SDK-10" in (baseline.get("built_in") or ""):
        owner = {p: g["group_id"] for g in plan["groups"] for p in g["project_ids"] + g["test_project_ids"]}
        for e in baseline.get("errors", []):
            if e["code"] in {"NU1901", "NU1902", "NU1903", "NU1904"}:
                continue  # advisory drift: reported as a decision
            rule = next((r["id"] for r in rules.values() if e["code"] in r.get("expected_diagnostics", [])), None)
            it = item(rule or f"CODE-{e['code']}", e["code"])
            it["tier"] = "confirmed"
            it["sdk_stage"] = True
            it["errors"].append(e)
            it["codes"].add(e["code"])
            if e.get("project") in owner:
                it["groups"].add(owner[e["project"]])
    for rule_id, fs in by_rule_finding.items():
        it = item(rule_id)
        it["code_hits"] = fs
        owners = {g["group_id"] for g in plan["groups"] for f in fs
                  if set(f.get("projects") or []) & set(g["project_ids"] + g["test_project_ids"])}
        it["groups"] |= owners
    out = list(items.values())
    for it in out:
        it["decision"] = it["key"] == TEST_EXPECTATIONS or _is_decision(it["repair"])
        it["impact"] = len(it["tests"]) + len(it["errors"]) + len(it["warnings"])
    out.sort(key=lambda i: (i["tier"] != "confirmed", -len(i["tests"]), i["layer"] or 99, -i["impact"], i["key"]))
    return out


def _scoped(it: dict, members: set[str]) -> dict:
    """The item restricted to one group's projects (its own errors, tests and code hits)."""
    s = dict(it)
    s["errors"] = [e for e in it["errors"] if e.get("project") in members]
    s["warnings"] = [w for w in it["warnings"] if w.get("project") in members]
    s["tests"] = [r for r in it["tests"] if r.get("project") in members]
    s["code_hits"] = [f for f in it["code_hits"] if set(f.get("projects") or []) & members]
    return s


def _where(it: dict, limit: int = 3) -> str:
    places = []
    for e in it["errors"] + it["warnings"]:
        f = e.get("file") or ""
        p = f"`{_short(f)}`" if f.endswith(("proj", ".props", ".targets")) or not f else f"`{f}`"
        if not f or ":" in f:  # no file, or a file outside the repository (an SDK or NuGet package)
            p = f"`{_short(e.get('project') or f)}`"
        if p not in places:
            places.append(p)
    if not places:
        for f in it["code_hits"]:
            for p in (f.get("projects") or []):
                if f"`{_short(p)}`" not in places:
                    places.append(f"`{_short(p)}`")
    if not places and it["tests"]:
        places = sorted({f"`{_short(t['project'])}`" for t in it["tests"]})
    extra = len(places) - limit
    return ", ".join(places[:limit]) + (f" +{extra} more" if extra > 0 else "")


def _evidence_line(it: dict) -> str:
    bits = []
    if it.get("sdk_stage"):
        bits.append("SDK 10 stage")
    if it["layer"]:
        bits.append(f"build wall {it['layer']}")
    if it["errors"]:
        bits.append(f"{len(it['errors'])} build error(s)")
    if it["warnings"]:
        bits.append(f"{len(it['warnings'])} warning(s)")
    if it["tests"]:
        bits.append(f"**{len(it['tests'])} failing test(s)**")
    if it["code_hits"] and not bits:
        bits.append(f"{len(it['code_hits'])} place(s) in code")
    return ", ".join(bits)


# --------------------------------------------------------------------------- export
def export(out: Path, repo: Path, response: dict, preflight: dict, inventory: dict, baseline: dict | None,
           experiment: dict | None, compat: dict, plan: dict, target_dir: Path | None = None,
           recipe: dict | None = None) -> str:
    """Writes the note set to <out>/obsidian/<repo> .NET 10 migration/, or into target_dir."""
    n = Names(repo.name)
    vault = Vault((target_dir or out / "obsidian") / f"{n.r} .NET 10 migration")
    rules = {r["id"]: r for r in (recipe or {}).get("rules", [])}
    groups = plan["groups"]
    by_id = {g["group_id"]: g for g in groups}
    group_of = {pid: g for g in groups for pid in g["project_ids"] + g["test_project_ids"]}
    items = build_items(plan, compat, experiment, rules, baseline)
    confirmed = [i for i in items if i["tier"] == "confirmed"]
    found = [i for i in items if i["tier"] == "found"]
    checks = [f for f in compat["findings"] if f.get("effect") == "review_api_usage"]
    checklist = compat.get("review_checklist", [])
    possible = len(checks) + len(checklist)
    drift = [l for l in preflight.get("limitations", []) if l["code"] == "BASELINE_NUGET_AUDIT_DRIFT"]
    decisions = [i for i in items if i["decision"]]
    if drift:
        decisions.append({"key": "NUGET-AUDIT-POLICY", "title": "Policy for known package vulnerabilities",
                          "detail": drift[0]["detail"], "tier": "confirmed", "tests": [], "errors": [],
                          "warnings": [], "code_hits": [], "groups": set(),
                          "repair": rules.get("SDK-NUGET-AUDIT-TRANSITIVE", {}).get("repair", {}),
                          "reference": rules.get("SDK-NUGET-AUDIT-TRANSITIVE", {}).get("reference"),
                          "rule": "SDK-NUGET-AUDIT-TRANSITIVE" if "SDK-NUGET-AUDIT-TRANSITIVE" in rules else None,
                          "decision": True, "layer": None, "impact": 0})
    base_props = {"repository": repo.name, "commit": (inventory["repository_commit"] or "")[:12],
                  "plan_id": plan["plan_id"], "analysed": date.today().isoformat()}
    bt = (experiment or {}).get("build_test") or {}
    tests_before = (baseline or {}).get("tests", {})
    regressions = sum(len(i["tests"]) for i in items)
    counts = response["summary"]["readiness_counts"]
    worst = next((r for r in ("BLOCKED_NEEDS_INVESTIGATION", "NEEDS_HUMAN_REVIEW", "NEEDS_KNOWN_REPAIR",
                              "VALIDATION_REQUIRES_WINDOWS", "WAITS_FOR_DEPENDENCY_REPAIR") if r in counts), "READY")
    rule_note = lambda it: n.rule(it["key"])  # noqa: E731
    dec_index = {d["key"]: i + 1 for i, d in enumerate(decisions)}

    # ------------------------------------------------------------------ 00 Start here
    if not experiment or experiment.get("status") != "EXECUTED":
        verdict = ("Not measured yet", "question",
                   "The .NET 10 experiment did not run, so nothing below is proven by a build. Findings come from "
                   "reading the code only.")
    else:
        top = max(confirmed, key=lambda i: len(i["tests"]), default=None)
        walls = len({i["layer"] for i in confirmed if i["layer"]})
        verdict = (f"{len(confirmed)} confirmed problem(s), {len(decisions)} decision(s)",
                   ("danger" if decisions or "BLOCKED_NEEDS_INVESTIGATION" in counts
                    else "warning" if confirmed else "success"),
                   (f"The .NET 10 experiment built {len(bt.get('projects_built', []))} of {len(inventory['projects'])} "
                    f"projects" + (f" after peeling {walls} build wall(s)" if walls else "") + ". "
                    + (f"{regressions} test(s) pass on .NET 8 and fail on .NET 10"
                       + (f"; **{len(top['tests'])} of them have one cause**." if top and len(top["tests"]) > regressions / 2
                          and regressions else ".") if regressions else "No test regressions.")))
    L = [front({**base_props, "type": "migration-start", "status": response["status"],
                "projects": len(inventory["projects"]), "groups": len(groups), "confirmed": len(confirmed),
                "found_in_code": len(found), "possible": possible, "decisions": len(decisions),
                "tags": ["dotnet-migration", "migration/start"]}),
         f"# {repo.name}: .NET 8 → .NET 10", ""]
    L += callout(verdict[1], f"Verdict: {verdict[0]}", verdict[2]) + [""]
    L += table(["Projects", "Groups", "Tests before", "Regressions", "Decisions"],
               [[len(inventory["projects"]), len(groups), tests_before.get("total", "—"), regressions or 0,
                 len(decisions)]]) + [""]
    L += ["## Top fixes, by impact", ""]
    for i, it in enumerate(confirmed[:5], 1):
        L.append(f"{i}. **{it['title']}**: {_where(it, 2)} · {_evidence_line(it)} → {link(rule_note(it), 'details')}")
    if not confirmed:
        L.append("Nothing failed in a real build yet. See " + link(n.breaks(), "what will break") + ".")
    if len(confirmed) > 5:
        L.append(f"\n…{len(confirmed) - 5} more confirmed in {link(n.breaks(), 'What will break')}.")
    L += ["", "## Where to go next", "",
          f"- {link(n.breaks(), 'What will break')}: {len(confirmed)} confirmed · {len(found)} found in code · "
          f"{possible} possible (folded)",
          f"- {link(n.route(), 'Route')}: five stages from SDK 10 to cut-over",
          f"- {link(n.map(), 'Map')}: how the {len(groups)} groups depend on each other",
          f"- {link(n.decisions(), 'Decisions')}: {len(decisions)} that need a person",
          f"- {link(n.todo(), 'Human to-do')}: the code and test changes to make once, before the group runs",
          f"- {link(n.faq(), 'Reviewer FAQ')} · {link(n.evidence(), 'Evidence')}", ""]
    L += [f"*Groups: " + ", ".join(f"{RAG.get(k, '')} {v} {PLAIN.get(k, k)}" for k, v in counts.items()) + ".*"]
    vault.write(n.start(), L)

    # ------------------------------------------------------------------ 01 What will break
    L = [front({**base_props, "type": "migration-breaks", "confirmed": len(confirmed), "found_in_code": len(found),
                "possible": possible, "tags": ["dotnet-migration", "migration/breaks"]}),
         f"# {repo.name}: what will break", ""]
    L += callout("abstract", "Read from the top", [
        "Ordered by strength of evidence, then by impact. "
        + " · ".join(f"{t['icon']} **{t['label']}**: {t['meaning']}" for t in TIERS.values()) + "."]) + [""]
    L += [f"Back to {link(n.start(), 'Start here')}.", ""]
    for tier, its in (("confirmed", confirmed), ("found", found)):
        t = TIERS[tier]
        L += [f"## {t['icon']} {t['label']} ({len(its)})", ""]
        if not its:
            L += ["Nothing in this tier.", ""]
            continue
        L += table(["Problem", "Where", "Evidence", "Fix"],
                   [[link(rule_note(it), it["title"], in_table=True), _where(it), _evidence_line(it),
                     "decision" if it["decision"] else (it["repair"].get("change_class") or "—").lower().replace("_", " ")]
                    for it in its], "compatibility.json / experiment.json") + [""]
    L += [f"## {TIERS['possible']['icon']} {TIERS['possible']['label']} ({possible})", "",
          f"Microsoft breaking changes that apply to technologies used here but did not show up in the build: "
          f"{link(n.background(), 'Background checklist')}."]
    vault.write(n.breaks(), L)

    # ------------------------------------------------------------------ rule notes
    for it in items:
        repair = it["repair"] or {}
        L = [front({**base_props, "type": "migration-rule", "rule": it["key"], "tier": it["tier"],
                    "fix_scope": repair.get("change_class"), "decision": it["decision"],
                    "tests": len(it["tests"]), "errors": len(it["errors"]),
                    "tags": ["dotnet-migration", f"migration/{it['tier']}"]}),
             f"# {it['title']}", ""]
        t = TIERS[it["tier"]]
        L += callout("danger" if it["tier"] == "confirmed" else "warning",
                     f"{t['icon']} {t['label']}: {_evidence_line(it) or t['meaning']}",
                     it["detail"] or it["title"]) + [""]
        L += [f"Back to {link(n.breaks(), 'What will break')}.", "", "## Fix", "",
              f"- **What:** {repair.get('action', 'no automatic repair')}",
              f"- **Scope:** {(repair.get('change_class') or '—').lower().replace('_', ' ')}"]
        if it["decision"] and it["key"] in dec_index:
            L.append(f"- **Decision record:** {link(n.decision(dec_index[it['key']], it['title']), 'open')}")
        if it.get("reference"):
            L.append(f"- **Source:** [Microsoft documentation]({it['reference']})")
        if it["groups"]:
            L += ["", "## Groups affected", "", ", ".join(link(n.group(by_id[gid]), gid) for gid in sorted(it["groups"])
                                                           if gid in by_id)]
        if it["errors"] or it["warnings"]:
            L += ["", "## Build evidence", ""]
            L += table(["Code", "File", "Line", "Message"],
                       [[d["code"], f"`{d.get('file') or _short(d.get('project') or '')}`", d.get("line") or "",
                         (d.get("message") or "")[:140]] for d in it["errors"] + it["warnings"]], "experiment.json", 50)
        if it["tests"]:
            by_project: dict[str, int] = {}
            for r in it["tests"]:
                by_project[_short(r["project"])] = by_project.get(_short(r["project"]), 0) + 1
            L += ["", f"## Failing tests ({len(it['tests'])})", ""]
            L += table(["Test project", "Failing tests"], sorted(([k, v] for k, v in by_project.items()),
                                                                 key=lambda r: -r[1]))
            L += ["", "Examples:", ""] + [f"- `{r['test'].rsplit('.', 2)[-2]}.{r['test'].rsplit('.', 1)[-1]}`: "
                                          f"{(r.get('message') or '')[:120]}" for r in it["tests"][:5]]
        if it["code_hits"] and not (it["errors"] or it["warnings"]):
            L += ["", "## Found in code", ""]
            L += table(["Project", "Evidence"], [[_short(p), f.get("evidence", "")] for f in it["code_hits"]
                                                 for p in (f.get("projects") or ["repository"])], "compatibility.json", 50)
        vault.write(f"Rules/{rule_note(it)}", L)

    # ------------------------------------------------------------------ 02 Route
    status = {p["id"]: p["status"] for p in plan["phases"]}
    def stage_mark(ids):
        st = [status.get(i) for i in ids if i in status]
        return "✅" if st and all(s in DONE for s in st) else ("❌" if "FAILED" in st else "⬜")
    L = [front({**base_props, "type": "migration-route", "tags": ["dotnet-migration", "migration/route"]}),
         f"# {repo.name}: route", ""]
    L += callout("abstract", "Five stages, each a separate patch for review",
                 "SDK first, then shared libraries for both versions, then the groups in dependency order, then "
                 "the finish. Nothing is changed without an approved patch.") + [""]
    L += [f"Back to {link(n.start(), 'Start here')}.", ""]
    L += [flow([(f"s{i}", f"{stage_mark(ids)} {i + 1}. {title}") for i, (title, ids) in enumerate(STAGES)],
               [(f"s{i}", f"s{i + 1}") for i in range(len(STAGES) - 1)]), ""]
    phases = {p["id"]: p for p in plan["phases"]}
    for i, (title, ids) in enumerate(STAGES):
        L += [f"## {i + 1}. {title}", ""]
        for pid in ids:
            p = phases.get(pid)
            if not p:
                continue
            done = p["status"] in DONE
            L += [f"**{PHASE_LABEL.get(pid, pid)}** · *{p['status'].lower().replace('_', ' ')}*", ""]
            shown, omitted = p["items"][:limits.NOTE_ROWS], max(0, len(p["items"]) - limits.NOTE_ROWS)
            for it in shown:
                text = it["change"]
                g = by_id.get(it.get("group_id") or "")
                if g:
                    text = text.replace(g["group_id"], link(n.group(g), g["group_id"]))
                tick = "x" if done or it.get("needed") is False else " "
                L.append(f"- [{tick}] {text}" + (f" — {RAG.get(it['readiness'], '')} {PLAIN.get(it['readiness'])}"
                                                if it.get("readiness") else "")
                         + (f" ({it['condition']})" if it.get("condition") else ""))
            if omitted:
                L.append(f"- {more_line(omitted, 'migration-plan.json')}")
            L.append("")
    vault.write(n.route(), L)

    # ------------------------------------------------------------------ 03 Map + canvas
    depth: dict[str, int] = {}
    def d(gid, seen=()):
        if gid not in depth:
            deps = [x for x in by_id[gid]["depends_on_group_ids"] if x in by_id and x not in seen]
            depth[gid] = 0 if not deps else 1 + max(d(x, seen + (gid,)) for x in deps)
        return depth[gid]
    for g in groups:
        d(g["group_id"])
    by_depth: dict[int, list[dict]] = {}
    for g in groups:
        by_depth.setdefault(depth[g["group_id"]], []).append(g)
    rows = [(f"Layer {k + 1}: {len(v)} group(s)",
             ", ".join(x["group_id"] for x in v[:3]) + (f" +{len(v) - 3} more" if len(v) > 3 else ""))
            for k, v in sorted(by_depth.items())]
    canvas_name = f"{n.r} - Map board"
    write_canvas(vault.root / f"{canvas_name}.canvas",
                 [{"id": g["group_id"], "layer": depth[g["group_id"]],
                   "color": CANVAS_COLOR.get(g["readiness"]),
                   "text": f"**{link(n.group(g), str(g['order']) + '. ' + g['group_id'])}**\n\n"
                           f"{RAG.get(g['readiness'], '')} {PLAIN.get(g['readiness'], '')}"} for g in groups],
                 [(dep, g["group_id"]) for g in groups for dep in g["depends_on_group_ids"]])
    L = [front({**base_props, "type": "migration-map", "groups": len(groups), "layers": len(by_depth),
                "tags": ["dotnet-migration", "migration/map"]}),
         f"# {repo.name}: map", ""]
    L += callout("tip", "Zoom levels",
                 [f"This note: the layers (each layer only needs the layers above it). "
                  f"Whole board: open **{canvas_name}.canvas** next to this note (zoom and drag). "
                  "One group: its note shows only what it needs and what needs it."]) + [""]
    L += [f"Back to {link(n.start(), 'Start here')}.", ""]
    if len(rows) <= 12:
        L += [layers(rows, caption="Arrows point from a layer to the layer that depends on it."), ""]
    L += ["## Groups", ""]
    L += table(["#", "Group", "Layer", "Status", "Projects", "Tests", "Needs"], [
        [g["order"], link(n.group(g), g["group_id"], in_table=True), depth[g["group_id"]] + 1,
         f"{RAG.get(g['readiness'], '')} {PLAIN.get(g['readiness'])}", len(g["project_ids"]),
         len(g["test_project_ids"]), ", ".join(g["depends_on_group_ids"]) or "—"] for g in groups], "migration-plan.json")
    if plan["shared_projects"]:
        L += ["", "## Shared libraries", "",
              "Built for both .NET 8 and .NET 10 until their last .NET 8 consumer moves: "
              + ", ".join(link(n.library(s), s["name"]) for s in plan["shared_projects"][:limits.NOTE_ROWS]) + "."]
    vault.write(n.map(), L)

    # ------------------------------------------------------------------ groups
    needed_by: dict[str, list[str]] = {}
    for g in groups:
        for dep in g["depends_on_group_ids"]:
            needed_by.setdefault(dep, []).append(g["group_id"])
    for g in groups:
        members = set(g["project_ids"] + g["test_project_ids"])
        mine = [s for s in (_scoped(it, members) for it in items if g["group_id"] in it["groups"])
                if s["errors"] or s["warnings"] or s["tests"] or s["code_hits"]]
        L = [front({**base_props, "type": "migration-group", "group_id": g["group_id"], "order": g["order"],
                    "readiness": g["readiness"], "layer": depth[g["group_id"]] + 1, "kind": g["group_kind"],
                    "platform": g["platform"], "depends_on": g["depends_on_group_ids"],
                    "projects": len(g["project_ids"]), "tests": len(g["test_project_ids"]),
                    "confidence": g["confidence"],
                    "tags": ["dotnet-migration", f"migration/{g['readiness'].lower().replace('_', '-')}"]}),
             f"# {g['order']}. {g['group_id']}", ""]
        L += callout(CALLOUT.get(g["readiness"], "note"),
                     f"{RAG.get(g['readiness'], '')} {PLAIN.get(g['readiness'], g['readiness'])}",
                     _verdict(g, mine)) + [""]
        L += [f"Back to {link(n.map(), 'the map')} · {link(n.start(), 'Start here')}.", "",
              neighbourhood(f"{g['order']}. {g['group_id']}", g["depends_on_group_ids"], needed_by.get(g["group_id"], []),
                            caption="Above: what it needs. Below: what needs it."), ""]
        if mine:
            L += ["## What must change here", ""]
            L += [f"- {TIERS[it['tier']]['icon']} {link(rule_note(it), it['title'])} · {_evidence_line(it)}" for it in mine]
            L.append("")
        L += ["## Why these projects belong together", ""] + [f"- {e}" for e in g["grouping_evidence"]] + [""]
        L += ["## Changes the migration will make", ""]
        L += table(["Scope", "File", "Change"], [
            [(c["change_class"] + ("" if c.get("allowed_in_group_run", True) else " (global phase)")).lower().replace("_", " "),
             f"`{c['file']}`",
             (f"{';'.join(c['from'])} → {';'.join(c['to'])}" if c["kind"] == "target_framework" else c["action"])]
            for c in g["proposed_changes"]], "migration-plan.json")
        L += ["", "## Projects", ""]
        projects = {p["id"]: p for p in inventory["projects"]}
        L += table(["Project", "Kind", "Target", "Platform"], [
            [_short(p), projects.get(p, {}).get("kind", "?"), ";".join(projects.get(p, {}).get("target_frameworks", [])),
             projects.get(p, {}).get("platform", "?")] for p in g["project_ids"] + g["test_project_ids"]], "inventory.json")
        L += ["", "## How to migrate this group", "",
              f"After {', '.join(g['depends_on_group_ids']) or 'the SDK stage'}:", "",
              "```bash", "PYTHONPATH=<skill>/scripts python -m dmig implement --repo <repo> \\",
              f"  --plan <analysis>/migration-plan.json --group {g['group_id']} --out <new folder> --allow-newer-commit",
              "```"]
        vault.write(f"Groups/{n.group(g)}", L)

    # ------------------------------------------------------------------ shared libraries
    for s in plan["shared_projects"]:
        L = [front({**base_props, "type": "shared-library", "project": s["project_id"],
                    "temporary_tfms": s["temporary_tfms"], "policy": s["shared_library_policy"],
                    "tags": ["dotnet-migration", "migration/shared-library"]}),
             f"# {s['name']} (shared library)", ""]
        L += callout("info", "Built for both versions until its last .NET 8 consumer moves",
                     f"{';'.join(s['current_tfms'])} → **{';'.join(s['temporary_tfms'])}** in stage 3, then .NET 8 is "
                     f"removed in the finish. {s['retirement_condition_for_net8']}.") + [""]
        L += [f"Back to {link(n.map(), 'the map')}.", "",
              neighbourhood(s["name"], [], s["consumer_groups"], caption="Below: the groups that use it."), "",
              "## Consumers", ""]
        L += [f"- {link(n.group(by_id[gid]), gid)}" for gid in s["consumer_groups"] if gid in by_id]
        L += ["", "## Validation matrix", ""]
        L += table(["Target", "Build", "Tests"], [[v["tfm"], "yes" if v["build"] else "no",
                                                   ", ".join(_short(t) for t in v["tests"]) or "—"]
                                                  for v in s["validation_matrix"]], "migration-plan.json")
        if s["package_strategy_per_tfm"]:
            L += ["", "## Packages per target", ""]
            L += table(["Package", "Strategy", "net8.0", "net10.0"], [
                [p["package"], p["strategy"], p["versions"].get("net8.0"), p["versions"].get("net10.0")]
                for p in s["package_strategy_per_tfm"]], "migration-plan.json")
        vault.write(f"Libraries/{n.library(s)}", L)

    # ------------------------------------------------------------------ 04 Decisions (ADR)
    L = [front({**base_props, "type": "migration-decisions", "decisions": len(decisions),
                "tags": ["dotnet-migration", "migration/decisions"]}),
         f"# {repo.name}: decisions", ""]
    L += callout("question", f"{len(decisions)} decision(s) need a person",
                 "Each is a short decision record: context, options, recommendation, and an empty **Decision** "
                 "line to fill in. The migration does not guess these.") + [""]
    L += [f"Back to {link(n.start(), 'Start here')}.", ""]
    L += table(["#", "Decision", "Evidence", "Decided?"],
               [[i + 1, link(n.decision(i + 1, dd["title"]), dd["title"], in_table=True), _evidence_line(dd) or "policy",
                 "☐"] for i, dd in enumerate(decisions)]) if decisions else ["Nothing needs a decision."]
    vault.write(n.decisions(), L)
    for i, dd in enumerate(decisions, 1):
        repair = dd.get("repair") or {}
        L = [front({**base_props, "type": "decision-record", "decision_id": f"D{i:02d}", "rule": dd.get("rule"),
                    "status": "open", "tags": ["dotnet-migration", "migration/decision"]}),
             f"# D{i:02d}. {dd['title']}", "",
             f"Back to {link(n.decisions(), 'Decisions')}.", "",
             "## Context", "", dd.get("detail") or dd["title"], ""]
        if _evidence_line(dd):
            L += [f"Evidence: {_evidence_line(dd)} ({_where(dd)}).", ""]
        L += ["## Options and recommendation", "", repair.get("action", "—").replace("Human review: ", "")
              .replace("Check: ", ""), ""]
        if dd.get("reference"):
            L += [f"Source: [Microsoft documentation]({dd['reference']})", ""]
        L += ["## Decision", "", "*To be decided:* ", "", "## Consequences", "",
              f"Affects: {', '.join(sorted(dd['groups'])) or 'the whole repository'}."]
        if dd.get("rule"):
            L += ["", f"Rule: {link(rule_note(dd), dd['rule'])}" if dd["key"] == dd.get("rule") else ""]
        vault.write(f"Decisions/{n.decision(i, dd['title'])}", L)

    # ------------------------------------------------------------------ 06 Human to-do
    from .todo import build_todo
    todo = build_todo(repo, plan, items)
    s = todo["summary"]
    L = [front({**base_props, "type": "migration-todo", "items": len(todo["items"]), "code": s["code"],
                "tests": s["test"], "decisions": s["decision"], "tags": ["dotnet-migration", "migration/todo"]}),
         f"# {repo.name}: human to-do", ""]
    L += callout("todo", f"{len(todo['items'])} change(s) the skill will not make: do them once, before the groups run",
                 f"{s['code']} code change(s), {s['test']} set(s) of changed test expectations, {s['decision']} decision(s). "
                 "Ordered by the first group that needs each. Tick each box when committed.") + [""]
    L += [f"Back to {link(n.start(), 'Start here')}.", ""]
    for t in todo["items"]:
        g = by_id.get(t["first_group"] or "")
        L += [f"- [ ] **{t['number']}. {t['title']}** · {t['kind']} · before "
              + (link(n.group(g), g["group_id"]) if g else "any group")
              + f" · details: {link(rule_note({'key': t['id']}), t['id'])}"]
        if t["places"]:
            L.append("    - where: " + ", ".join(f"`{p}`" for p in t["places"][:20])
                     + (f" +{len(t['places']) - 20} more" if len(t["places"]) > 20 else ""))
        if t["kind"] == "test":
            for c in t["tests"][:20]:
                L.append(f"    - tests in `{c['file'] or c['class']}`: " + ", ".join(f"`{x}`" for x in c["tests"][:10]))
        elif t["tests"]:
            L.append(f"    - shows up as {sum(len(c['tests']) for c in t['tests'])} failing test(s)")
        if t.get("multi_target_note"):
            L.append("    - note: " + t["multi_target_note"])
    if not todo["items"]:
        L.append("Nothing: every confirmed problem has an automatic repair.")
    vault.write(n.todo(), L)

    # ------------------------------------------------------------------ 05 Reviewer FAQ
    blocked = [b for b in bt.get("environmental_blocks", [])]
    fixes = (experiment or {}).get("measurement_fixes") or []
    relaxed = ((bt.get("layers") or {}).get("relaxed_codes")) or []
    qa = [
        ("Did the analysis change our repository?",
         f"No. Changes persisted: **{response['changes_persisted']}**; rollback verified: "
         f"**{response['rollback_verified']}**. Every experiment ran in a throwaway git worktree."),
        ("How sure is this?",
         f"{len(confirmed)} problem(s) are **confirmed** by a real .NET 10 build or test run; {len(found)} are found in "
         f"code only; {possible} are possible and folded away. Baseline: {tests_before.get('total', '—')} tests, "
         f"{tests_before.get('passed', '—')} passed on .NET 8."),
        ("Which tests failed before any change?",
         f"{tests_before.get('failed', 0)} test(s) fail on the unchanged .NET 8 code. They are recorded in "
         f"{link(n.evidence(), 'Evidence')} and never counted as migration problems."),
        ("What did the analysis do to see further than the first error?",
         ("It relaxed " + ", ".join(f"`{c}`" for c in relaxed) + " to warnings in the throwaway copy, "
          if relaxed else "Nothing special: ")
         + (f"raised {len(fixes)} pinned package(s) there, " if fixes else "")
         + ("and measured with the NuGet audit off (new advisories break even the old code). " if drift else "")
         + "Every error seen before relaxing is still listed."),
        ("What was not tested?",
         f"{sum(1 for b in blocked)} environment block(s) and {bt.get('tests', {}).get('skipped', 0)} skipped test(s) "
         f"in the experiment; groups marked {RAG['NOT_VERIFIED_NO_BASELINE']} could not be verified."),
        ("How many steps?",
         f"Five stages ({link(n.route(), 'Route')}), {len(groups)} group(s) in stage 4, each its own patch."),
        ("What needs a person?",
         f"{len(decisions)} decision(s): {link(n.decisions(), 'Decisions')}."),
    ]
    L = [front({**base_props, "type": "migration-faq", "tags": ["dotnet-migration", "migration/faq"]}),
         f"# {repo.name}: reviewer FAQ", "", f"Back to {link(n.start(), 'Start here')}.", ""]
    for q, a in qa:
        L += [f"## {q}", "", a, ""]
    vault.write(n.faq(), L)

    # ------------------------------------------------------------------ Evidence
    L = [front({**base_props, "type": "migration-evidence", "preflight": preflight["status"],
                "tags": ["dotnet-migration", "migration/evidence"]}),
         f"# {repo.name}: evidence", "", f"Back to {link(n.start(), 'Start here')}.", "", "## Environment", "",
         f"Preflight **{preflight['status'].lower().replace('_', ' ')}**; SDK in the repository "
         f"`{preflight['selected_sdk_in_repository']}`; runtimes "
         + ", ".join(f"{r['name'].replace('Microsoft.', '')} {r['version']}" for r in preflight["runtimes"]) + ".", ""]
    L += [f"- ⛔ {b['detail']}" for b in preflight["blocking_items"]]
    L += [f"- ⚠️ {l['detail']}" for l in preflight["limitations"]]
    if baseline:
        fails = [r for r in baseline["tests"]["results"] if r["outcome"] == "failed"]
        L += ["", "## Baseline: the unchanged code on .NET 8", "",
              f"{len(baseline.get('projects_built', []))} project(s) built; {tests_before.get('passed', 0)} passed, "
              f"{tests_before.get('failed', 0)} failed, {tests_before.get('skipped', 0)} skipped.", ""]
        shown = fails[:limits.REPORT_LIST]
        L += [f"- `{r['test'].split('(')[0]}` ({_short(r['project'])})" for r in shown]
        if len(fails) > len(shown):
            L.append(f"- {more_line(len(fails) - len(shown), 'baseline.json')}")
    per = (bt.get("layers") or {}).get("per_layer") or []
    if per:
        L += ["", "## Build walls in the .NET 10 experiment", "",
              "Each wall hides the next: restore stops before compiling, and a failing root library stops "
              "everything that depends on it.", ""]
        L += table(["Wall", "Restore", "Projects built", "Stopped on"],
                   [[x["layer"], "ok" if x["restore_succeeded"] else "failed", x["projects_built"],
                     ", ".join(f"`{c}`" for c in x["stopped_on"]) or "—"] for x in per])
    if fixes:
        L += ["", "Measurement-only fixes in the throwaway copy:", ""]
        L += [f"- `{f['file']}`: {f['package']} {f['before']} → {f['after']} ({f['reason']})" for f in fixes]
    if regressions:
        L += ["", "## Test regressions by cause", ""]
        L += table(["Cause", "Failing tests", "Example"],
                   [[link(rule_note(it), it["title"], in_table=True), len(it["tests"]),
                     f"`{it['tests'][0]['test'].split('(')[0].rsplit('.', 1)[-1]}`"] for it in items if it["tests"]])
    if plan["uncertainties"]:
        L += ["", "## Open questions", ""]
        shown = plan["uncertainties"][:limits.REPORT_LIST]
        L += [f"- {u['detail']}" for u in shown]
        if len(plan["uncertainties"]) > len(shown):
            L.append(f"- {more_line(len(plan['uncertainties']) - len(shown), 'migration-plan.json → uncertainties')}")
    vault.write(f"Evidence/{n.evidence()}", L)

    # ------------------------------------------------------------------ Background checklist (folded)
    L = [front({**base_props, "type": "migration-checklist", "code_checks": len(checks), "review_items": len(checklist),
                "tags": ["dotnet-migration", "migration/checklist"]}),
         f"# {repo.name}: background checklist", ""]
    L += callout("note", "Possible, not detected",
                 "Microsoft breaking changes that may matter here but did not fail in the build or match a rule. "
                 "Open an area when you work on it. Confirmed and found-in-code items are in "
                 + link(n.breaks(), "What will break") + ".") + [""]
    if checks:
        body = [f"- [ ] [{f['title']}]({f['reference']}) — `{f.get('evidence', '')}` "
                f"({', '.join(_short(p) for p in f.get('projects') or []) or 'repository'})" for f in checks[:limits.NOTE_ROWS]]
        L += callout("example", f"Code that uses an affected API ({len(checks)})", body, fold="-") + [""]
    by_area: dict[str, list[dict]] = {}
    for c in checklist:
        by_area.setdefault(c["area"], []).append(c)
    for area in sorted(by_area):
        body = [f"- [ ] [{c['title']}]({c['reference']}) — {c.get('change_type')}, {c.get('introduced_in')}"
                for c in by_area[area][:limits.NOTE_ROWS]]
        L += callout("note", f"{area} ({len(by_area[area])})", body, fold="-") + [""]
    vault.write(f"Background/{n.background()}", L)

    problems = vault.validate({n.start(): START_MAX_LINES})
    if problems:
        (vault.root / "_export-problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
    return str(vault.root)


def _verdict(g: dict, mine: list[dict]) -> str:
    r = g["readiness"]
    confirmed = [i for i in mine if i["tier"] == "confirmed"]
    if r == "READY":
        return "Built and tested on .NET 10 in the experiment with no new errors or regressions." + (
            " It has no test project, so only the build checks it." if not g["test_project_ids"] else "")
    if confirmed:
        return (f"{len(confirmed)} confirmed problem(s) in the experiment: "
                + "; ".join(f"{i['title']} ({_evidence_line(i)})" for i in confirmed[:3])
                + ("; …" if len(confirmed) > 3 else "") + ".")
    return PLAIN.get(r, r) + "."
