"""obsreport: turn structured results into a readable, linked set of Obsidian notes.

Reusable by any skill that produces a report (a migration analysis, a dependency audit, study
material). It knows nothing about .NET. It provides:

  notes     front matter, callouts, tables, links, safe note names   (notes.py)
  diagrams  Mermaid flowcharts that refuse to grow unreadable         (diagrams.py)
  canvas    a zoomable JSON Canvas board for maps too big for a note  (canvas.py)
  vault     writes a note set and checks it: links resolve, every note
            has properties, diagrams stay within the limits          (vault.py)

Design rules (see the README): overview first, zoom and filter, details on demand (Shneiderman);
the answer first (BLUF / Pyramid Principle); evidence in tiers; vertical diagrams with at most
DIAGRAM_MAX_NODES boxes, bigger maps go to a Canvas; plain labels on screen, codes in properties.
"""
from .canvas import write_canvas
from .diagrams import MAX_NODES as DIAGRAM_MAX_NODES
from .diagrams import DiagramTooBig, flow, layers, neighbourhood
from .notes import TIERS, callout, front, link, more_line, safe, table
from .vault import Vault

__version__ = "0.1.0"
DIAGRAM_MAX_ROW = 8

__all__ = ["Vault", "write_canvas", "flow", "layers", "neighbourhood", "DiagramTooBig", "front", "callout",
           "table", "link", "safe", "more_line", "TIERS", "DIAGRAM_MAX_NODES", "DIAGRAM_MAX_ROW"]
