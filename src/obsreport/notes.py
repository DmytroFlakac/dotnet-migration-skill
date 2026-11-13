"""Building blocks for one note: properties, callouts, tables, links."""
from __future__ import annotations

import re

NOTE_ROWS = 200

# Evidence tiers: the strongest evidence is shown first and expanded; the weakest is folded away.
TIERS = {
    "confirmed": {"icon": "🔴", "label": "Confirmed", "meaning": "failed in a real build or test run"},
    "found": {"icon": "🟠", "label": "Found in code", "meaning": "a rule matched the source; not proven by a build"},
    "possible": {"icon": "⚪", "label": "Possible", "meaning": "applies to technologies in use; check by hand"},
}


def safe(text: str) -> str:
    """A string that is valid as an Obsidian note name."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]`]', "", str(text))).strip()


def link(note: str, label: str | None = None, in_table: bool = False) -> str:
    """Wikilink; inside a Markdown table the alias bar must be escaped."""
    if not label or label == note:
        return f"[[{note}]]"
    return f"[[{note}\\|{label}]]" if in_table else f"[[{note}|{label}]]"


def _yaml(value) -> str:
    if isinstance(value, list):
        return "\n" + "\n".join(f"  - {_yaml(v)}" for v in value) if value else " []"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    s = str(value)
    return f'"{s}"' if re.search(r"[:#\[\]{}&*!|>'\"%@`,]", s) or s != s.strip() else s


def front(props: dict) -> str:
    """YAML front matter (Obsidian properties)."""
    lines = ["---"]
    for k, v in props.items():
        rendered = _yaml(v)
        lines.append(f"{k}:{rendered}" if rendered.startswith("\n") or rendered == " []" else f"{k}: {rendered}")
    return "\n".join(lines) + "\n---\n"


def callout(kind: str, title: str, lines: list[str] | str = (), fold: str = "") -> list[str]:
    """`> [!kind]± title` followed by the body; fold is '', '+' (open) or '-' (collapsed)."""
    body = [lines] if isinstance(lines, str) else list(lines)
    return [f"> [!{kind}]{fold} {title}"] + [f"> {l}" if l else ">" for l in body]


def more_line(omitted: int, where: str) -> str:
    return f"… and {omitted} more — complete list in {where}." if omitted else ""


def table(headers: list[str], rows: list[list], where: str = "", limit: int = NOTE_ROWS) -> list[str]:
    """Markdown table, capped at `limit` rows with a pointer to the complete data."""
    shown, omitted = rows[:limit], max(0, len(rows) - limit)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in shown:
        cells = [str(c).replace("\n", " ") for c in r]
        # escape bars that are not already the escaped alias bar of a wikilink
        cells = [re.sub(r"(?<!\\)\|", r"\\|", c) for c in cells]
        out.append("| " + " | ".join(cells) + " |")
    return out + (["", more_line(omitted, where)] if omitted else [])
