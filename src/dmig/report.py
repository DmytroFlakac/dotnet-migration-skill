"""Human-readable migration report (migration-report.md), answer first and size-capped.

Structure follows BLUF / the Pyramid Principle: the conclusion and the decisions come first, then
the reasons, then the evidence. Every table and list is capped (limits.py) so the file stays small
on huge monorepos; full detail lives in the JSON files and in one Obsidian note per group.
"""
from __future__ import annotations

import json

from . import limits

RAG = {"READY": "🟢", "WAITS_FOR_DEPENDENCY_REPAIR": "🟡", "NEEDS_KNOWN_REPAIR": "🟡", "NEEDS_HUMAN_REVIEW": "🟠",
       "BLOCKED_PLATFORM": "🟠", "BLOCKED_NEEDS_INVESTIGATION": "🔴",
       "NOT_VERIFIED_NO_EXPERIMENT": "⚪", "NOT_VERIFIED_NO_BASELINE": "⚪",
       "OUT_OF_SCOPE_ADAPTER_DISABLED": "⚫", "OUT_OF_SCOPE_REPORT_ONLY": "⚫"}
PLAIN = {"READY": "ready", "WAITS_FOR_DEPENDENCY_REPAIR": "waits for a dependency's repair",
         "NEEDS_KNOWN_REPAIR": "needs a known (automatic) repair", "NEEDS_HUMAN_REVIEW": "needs a human decision",
         "BLOCKED_PLATFORM": "needs a Windows machine to validate",
         "BLOCKED_NEEDS_INVESTIGATION": "blocked: failures no rule explains",
         "NOT_VERIFIED_NO_EXPERIMENT": "not verified (no experiment)", "NOT_VERIFIED_NO_BASELINE": "not verified (no baseline)",
         "OUT_OF_SCOPE_ADAPTER_DISABLED": "out of scope: its framework add-on is off (--adapters)",
         "OUT_OF_SCOPE_REPORT_ONLY": "out of scope: listed only (legacy/MAUI/Xamarin/Functions)"}


def _name(pid: str) -> str:
    return pid.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _table(headers: list[str], rows: list[list], where: str) -> list[str]:
    shown, omitted = limits.cap(rows, limits.REPORT_ROWS)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in shown]
    if omitted:
        out += ["", limits.more_line(omitted, where)]
    return out


def _bullets(lines: list[str], where: str) -> list[str]:
    shown, omitted = limits.cap(lines, limits.REPORT_LIST)
    return shown + ([f"- {limits.more_line(omitted, where)}"] if omitted else [])


