"""Step A6: migration groups, shared libraries, platform labels and group ordering.

Rules:
* A *root* is an application, or a library nothing else (except tests) consumes.
* A library reached by two or more roots is a SHARED library: it goes into the
  shared-foundation group and is multi-targeted until its last .NET 8 consumer migrates.
* Every root owns one group: itself, its exclusive dependencies and the tests that
  validate them. Tests of shared libraries belong to the shared-foundation group.
"""
from __future__ import annotations

import re

from .graph import ProjectGraph

SHARED_GROUP = "shared-foundation"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def temporary_tfms(project: dict) -> list[str]:
    tfms = project["target_frameworks"] or ["net8.0"]
    out = []
    for tfm in tfms:
        out.append(tfm)
        if tfm.startswith("net8.0"):
            out.append(tfm.replace("net8.0", "net10.0", 1))
    return list(dict.fromkeys(out))


def build_groups(graph: ProjectGraph, tests: list[dict]) -> dict:
    non_test = [pid for pid in graph.projects if graph.kind(pid) != "test"]
    roots = sorted(pid for pid in non_test if graph.kind(pid) == "application"
                   or not any(graph.kind(c) != "test" for c in graph.consumers.get(pid, ())))
    closures = {r: graph.closure(r) for r in roots}
    roots_reaching = {pid: sorted(r for r in roots if pid in closures[r]) for pid in non_test}
    shared = sorted(pid for pid in non_test if pid not in roots and len(roots_reaching[pid]) >= 2)

    groups: dict[str, dict] = {}
    owner: dict[str, str] = {}
    if shared:
        groups[SHARED_GROUP] = _new_group(SHARED_GROUP, "Shared foundation libraries", shared, "SHARED_FOUNDATION")
        for pid in shared:
            owner[pid] = SHARED_GROUP
        groups[SHARED_GROUP]["grouping_evidence"].append(
            "Each library here is reached by two or more roots: " + "; ".join(
                f"{graph.projects[p]['name']} ← {', '.join(graph.projects[r]['name'] for r in roots_reaching[p])}"
                for p in shared))
    for root in roots:
        kind = "APPLICATION" if graph.kind(root) == "application" else "STANDALONE_LIBRARY"
        gid = ("app-" if kind == "APPLICATION" else "component-") + _slug(graph.projects[root]["name"])
        members = sorted(p for p in closures[root] if p not in shared)
        g = _new_group(gid, f"{graph.projects[root]['name']} and its own projects", members, kind)
        g["root"] = root
        g["grouping_evidence"].append(
            f"Root {graph.projects[root]['name']} ({kind.lower().replace('_', ' ')}); members are its "
            "transitive ProjectReferences that no other root uses.")
        groups[gid] = g
        for pid in members:
            owner[pid] = gid

    unassigned = []
    for t in tests:
        targets = [owner[v] for v in t["validates"] if v in owner]
        if not targets:
            unassigned.append({"project": t["test_project"],
                               "reason": "test project references no production project in the repository"})
            continue
        # A test that touches an application group belongs there; tests of shared code stay shared.
        gid = next((g for g in targets if g != SHARED_GROUP), targets[0])
        groups[gid]["test_project_ids"].append(t["test_project"])
        owner[t["test_project"]] = gid
        if len(set(targets)) > 1:
            groups[gid]["grouping_evidence"].append(
                f"{graph.projects[t['test_project']]['name']} also references shared code; it is validated with "
                f"{gid} and must pass for every TFM of the shared library.")

    for gid, g in groups.items():
        member_set = set(g["project_ids"]) | set(g["test_project_ids"])
        deps = {owner[d] for m in member_set for d in graph.deps.get(m, ()) if d in owner} - {gid}
        g["depends_on_group_ids"] = sorted(deps)
        g["shared_project_ids"] = sorted({d for m in member_set for d in graph.closure(m) if d in shared}
                                         - set(g["project_ids"])) if gid != SHARED_GROUP else []
        g["entrypoints"] = sorted(p for p in g["project_ids"] if graph.kind(p) == "application")
        g["platform"] = "WINDOWS" if any(graph.projects[p]["platform"] == "WINDOWS" for p in member_set) else "ANY"
        g["platform_constraints"] = [f"{graph.projects[p]['name']} requires Windows" for p in sorted(member_set)
                                     if graph.projects[p]["platform"] == "WINDOWS"]
        evaluated = all(graph.projects[p]["evaluated"] for p in member_set)
        unresolved = any(graph.projects[p]["unresolved_references"] for p in member_set)
        g["confidence"] = "high" if evaluated and not unresolved else ("medium" if not unresolved else "low")
        g["test_project_ids"].sort()
        if g["shared_project_ids"]:
            g["grouping_evidence"].append("Uses shared libraries (overlap, handled by multi-targeting): "
                                          + ", ".join(graph.projects[s]["name"] for s in g["shared_project_ids"]))

    order = _order(groups)
    for i, gid in enumerate(order, 1):
        groups[gid]["order"] = i

    shared_records = []
    for pid in shared:
        consumer_groups = sorted({owner[c] for c in graph.all_consumers(pid) if c in owner and owner[c] != SHARED_GROUP})
        proj = graph.projects[pid]
        shared_records.append({
            "project_id": pid,
            "name": proj["name"],
            "current_consumers": sorted(graph.consumers.get(pid, ())),
            "consumer_groups": consumer_groups,
            "remaining_net8_consumers": sorted(c for c in graph.all_consumers(pid)
                                               if any(t.startswith("net8.0") for t in graph.projects[c]["target_frameworks"])),
            "current_tfms": proj["target_frameworks"],
            "temporary_tfms": temporary_tfms(proj),
            "retirement_condition_for_net8": "Remove net8.0 only after these groups have migrated: "
                                             + ", ".join(consumer_groups),
            "package_strategy_per_tfm": [],  # filled by compatibility analysis
            "owning_migration_phase": "PHASE_3_MULTITARGET_SHARED",
            "validation_matrix": [
                {"tfm": tfm, "build": True,
                 "tests": sorted(t["test_project"] for t in tests if pid in t["validates"])}
                for tfm in temporary_tfms(proj)],
            "shared_library_policy": "MULTITARGET_UNTIL_LAST_CONSUMER",
        })
    return {"groups": [groups[g] for g in order], "shared_projects": shared_records,
            "unassigned_projects": unassigned, "roots": roots}


def _new_group(gid: str, name: str, members: list[str], kind: str) -> dict:
    return {"group_id": gid, "name": name, "group_kind": kind, "project_ids": sorted(members),
            "test_project_ids": [], "shared_project_ids": [], "depends_on_group_ids": [],
            "entrypoints": [], "platform": "ANY", "platform_constraints": [], "grouping_evidence": [],
            "confidence": "high"}


def _order(groups: dict[str, dict]) -> list[str]:
    """Shared foundation first, then groups whose dependencies are done, smallest first."""
    remaining, order = dict(groups), []
    while remaining:
        ready = [g for g, v in remaining.items() if set(v["depends_on_group_ids"]) <= set(order)]
        if not ready:  # cycle between groups: keep deterministic and let the report flag it
            ready = list(remaining)
        ready.sort(key=lambda g: (g != SHARED_GROUP, len(remaining[g]["project_ids"]), g))
        order.append(ready[0])
        del remaining[ready[0]]
    return order
