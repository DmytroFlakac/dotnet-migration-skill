# Architecture

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Python 3.11 + the `dotnet` CLI; MSBuild facts via `dotnet msbuild -getProperty/-getItem` | No in-process MSBuild hosting; Python is available in agent sandboxes |
| D2 | Analysis first; implementation one approved group at a time | Large repositories need a plan and evidence before any edit |
| D3 | A preflight runs before everything: READY / READY_WITH_LIMITATIONS / BLOCKED | Unknown environment capabilities become reported facts, not assumptions |
| D4 | Shared libraries build for `net8.0;net10.0` until their last .NET 8 consumer migrates | Consumers move at different times |
| D5 | Every edit is classified PROJECT_LOCAL, GROUP_SHARED or REPOSITORY_GLOBAL; group runs never change repository-global files | `global.json`, `Directory.*.props`, `NuGet.config` and CI affect everyone |
| D6 | SDK first: the unchanged `net8.0` code must build and test on the .NET 10 SDK before any target changes | Separates toolchain failures from framework failures |
| D7 | No commercial tool or coding assistant is required | Portable core; such tools can be optional adapters |
| D8 | Breaking-change knowledge is data (`recipes/rules/*.yaml`) | Rules are reviewed and extended without code changes |
| D9 | Experiments run in disposable git worktrees; rollback is verified | The person's repository is never touched |
| D10 | Package versions come only from a package catalogue (configured feeds, or a mirror fixture with approved versions) | Never invent or force a version the catalogue does not expose |

## Flow

```text
doctor → analyze (inventory → graph → groups → baseline → compatibility → layered experiment → plan,
         report, human to-do, notes)
       → global PHASE_1 (SDK) → migrate each group in order → global PHASE_5/6
       → validate --scope repository (PHASE_7) → global PHASE_8 (cut-over)
```

## Modules (`src/dmig/`)

| Module | Responsibility |
|---|---|
| `cli.py` | commands, default folders, contract result |
| `doctor.py`, `preflight.py` | environment checks |
| `discovery.py`, `graph.py`, `grouping.py` | inventory, project graph, migration groups |
| `buildtest.py` | restore / build / test, diagnostics, TRX parsing, scoped builds |
| `experiment.py` | layered .NET 10 experiment, test comparison, flaky/environment classification |
| `compat.py`, `catalog.py` | rules, package catalogue (public feeds via `dotnet`, or a mirror fixture) |
| `plan.py`, `report.py`, `todo.py`, `obsidian.py` | plan, Markdown report, human to-do, note set |
| `implement.py`, `globalphase.py`, `validate.py` | group runs, global phases, validation, rollback |
| `cache.py`, `edits.py`, `secrets.py`, `contract.py`, `limits.py` | baseline cache, style-preserving edits, credential redaction, camelCase result, size caps |
