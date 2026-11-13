"""Size caps for everything an agent is expected to read.

A monorepo with thousands of projects must not produce a report that costs thousands of tokens to
read. Readable outputs (response.json, migration-report.md, the Obsidian notes) show at most these
many rows and then say how many more exist and where the complete data is. The machine-readable
JSON files (inventory, plan, baseline, experiment) stay complete; agents open them only when needed.
"""
from __future__ import annotations

RESPONSE_ITEMS = 15        # unresolved_items in response.json
REPORT_ROWS = 25           # rows per table in migration-report.md
REPORT_LIST = 10           # bullet lines per list in migration-report.md
NOTE_ROWS = 200            # rows per table in an Obsidian note
DIAGRAM_NODES = 30         # nodes per Mermaid diagram


def cap(items: list, limit: int) -> tuple[list, int]:
    """(first `limit` items, how many were left out)."""
    return items[:limit], max(0, len(items) - limit)


def more_line(omitted: int, where: str) -> str:
    return f"… and {omitted} more — complete list in {where}." if omitted else ""
