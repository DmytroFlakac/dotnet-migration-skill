---
name: obsidian-report
description: Turn structured results (JSON from an analysis, audit, test run, migration, research or study material) into a readable, linked set of Obsidian notes that stays readable at any size: a one-screen start note, findings ranked by evidence, vertical diagrams with at most 12 boxes, a zoomable Canvas board for big maps, decision records and a reviewer FAQ. Use this whenever a skill or task produces a report, findings, a plan or a dependency map that a person will read in Obsidian, or when existing generated notes are hard to read (tiny diagrams, long unranked checklists, buried key findings). Ships the obsreport Python package (no dependencies).
---

# Obsidian report

Turns results into a note set people can actually read. The package `scripts/obsreport` does the
mechanics (properties, callouts, tables, links, size-limited Mermaid diagrams, JSON Canvas, a
validator). This file says how to shape the content; `references/design-rules.md` explains why.

## When the output is bad, it is usually one of these

| Symptom | Cause | Fix with |
|---|---|---|
| Diagram text is tiny | horizontal chain or too many boxes; Obsidian shrinks Mermaid to the note width | `flow()` (always top-down, max 12 boxes), `layers()`, `neighbourhood()`, a Canvas |
| Reader cannot find what matters | everything listed with equal weight | evidence tiers, top-5 by impact on the start note |
| Checklist of hundreds of items | possible issues mixed with proven ones | tier "possible" goes to a folded Background note |
| Same empty status repeated | no data yet shown per row | say it once in the verdict |
| Codes on screen (`PHASE_1_SDK_GLOBAL`) | machine names used as labels | plain labels; codes go in properties |

## The note set (adapt the names to the domain)

```text
<R> - 00 Start here       verdict callout, 3–5 key numbers, top 5 items by impact, links. ≤ 70 lines.
<R> - 01 <Findings>       all items in evidence tiers: confirmed → found → possible (folded/linked)
<R> - 02 Route            only if there are steps: ≤ 6 vertical stages, checklists under each
<R> - 03 Map              only if there is structure: layers diagram (C4 level 1) + "<R> - Map board.canvas"
<R> - 04 Decisions        index of decision records; one note each in Decisions/ (context, options,
                          recommendation, empty Decision line, consequences)
<R> - 05 Reviewer FAQ     the questions a sceptical reader asks, answered from the data
<Items>/                  one note per item (same template: callout verdict, local diagram, evidence)
Evidence/                 how it was measured, what was not measured
Background/               the "possible" tier, folded by area
```

Prefix every note name with the subject (`<R>`) so several reports can share a vault.

## How to use the package

```python
import sys; sys.path.insert(0, "<skill>/scripts")
from obsreport import Vault, front, callout, table, link, flow, layers, neighbourhood, write_canvas, TIERS

v = Vault(Path(out) / "acme audit")                      # replaces the folder: generated notes only
v.write("acme - 00 Start here", [
    front({"type": "audit-start", "items": 12, "tags": ["audit"]}),
    "# acme: dependency audit", "",
    *callout("danger", "Verdict: 3 confirmed problems", "Two block the release."), "",
    *table(["Packages", "Confirmed", "Decisions"], [[140, 3, 1]]), "",
    "## Top fixes, by impact", "",
    f"1. **Old TLS library**: `Api` · 40 failing tests → {link('acme - Rule TLS', 'details')}",
])
v.write("acme - 03 Map", [front({"type": "map"}), "# Map", "",
    layers([("Layer 1: 2 libraries", "core, util"), ("Layer 2: 9 services", "api, web +7 more")])])
write_canvas(v.root / "acme - Map board.canvas",
             [{"id": "core", "text": link("acme - core"), "layer": 0, "color": "green"}, ...],
             [("core", "api")])
problems = v.validate({"acme - 00 Start here": 70})       # broken links, no properties,
assert not problems, problems                             # horizontal/oversized diagrams, long start note
```

Inside tables pass `in_table=True` to `link()` (escapes the alias bar). `flow()` raises
`DiagramTooBig` above 12 boxes: aggregate into layers or put the full picture on a Canvas.

## Rules that keep it readable

1. **Answer first.** The start note opens with a verdict callout a reader can act on.
2. **Evidence tiers.** 🔴 confirmed (measured) → 🟠 found (detected, not proven) → ⚪ possible (folded).
3. **Rank by impact** inside a tier; show the top 5 on the start note.
4. **Diagrams:** top-down only, ≤ 12 boxes; bigger → layers in the note + Canvas for the whole.
5. **Same template for every item note** so they compare at a glance.
6. **Scope numbers to the note.** An item note counts only its own evidence, not the total.
7. **Plain words on screen, codes in properties** (properties stay filterable with Bases/Dataview).
8. **Cap every list** (`table(limit=...)`) and say where the complete data is.
9. **Validate before handing over**; fix every problem the validator lists.
