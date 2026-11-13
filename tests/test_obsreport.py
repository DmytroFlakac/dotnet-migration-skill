"""The reusable note-set builder: limits, links, canvas (no dotnet needed)."""
import json
from pathlib import Path

import pytest

from obsreport import DiagramTooBig, Vault, callout, flow, front, layers, link, neighbourhood, safe, table, write_canvas


def test_diagrams_refuse_to_become_unreadable():
    flow([(f"n{i}", f"box {i}") for i in range(12)], [])
    with pytest.raises(DiagramTooBig):
        flow([(f"n{i}", f"box {i}") for i in range(13)], [])
    assert "flowchart TD" in layers([("Layer 1", "a, b"), ("Layer 2", "")])
    hood = neighbourhood("me", [f"up{i}" for i in range(9)], ["down"], side=4)
    assert "+5 more" in hood and hood.count('["') == 7  # 4 + more + me + 1


def test_note_building_blocks():
    assert safe('SYSLIB0057: `X509` [x] a/b') == "SYSLIB0057 X509 x ab"
    assert link("N", "label", in_table=True) == "[[N" + "\\" + "|label]]"
    assert front({"tags": ["a"], "n": 1}).startswith("---\ntags:\n  - a\nn: 1")
    assert callout("note", "T", ["x"], fold="-") == ["> [!note]- T", "> x"]
    rows = table(["A"], [[i] for i in range(5)], "data.json", limit=2)
    assert "3 more" in rows[-1] and len([r for r in rows if r.startswith("| ")]) == 3
    assert table(["A"], [[link("N", "l", in_table=True)]])[-1] == "| [[N" + "\\" + "|l]] |"   # escaped bar kept once


def test_vault_validation_and_canvas(tmp_path: Path):
    v = Vault(tmp_path / "set")
    v.write("A", [front({"t": 1}), "# A", link("B"), link("Missing")])
    v.write("Sub/B", ["# B without properties", "```mermaid", "flowchart LR", '  a["x"]', "```"])
    write_canvas(v.root / "board.canvas", [{"id": "a", "text": link("A"), "layer": 0, "color": "green"},
                                           {"id": "b", "text": link("Nope"), "layer": 1}], [("a", "b")])
    problems = v.validate({"A": 2})
    assert "A: broken link [[Missing]]" in problems
    assert "B: no properties" in problems and "B: horizontal diagram" in problems
    assert any(x.startswith("A: ") and x.endswith("lines > 2") for x in problems) and "board.canvas: broken link [[Nope]]" in problems
    board = json.loads((v.root / "board.canvas").read_text(encoding="utf-8"))
    assert board["nodes"][0]["color"] == "4" and board["edges"][0]["fromNode"] == "a"
    assert board["nodes"][1]["y"] > board["nodes"][0]["y"]       # layers go top to bottom
