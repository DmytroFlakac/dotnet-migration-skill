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
python -m pip install -r requirements.txt     # tests: also requirements-dev.txt
dmig doctor  path/to/repo
dmig analyze path/to/repo --target net10.0
```

**Defaults are strict:**

- **No network client of its own.** Everything about packages goes through `dotnet` and the feeds
  configured on the machine; those feeds are checked by default (`--offline` skips it). The public
  nuget.org catalogue is used only with `--allow-public-nuget`. Package versions come from a local
  catalogue file, `--package-catalog mirror.json` (format: `fixtures/package-catalog.json`): only
  approved versions are chosen; a missing one is `PACKAGE_UNAVAILABLE`, and without a catalogue
  versions are `METADATA_UNAVAILABLE`, never guessed or fetched elsewhere.
- **Framework add-ons are off.** Default scope: class libraries, console apps, worker services and
  test projects. `--adapters aspnet_core,efcore,windows_service` enables the others. .NET
  Framework, WPF, WinForms, MAUI, Xamarin and Azure Functions are report-only. Out-of-scope
  projects are listed (`OUT_OF_SCOPE_ADAPTER_DISABLED` / `OUT_OF_SCOPE_REPORT_ONLY`), never
  silently dropped.
- **Locked-down Windows friendly.** Plain-ASCII console output, no `sh`/`bash`/`sed`, git hooks
  off, always `python -m ...`. A .NET 8 runtime installed in another dotnet folder is found (or
  given with `--dotnet-root`) and combined with the SDK without changing any installation.

Integration points are interfaces with mocks in `src/dmig/adapters/` (`IPackageCatalog`,
`ISourceControl`, `IWorkItemAdapter`, `ISecretProvider`, `ICiExecutor`, `ITelemetry`, `IDelivery`);
inputs, outputs, every status and exit code are in
[`docs/integration-contract.md`](docs/integration-contract.md).

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
| `samples/` | a synthetic monorepo used by the tests (copied, never changed) |
| `fixtures/` | the mock package catalogue (`package-catalog.json`) |
| `tests/` | unit, real-repository lessons, contract, offline, adapter and full-lifecycle tests (run with networking disabled) |

## Tests

```bash
python -m pytest                          # offline: tests that need a feed are skipped, each with its reason
DMIG_TEST_NETWORK=1 python -m pytest      # also the tests that restore packages from the configured feeds
```

Tests that need something the machine lacks (SDK 10, a .NET 8 runtime, a reachable feed, git
worktrees) are skipped, and the skip names it. `DMIG_NUGET_SOURCE` points the sample at one feed
(a URL or a local folder); `DMIG_DOTNET_ROOTS` names extra dotnet folders.

Breaking-change knowledge is derived from Microsoft's public documentation; attributions and
dependency licences are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Changes: `CHANGELOG.md`.
