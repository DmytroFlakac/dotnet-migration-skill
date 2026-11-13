# Design rules and where they come from

Read when adapting the note set to a new kind of report, or when a reader says the notes are hard
to use.

| Rule | Source | What it fixes |
|---|---|---|
| Overview first, zoom and filter, details on demand | Ben Shneiderman, "The eyes have it" (1996): the visual information-seeking mantra | a reader drowning in detail before seeing the shape |
| Bottom line up front | US military writing (BLUF); Barbara Minto, *The Pyramid Principle* | the conclusion buried at the end |
| Zoom levels for structure | Simon Brown, C4 model: context → containers → components | one giant dependency graph |
| Evidence tiers | graded evidence in medicine and security triage | proven and speculative items looking equal |
| Narrative + FAQ | Amazon six-pager and PR/FAQ | reviewers asking the same questions every time |
| Decision records | Michael Nygard, Architecture Decision Records (2011) | decisions lost in chat; the tool silently guessing |
| Split by purpose | Daniele Procida, Diátaxis: tutorial / how-to / reference / explanation | notes mixing "what to do" with "why" |
| Small multiples, data-ink | Edward Tufte | inconsistent per-item notes; decoration and repeated empty statuses |
| Progressive disclosure | interaction design (Nielsen Norman Group) | long checklists: fold the weak tier |

## Obsidian-specific facts the package relies on

- Mermaid diagrams are scaled to the note width; a 9-box horizontal chain or a 19-box graph becomes
  unreadable. Top-down diagrams with ≤ 12 boxes stay legible.
- Canvas (`.canvas`, JSON Canvas 1.0) is zoomable. Text cards with `[[wikilinks]]` work wherever
  the folder sits in the vault; file cards would need vault-relative paths.
- Callouts with `-` after the type (`> [!note]- Title`) start folded.
- Inside a Markdown table the alias bar of a wikilink must be escaped: `[[Note\|label]]`.
- Properties (front matter) are filterable with the Bases core plugin and with Dataview.

## Checklist before handing over

- [ ] Start note ≤ 70 lines, verdict first, top 5 by impact, links to every part.
- [ ] Every number on an item note is scoped to that item.
- [ ] No horizontal diagram, none over 12 boxes; big maps on a Canvas.
- [ ] The weakest tier is folded or on a Background note.
- [ ] `Vault.validate()` returns nothing.
