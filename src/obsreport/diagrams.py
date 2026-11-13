"""Mermaid diagrams that stay readable inside a note.

Obsidian scales a Mermaid diagram down to the note width, so a wide or crowded diagram becomes
unreadable. Every diagram here is vertical (top-down) and has at most DIAGRAM_MAX_NODES boxes;
asking for more raises DiagramTooBig, so the caller aggregates (layers) or uses a Canvas instead.
"""
from __future__ import annotations

MAX_NODES = 12


class DiagramTooBig(ValueError):
    pass


def _label(text: str) -> str:
    return str(text).replace('"', "'").replace("[", "(").replace("]", ")")


def flow(nodes: list[tuple], edges: list[tuple[str, str]], styles: dict[str, str] | None = None,
         caption: str = "") -> str:
    """nodes: (id, label[, style_name]); edges: (from_id, to_id); styles: style_name -> 'stroke:#hex'.
    Always top-down. Raises DiagramTooBig above MAX_NODES boxes."""
    if len(nodes) > MAX_NODES:
        raise DiagramTooBig(f"{len(nodes)} boxes > {MAX_NODES}: aggregate or use a canvas")
    lines = ["```mermaid", "flowchart TD"]
    for n in nodes:
        lines.append(f'  {n[0]}["{_label(n[1])}"]' + (f":::{n[2]}" if len(n) > 2 and n[2] else ""))
    ids = {n[0] for n in nodes}
    lines += [f"  {a} --> {b}" for a, b in edges if a in ids and b in ids]
    for name, css in (styles or {}).items():
        lines.append(f"  classDef {name} {css}")
    lines.append("```")
    return "\n".join(lines + ([f"", f"*{caption}*"] if caption else []))


def layers(rows: list[tuple[str, str]], caption: str = "") -> str:
    """A vertical chain of aggregated boxes: (title, detail) per layer, e.g. C4 level 1."""
    nodes = [(f"l{i}", f"{t}<br/>{d}" if d else t) for i, (t, d) in enumerate(rows)]
    return flow(nodes, [(f"l{i}", f"l{i + 1}") for i in range(len(rows) - 1)], caption=caption)


def neighbourhood(center: str, upstream: list[str], downstream: list[str], side: int = 4,
                  center_style: str = "", caption: str = "") -> str:
    """One item with what it needs (above) and what needs it (below); each side capped at `side`
    boxes plus a '+N more' box."""
    def capped(items: list[str], prefix: str) -> list[tuple]:
        shown = [(f"{prefix}{i}", x) for i, x in enumerate(items[:side])]
        if len(items) > side:
            shown.append((f"{prefix}more", f"+{len(items) - side} more"))
        return shown
    up, down = capped(upstream, "u"), capped(downstream, "d")
    nodes = up + [("c", center, "me")] + down
    edges = [(u[0], "c") for u in up] + [("c", d[0]) for d in down]
    return flow(nodes, edges, {"me": "stroke-width:3px"} if not center_style else {"me": center_style},
                caption=caption)
