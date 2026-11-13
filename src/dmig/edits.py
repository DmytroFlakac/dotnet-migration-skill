"""Write repository files back in the style they were read.

A patch should show only what changed. Writing a file with different line endings (Python on
Windows turns "\\n" into "\\r\\n"), a different indent, a lost byte-order mark or a lost final newline
makes every line look changed. Seen in a real run: a two-value global.json edit became a whole-file diff.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BOM = "﻿"


def read(path: Path) -> str:
    """The file's text exactly as stored (byte-order mark and line endings kept)."""
    return path.read_bytes().decode("utf-8", errors="replace")


def write_like(path: Path, new_text: str, original: str) -> None:
    """Write new_text using the original's line endings, byte-order mark and final-newline choice."""
    newline = "\r\n" if "\r\n" in original else "\n"
    text = new_text.lstrip(BOM).replace("\r\n", "\n")
    if not original.rstrip(BOM).endswith(("\n", "\r")):
        text = text.rstrip("\n")
    elif not text.endswith("\n"):
        text += "\n"
    if newline != "\n":
        text = text.replace("\n", newline)
    path.write_bytes(((BOM if original.startswith(BOM) else "") + text).encode("utf-8"))


def json_indent(original: str) -> int:
    m = re.search(r"\n( +)\S", original)
    return len(m.group(1)) if m else 2


def write_json_like(path: Path, data, original: str) -> None:
    """json.dumps with the original's indent, then write_like."""
    write_like(path, json.dumps(data, indent=json_indent(original), ensure_ascii=False) + "\n", original)
