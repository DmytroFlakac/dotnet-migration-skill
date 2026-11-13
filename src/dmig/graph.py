"""Steps A4–A5: project graph, consumers, applications reached, and test association."""
from __future__ import annotations

from collections import defaultdict


class ProjectGraph:
    def __init__(self, projects: list[dict]):
        self.projects = {p["id"]: p for p in projects}
        self.deps: dict[str, set[str]] = {pid: set(p["project_references"]) & self.projects.keys()
                                          for pid, p in self.projects.items()}
        self.consumers: dict[str, set[str]] = defaultdict(set)
        for pid, deps in self.deps.items():
            for dep in deps:
                self.consumers[dep].add(pid)

    def closure(self, pid: str) -> set[str]:
        """pid and everything it depends on, transitively."""
        seen, stack = set(), [pid]
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.deps.get(cur, ()))
        return seen

    def all_consumers(self, pid: str) -> set[str]:
        """Everything that depends on pid, transitively (pid excluded)."""
        seen, stack = set(), list(self.consumers.get(pid, ()))
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.consumers.get(cur, ()))
        return seen

    def kind(self, pid: str) -> str:
        return self.projects[pid]["kind"]

    def applications_reaching(self, pid: str) -> set[str]:
        """Applications that use pid directly or transitively (pid itself if it is one)."""
        apps = {c for c in self.all_consumers(pid) if self.kind(c) == "application"}
        if self.kind(pid) == "application":
            apps.add(pid)
        return apps

    def topo_order(self) -> list[str]:
        """Dependencies first (leaf → root); ties broken by id for stable output."""
        order, temp, done = [], set(), set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in temp:  # cycle: keep going, the report flags it
                return
            temp.add(node)
            for dep in sorted(self.deps.get(node, ())):
                visit(dep)
            temp.discard(node)
            done.add(node)
            order.append(node)

        for node in sorted(self.projects):
            visit(node)
        return order

    def cycles(self) -> list[list[str]]:
        found, color = [], {}

        def dfs(node: str, path: list[str]) -> None:
            color[node] = 1
            for dep in sorted(self.deps.get(node, ())):
                if color.get(dep) == 1:
                    found.append(path[path.index(dep):] + [dep])
                elif color.get(dep) is None:
                    dfs(dep, path + [dep])
            color[node] = 2

        for node in sorted(self.projects):
            if color.get(node) is None:
                dfs(node, [node])
        return found


def _base_name(name: str) -> str:
    low = name.lower()
    for suffix in (".unittests", ".tests", ".test", ".integrationtests", ".specs"):
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return name


def associate_tests(graph: ProjectGraph) -> list[dict]:
    """A5: tests validate the production projects they reference. Names are only a hint."""
    by_name = {p["name"].lower(): pid for pid, p in graph.projects.items() if p["kind"] != "test"}
    result = []
    for pid, proj in sorted(graph.projects.items()):
        if proj["kind"] != "test":
            continue
        validates = sorted(d for d in graph.deps[pid] if graph.kind(d) != "test")
        hint = by_name.get(_base_name(proj["name"]).lower())
        evidence = [{"type": "project_reference", "target": v} for v in validates]
        if proj["test_frameworks"]:
            evidence.append({"type": "test_framework", "value": ",".join(proj["test_frameworks"])})
        solutions = proj.get("solutions", [])
        if solutions:
            evidence.append({"type": "solution_membership", "value": ",".join(solutions)})
        result.append({
            "test_project": pid,
            "validates": validates,
            "name_hint": hint,
            "name_hint_agrees": hint in validates if hint else None,
            "frameworks": proj["test_frameworks"],
            "evidence": evidence,
            "confidence": "high" if validates else "low",
            "note": None if validates else "No production project reference: coverage is NOT claimed "
                                           "from the name alone.",
        })
    return result
