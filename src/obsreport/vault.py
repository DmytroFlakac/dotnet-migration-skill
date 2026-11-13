"""A note set on disk, written in one place and checked before anyone reads it."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .diagrams import MAX_NODES

LINK = re.compile(r"\[\[([^\]|#\\]+)")
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.S)
MERMAID_NODE = re.compile(r'^\s+(\w+)\["', re.M)


class Vault:
    """root: the folder that holds this note set (replaced on every export)."""

    def __init__(self, root: Path, fresh: bool = True):
        self.root = root
        if fresh:
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        self.written: list[Path] = []

    def write(self, rel: str, lines: list[str] | str) -> Path:
        """rel: 'Folder/Note name' without .md."""
        path = self.root / f"{rel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = lines if isinstance(lines, str) else "\n".join(lines)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        self.written.append(path)
        return path

    def validate(self, max_lines: dict[str, int] | None = None) -> list[str]:
        """Problems that make a note set hard to use: broken links, missing properties, diagrams
        over the box limit, notes longer than their budget ({note name: max lines})."""
        notes = {p.stem: p for p in self.root.rglob("*.md")}
        problems = []
        for name, p in notes.items():
            text = p.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                problems.append(f"{name}: no properties")
            for target in LINK.findall(text):
                if target.strip() not in notes:
                    problems.append(f"{name}: broken link [[{target}]]")
            for block in MERMAID.findall(text):
                if "flowchart LR" in block:
                    problems.append(f"{name}: horizontal diagram")
                n = len(set(MERMAID_NODE.findall(block)))
                if n > MAX_NODES:
                    problems.append(f"{name}: diagram with {n} boxes")
            limit = (max_lines or {}).get(name)
            if limit and len(text.splitlines()) > limit:
                problems.append(f"{name}: {len(text.splitlines())} lines > {limit}")
        for c in self.root.rglob("*.canvas"):
            data = json.loads(c.read_text(encoding="utf-8"))
            for n in data.get("nodes", []):
                for target in LINK.findall(n.get("text", "")):
                    if target.strip() not in notes:
                        problems.append(f"{c.name}: broken link [[{target}]]")
        return problems