def render(response: dict, preflight: dict, inventory: dict, baseline: dict | None, experiment: dict | None,
           compat: dict, plan: dict) -> str:
    groups = plan["groups"]
    counts = response["summary"]["readiness_counts"]
    L: list[str] = ["# .NET 8 → .NET 10 migration analysis", ""]

    # 1. Bottom line ------------------------------------------------------------------------
    first = next((g for g in groups), None)
    blocked = [g for g in groups if g["readiness"].startswith("BLOCKED")]
    L += ["## Bottom line", "",
          f"- **Status:** `{response['status']}` — {len(inventory['projects'])} projects in {len(groups)} migration "
          f"group(s); " + ", ".join(f"{RAG.get(k, '')} {v} {PLAIN.get(k, k)}" for k, v in counts.items()) + ".",
          f"- **Start with:** PHASE_1 (SDK/CI), then `{first['group_id']}`." if first else "- No groups found.",
          f"- **Needs a person:** {len(blocked)} blocked group(s), {len(plan['blockers'])} blocker(s), "
          f"{len(plan['uncertainties'])} uncertainty item(s)." if (blocked or plan["blockers"]) else
          "- **Needs a person:** nothing blocking; see the uncertainties for what was not verified.",
          f"- **Safety:** repository changed: {response['changes_persisted']}; experiment rollback verified: "
          f"{response['rollback_verified']}. Nothing was committed or pushed.",
          "", "> Full detail, one note per group, is in the Obsidian folder (`obsidian/`; open the note "
              "ending in `00 Overview`). The JSON files hold the complete data.", ""]

    # 2. Group order (the decision) ------------------------------------------------------------
    L += ["## Migration order", ""]
    L += _table(["#", "Group", "Status", "Projects", "Tests", "Depends on", "Main reason"], [
        [g["order"], f"`{g['group_id']}`", f"{RAG.get(g['readiness'], '')} {PLAIN.get(g['readiness'], g['readiness'])}",
         len(g["project_ids"]), len(g["test_project_ids"]), ", ".join(g["depends_on_group_ids"]) or "—",
         _reason(g)] for g in groups], "migration-plan.json → groups")
    L.append("")

    # 3. What breaks ---------------------------------------------------------------------------
    issues = [f for f in compat["findings"] if f.get("effect") not in {"none", "none_known", "review_api_usage"}
              and f["rule_id"] != "PKG-CONFIRMED-BY-EXPERIMENT"]
    checks = [f for f in compat["findings"] if f.get("effect") == "review_api_usage"]
    checklist = compat.get("review_checklist", [])
    L += ["## What breaks on .NET 10", ""]
    if issues:
        L += _table(["Rule", "What", "Where", "Effect", "Fix scope"], [
            [f["rule_id"], f["title"],
             (", ".join(_name(p) for p in (f.get("projects") or [])[:3])
              + (f" +{len(f['projects']) - 3}" if len(f.get("projects") or []) > 3 else "")) or "repository",
             f.get("effect"), (f.get("repair") or {}).get("change_class", "—")] for f in issues],
            "compatibility.json → findings")
    else:
        L.append("No breaking changes detected.")
    L += ["", f"To check (not detected as failures): {len(checks)} place(s) where the code uses an API that a "
              f"Microsoft breaking change concerns, and {len(checklist)} review item(s) for the technologies this "
              "repository uses — both in the Obsidian note ending in `Review checklist`."]
    L.append("")

    # 4. Plan ------------------------------------------------------------------------------------
    L += ["## Plan (phases)", ""]
    for p in plan["phases"]:
        n = len(p["items"])
        L.append(f"- **{p['id']}** — {p['status']} — {p['description']}" + (f" ({n} item(s))" if n else ""))
    L.append("")

    # 5. Evidence ---------------------------------------------------------------------------------
    L += ["## Evidence", ""]
    L.append(f"- **Environment:** preflight `{preflight['status']}`; SDK in repository "
             f"`{preflight['selected_sdk_in_repository']}`.")
    if baseline:
        L.append(f"- **Baseline** (unchanged repository, built in a disposable worktree): build "
                 f"{'succeeded' if baseline['build_succeeded'] else 'FAILED'}; {baseline['tests']['total']} test "
                 f"result(s), {baseline['tests']['failed']} already failing before any change (kept separate, never 'fixed').")
    if experiment and experiment.get("status") == "EXECUTED":
        bt = experiment["build_test"]
        cmp = experiment.get("test_comparison", {})
        L.append(f"- **net10.0 experiment** (every project retargeted at once in a throwaway worktree): build "
                 f"{'succeeded' if bt['build_succeeded'] else 'failed'}; "
                 f"{len(experiment.get('diagnostic_comparison', {}).get('new_errors', []))} new error(s), "
                 f"{len(cmp.get('regressions', []))} test regression(s). One group's error fails the whole experiment "
                 "build; the order table attributes each failure to its group.")
    L.append("")

    # 6. Open items -------------------------------------------------------------------------------
    L += ["## Blockers and uncertainties", ""]
    lines = [f"- ⛔ {b['group_id'] or 'environment'}: {b['detail']}" for b in plan["blockers"]] + \
            [f"- ❔ `{u['code']}`: {u['detail']}" for u in plan["uncertainties"]]
    L += _bullets(lines, "migration-plan.json → blockers / uncertainties") if lines else ["None."]
    L += ["", "## Not done by this analysis", "",
          "- No file in the repository was modified; no commit, push, pull request or work item.",
          "- No repair was applied: repairs run in implementation mode, one approved group at a time.",
          "- Vulnerability fixes and general package modernisation are out of scope.", ""]
    return "\n".join(L)


def _reason(g: dict) -> str:
    ex = g["experiment"]
    if ex["new_errors"]:
        e = ex["new_errors"][0]
        return f"{e['code']} → rule {e['matched_rule'] or 'none'}"
    if ex["test_regressions"]:
        r = ex["test_regressions"][0]
        return (f"{len(ex['test_regressions'])} test regression(s) → {', '.join(r['probable_cause'] or ['unknown'])}"
                + (f" (fixed in {', '.join(r['inherited_from_groups'])})" if r.get("inherited_from_groups") else ""))
    if g["readiness"] == "BLOCKED_PLATFORM":
        return "Windows-only projects"
    if not g["test_project_ids"]:
        return "no tests: build-only validation"
    return "built and tested on net10.0"


def _json(obj) -> str:
    return json.dumps(obj, indent=2)
