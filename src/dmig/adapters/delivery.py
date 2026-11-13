"""IDelivery: branch and merge request for an approved result. The core never calls it.

Mock: LocalFileDelivery writes `migration.patch` and `migration-report.md` into a folder, and nothing
else: no branch, commit, push or merge request. The branch name it would use is
checked against the branch policy and written into the report.
"""
from __future__ import annotations

import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

DEFAULT_BRANCH_POLICY = r"^feature/TEST-\d+-[a-z0-9][a-z0-9-]*$"


class IDelivery(ABC):
    @abstractmethod
    def branch_name(self, work_item: str, summary: str) -> str: ...

    @abstractmethod
    def deliver(self, patch: Path, result: dict, work_item: str, summary: str) -> dict:
        """Create branch + commit + push + merge request (production), or the local equivalent."""


class LocalFileDelivery(IDelivery):
    def __init__(self, folder: Path, branch_policy: str = DEFAULT_BRANCH_POLICY):
        self.folder, self.policy = Path(folder), re.compile(branch_policy)

    def branch_name(self, work_item: str, summary: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:40] or "dotnet10"
        name = f"feature/{work_item}-{slug}"
        if not self.policy.match(name):
            raise ValueError(f"branch name {name!r} does not match the policy {self.policy.pattern!r}")
        return name

    def deliver(self, patch: Path | None, result: dict, work_item: str, summary: str) -> dict:
        """patch None (nothing to change, or rolled back): only the report is written."""
        self.folder.mkdir(parents=True, exist_ok=True)
        branch = self.branch_name(work_item, summary)
        if patch is not None and Path(patch).exists():
            shutil.copy2(patch, self.folder / "migration.patch")
        verified = result.get("rollback_verified", (result.get("rollback") or {}).get("verified"))
        lines = [f"# {work_item}: {summary}", "",
                 f"- **Status:** {result.get('status')}",
                 f"- **Ready for human review:** {bool(result.get('ready_for_human_review'))}",
                 f"- **Rollback verified:** {bool(verified)}",
                 f"- **Branch it would use:** `{branch}`",
                 f"- **Files changed:** {len(result.get('files_changed') or [])}"]
        if result.get("stop_reason"):
            lines.append(f"- **Stop reason:** {result['stop_reason']}")
        lines += ["", "Apply with `git apply migration.patch`. Nothing was committed, pushed or opened."]
        unresolved = result.get("unresolved_items") or []
        if unresolved:
            lines += ["", "## Open items", ""] + [f"- {u}" for u in unresolved[:50]]
        (self.folder / "migration-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"branch": branch, "work_item": work_item, "patch": "migration.patch",
                "report": "migration-report.md", "status": result.get("status")}


SourceControl, LocalReviewPackage = IDelivery, LocalFileDelivery  # earlier names (they meant delivery)
