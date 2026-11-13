"""JSON Canvas (https://jsoncanvas.org) boards for maps too big for a note.

Nodes are text cards holding a wikilink, so the board works wherever the note set sits inside
the vault (file nodes would need vault-relative paths, which the writer does not know).
Layout: one band per layer, top to bottom, wrapped every `per_row` cards.
"""
from __future__ import annotations

import json
from pathlib import Path

CARD_W, CARD_H, GAP_X, GAP_Y = 280, 120, 40, 80
# Obsidian canvas colour presets: 1 red, 2 orange, 3 yellow, 4 green, 5 cyan, 6 purple
COLORS = {"red": "1", "orange": "2", "yellow": "3", "green": "4", "cyan": "5", "purple": "6"}


def write_canvas(path: Path, cards: list[dict], edges: list[tuple[str, str]], per_row: int = 6) -> Path:
    """cards: {"id", "text" (Markdown, may contain [[links]]), "layer" (int), "color" (name or None)}.
    edges: (from_id, to_id), drawn from the bottom of one card to the top of the other."""
    by_layer: dict[int, list[dict]] = {}
    for c in cards:
        by_layer.setdefault(int(c.get("layer", 0)), []).append(c)
    nodes, y = [], 0
    widest = max((min(len(v), per_row) for v in by_layer.values()), default=1)
    for layer in sorted(by_layer):
        row = by_layer[layer]
        for start in range(0, len(row), per_row):
            chunk = row[start:start + per_row]
            offset = (widest - len(chunk)) * (CARD_W + GAP_X) // 2
            for i, c in enumerate(chunk):
                node = {"id": c["id"], "type": "text", "text": c["text"], "x": offset + i * (CARD_W + GAP_X),
                        "y": y, "width": CARD_W, "height": CARD_H}
                if c.get("color") in COLORS:
                    node["color"] = COLORS[c["color"]]
                nodes.append(node)
            y += CARD_H + GAP_Y
        y += GAP_Y  # extra space between layers
    ids = {n["id"] for n in nodes}
    canvas_edges = [{"id": f"e{i}", "fromNode": a, "fromSide": "bottom", "toNode": b, "toSide": "top"}
                    for i, (a, b) in enumerate(edges) if a in ids and b in ids]
    path.write_text(json.dumps({"nodes": nodes, "edges": canvas_edges}, indent=1), encoding="utf-8")
    return path
