# dotnet-migration-skill

An **analysis-first .NET 8 → .NET 10 migration tool for large monorepos**, packaged as an agent
skill (it also runs as a plain command line). It reads the whole repository, measures it on
.NET 8, tries .NET 10 in a throwaway git worktree, and writes a plan: SDK first, shared libraries
built for both versions while they still have .NET 8 consumers, application groups migrated one
by one in dependency order, then the finish. Every change comes as a **patch for review** or a
**verified rollback**; the tool never commits, pushes, or edits the repository in place.

No Copilot, MCP server or commercial tool is required: Python 3.11+, `pyyaml`, `jsonschema`, git,
and the .NET SDK.

## What it does

| Command | Purpose |
|---|---|
| `dmig doctor <repo>` | Is this machine ready? SDKs and runtimes, package feeds (through `dotnet`, so private feeds and credential providers work), git worktrees (actually created and deleted), disk, long paths, Python. Outcome READY / READY_WITH_LIMITATIONS / BLOCKED. |
| `dmig analyze <repo>` | Inventory, project graph, migration groups, baseline build and test, compatibility rules, a layered .NET 10 experiment, the plan, a Markdown report, a human to-do list, and an Obsidian note set. Changes nothing. |
| `dmig global <repo> --phase …` | Repository-global phases (SDK, retire .NET 8, converge packages, cut-over) as separately approved patches. |
| `dmig migrate <repo> --group <id>` | Migrate one approved group in a disposable worktree: deterministic edits, bounded repairs, a final gate, then a patch or a rollback. |
| `dmig validate <repo> --scope group\|repository` | Re-check a group or the whole repository against the baseline. |
| `dmig rollback <run folder>` | Clean up a run's throwaway workspace and print how to undo its patch. |
| `dmig report <repo>` | Re-render plan, report and notes from saved results, without rebuilding. |

Outputs go to `<repo>.dmig/<step>/` next to the repository. Each run writes a detailed `result.json`
and a compact camelCase `dmig-result.json`.

## Running it

Nothing is installed. Copy the packaged skill folder (`python scripts/package_skill.py` →
`dist/dotnet-8-to-10-migration/`) and use its starter file, `dmig.cmd` (Windows) or `dmig`
(Linux/macOS). From a source checkout: `PYTHONPATH=src python -m dmig <command> <repo>`.

```bash
pip install -r requirements.txt
dmig doctor  path/to/repo
dmig analyze path/to/repo --target net10.0
```

A package mirror can be described with `--package-catalog mirror.json` (format:
`samples/package-catalog.fixture.json`): only approved versions are ever chosen.

## Safety net

1. **Baseline first:** failures in the unchanged code are recorded and never blamed on the migration.
2. **Layered experiment:** when the .NET 10 build stops on a wall (package warnings, then
   obsolete APIs treated as errors), the wall is recorded, only those codes are relaxed in the
   throwaway copy, and it builds again, so every problem is visible up front.
3. **Gate:** no new build errors, no test that passed before fails (a failing test is re-run once;
   flaky and environment-unstable tests are reported, never counted as passed), the diff stays in
   scope, no credential added. Otherwise: rollback, no patch.
4. **Rollback proof:** commit, status and worktrees are compared before and after.

## Tested on a real repository

On [bitwarden/server](https://github.com/bitwarden/server) at its last .NET 8 commit (56
projects, ~10,000 tests), every .NET 10 failure the analysis found (4 build walls and 603 test
regressions, 590 of them from one settings class) matched a change in the maintainers' own
.NET 10 upgrade ([PR #7171](https://github.com/bitwarden/server/pull/7171)).

## Repository layout

| Folder | What it is for |
|---|---|
| `skill/` | the skill's `SKILL.md`, references and starter files |
| `skills/obsidian-report/` | a separate, reusable skill for readable Obsidian note sets |
| `src/dmig/` | the migration tool |
| `src/obsreport/` | notes, size-limited diagrams, Canvas, validator (no .NET knowledge) |
| `recipes/` | the recipe, hand-written rules, and a catalogue generated from Microsoft's breaking-change lists |
| `schemas/` | JSON schemas every output is validated against |
| `samples/` | a synthetic monorepo and a package-mirror fixture used by the tests |
| `tests/` | unit, real-repository lessons, contract and full-lifecycle tests |

## Tests

```bash
python -m pytest tests/test_units.py tests/test_real_repo_lessons.py tests/test_obsreport.py   # fast, no dotnet
DMIG_DOTNET=<dotnet with SDK 10 + runtimes 8> python -m pytest                                 # everything
```

Breaking-change knowledge is derived from Microsoft's public documentation (see
`recipes/sources/README.md`). Status and changes: `CHANGELOG.md`.
