# Changelog

Versions follow semantic versioning. 1.0 is reserved for the end of the production checklist
(full real-repository migration, Linux run, hardening, a real-world pilot).

## Unreleased

### Hardening for locked-down Windows machines
- **Console never crashes on cp1252:** plain ASCII on the console (PASS/WARN/FAIL/BLOCK), stdout and
  stderr replace what they cannot show; files stay UTF-8.
- **No HTTP client in the tool:** the nuget.org HTTP fallback (`compat.py`) and the urllib feed probe
  (`preflight.py`) are gone; package traffic goes only through `dotnet` and the configured feeds.
  `--allow-network` is now `--allow-public-nuget` (old name kept); without a catalogue, versions are
  `METADATA_UNAVAILABLE`.
- **Configured feeds are probed by default** (`--offline` skips it), each reported as OK,
  AUTH_REQUIRED (401), FORBIDDEN (403), NOT_FOUND (404), DNS_FAILURE, TIMEOUT or FEED_UNREACHABLE;
  one failing feed among working ones is a limitation, not a block.
- **.NET 8 in another dotnet folder:** found in the usual places or via `--dotnet-root` /
  `DMIG_DOTNET_ROOTS`, and combined with the active SDK through links (nothing installed or changed).
- **Doctor:** .NET 8 runtime missing is a WARN (a capability), non-git folders get exact steps,
  path-length risk, workloads, clean working tree, shared/global files, Windows-only projects;
  `environment-capabilities.json` names the capabilities.
- **git hooks off** for every git command (Git for Windows runs hooks through `sh`).
- **Scope:** WPF and WinForms are report-only; `net*-windows` without Windows is `BLOCKED_PLATFORM`
  (was `REQUIRES_WINDOWS`).
- **Adapters renamed** to the fixed names (`IPackageCatalog`, `ISourceControl`, `IWorkItemAdapter`,
  `ISecretProvider`, `ICiExecutor`, `ITelemetry`, `IDelivery`); the mock catalogue is
  `fixtures/package-catalog.json`; earlier names remain as aliases.
- **Result:** `PARTIAL`, `groups`, `changes`, `repairs`, `stopReason`, `missingCapabilities`,
  `unvalidated`, `rollbackVerified`; every run writes `migration-report.md` (and `migration.patch`).
  The camelCase request is accepted.
- **A run that stops before any repair records why** (`repair_attempts` entry with
  `stop_for_human_review`), instead of an empty list.
- **ASP.NET Core packages pinned in a project** (e.g. `Microsoft.AspNetCore.Mvc.Testing`) move to 10.x
  with the project (split by target framework while it keeps net8.0). Found on a real repository.
- **Sample:** `NuGet.config` no longer hard-codes nuget.org; default outputs for repositories inside
  the tool folder go to the temp folder.
- **Tests:** capability-based skips naming what is missing; feed tests only with
  `DMIG_TEST_NETWORK=1` or a local `DMIG_NUGET_SOURCE`; `samples/` guarded against changes;
  strict compile test (warnings as errors); `requirements-dev.txt` with pinned pytest.

### Changed (behaviour)
- **No network by default.** The tool makes no network call of its own unless `--allow-network`;
  the default package catalogue is a local JSON file (`LocalCatalog`), the public one is used only
  with `--allow-network` and never as a fallback; a package or version missing from the catalogue
  is `PACKAGE_UNAVAILABLE`. Preflight lists feeds without contacting them. The whole test suite
  runs with Python sockets refused.
- **Framework add-ons off by default.** Default scope: class libraries, console apps, worker
  services, test projects. `--adapters aspnet_core,efcore,wpf,winforms,windows_service` enables the
  others; projects of a disabled type are listed as `OUT_OF_SCOPE_ADAPTER_DISABLED` (legacy, MAUI,
  Xamarin, Azure Functions: `OUT_OF_SCOPE_REPORT_ONLY`) and never migrated.
- Doctor labels each check PASS / WARN / BLOCK; a missing .NET 8 runtime is BLOCK; Windows
  availability reported; disk estimated from the repository size. Validation of a Windows-only
  group on another OS is `REQUIRES_WINDOWS`.
- Projects still used by .NET 8-only projects outside their group keep `net8.0` next to `net10.0`.

### Added
- `src/dmig/adapters/`: PackageCatalog, SourceControl, WorkItems, Secrets, Telemetry, Runner, each
  with a mock and a test; telemetry written through the adapter (`telemetry.jsonl`).
- `docs/integration-contract.md` (inputs, outputs, statuses, exit codes, responsibilities,
  acceptance tests); `THIRD_PARTY_NOTICES.md`; transitive dependencies in `requirements.lock`.
- Human to-do list (`human-todo.md/.json`, Obsidian note "06 Human to-do").
- Tests: every `.py` compiles; offline guard; scope; adapters.
- **Package catalogue interface**: `PackageCatalog` with `list_versions`,
  `is_available`, `is_approved`, `get_compatibility`, `get_dependencies`, `get_metadata`;
  `PublicNuGetCatalog` (configured feeds via `dotnet`) and `FixturePackageCatalog` (JSON mirror with
  approved versions, `fixtures/package-catalog.json`). Analysis strategies, PHASE_6, the
  PHASE_1 MSBuild SDK pin repair and the NU1605 group repair choose only approved versions.
  New findings `PKG-NOT-IN-CATALOGUE` and `PKG-VERSION-NOT-APPROVED`. `--package-catalog` /
  `DMIG_PACKAGE_CATALOG`; implementation and global phases reuse the analysis's choice.
- **Contract surface** (§8, §19): `dmig <command> <repository>`; outputs default to `<repo>.dmig/<step>/`;
  `migrate --group --target`, `validate --scope group|repository`, `rollback <run>`; `dmig-result.json`
  (camelCase contract result, schema) next to every `result.json`; `project-graph.json` and
  `migration-groups.json` from the analysis; doctor outcome READY / READY_WITH_LIMITATIONS / BLOCKED in
  `environment-capabilities.json` + `doctor-report.md`, with repository size, package cache and a real
  create/delete of a disposable workspace.
- `dmig doctor`; feeds probed through the SDK; NU1605 and multi-target NU1510 group repairs.

### Fixed
- PHASE_1 repair records now match the result schema.
- Patches use the repository's own line-ending settings; edits keep each file's style.

## 0.9.0 (13 Nov 2026), learned from a real monorepo

Tested on bitwarden/server (56 projects, about 10,000 tests) at the last .NET 8 commit, compared with
the engineers' real .NET 10 upgrade (PR #7171): every failure the experiment found matched a change
in that PR.

### Added
- **Layered experiment**: when the build stops on a wall (package warnings-as-errors, then
  obsolete-API warnings-as-errors), the wall is recorded, only those codes are relaxed in the
  throwaway copy, and the build runs again, up to 4 layers.
- **NuGet advisory drift**: a baseline that fails only on NU1901–NU1904 is re-measured with the
  audit off; implementation and global phases keep that decision; the packages become a decision.
- **PHASE_1 repairs MSBuild SDK pins** (MSB4019 inside a pinned SDK package → newest stable).
- **Rules**: SRC-CONFIG-NULL-SETTER, PKG-DOWNGRADE-PIN (NU1605), SDK-MSBUILD-SDK-TOO-OLD (MSB4019),
  PKG-PRUNED-ANY (NU1510), SDK-NUGET-AUDIT-TRANSITIVE. Rules may name the error a regression shows
  (`regression_message`).
- **`dmig report`**: re-render plan, report and notes from saved results, no builds.
- **`obsreport` package and `obsidian-report` skill**: the redesigned Obsidian export (start note,
  evidence tiers, vertical diagrams ≤ 12 boxes, Canvas map, decision records, reviewer FAQ, validator).
- **Secrets**: command output and logs are redacted; a patch that would add a credential is refused.
- Preflight checks the ASP.NET Core shared framework per major; test hosts that cannot start are
  reported as environment-blocked.
- **Flaky tests**: regressed tests are re-run once in the gate; those passing on retry are reported
  as flaky (not blocking); failures that look like the environment (timeouts, deadlocks) are marked.
- **Time budget**: `--max-minutes` on analyze/implement/global; on expiry the step stops safely,
  says so in its result and exits with code 4 (git cleanup always runs).
- **CI for the tool**: `.gitlab-ci.yml` (fast tests, full suite on Linux with SDK 10 + runtimes 8,
  packaging).
- `--version`; requirements.txt (ranges) and requirements.lock (tested versions).

### Fixed
- Tests compared by method name (data-driven tests print random arguments).
- A global phase whose own baseline cannot run (SDK not installed) compared tests with nothing and
  passed vacuously; it now compares with the analysis baseline and says so.
- SDK-stage errors in an SDK-10 baseline were treated as pre-existing and hidden; they are now
  confirmed SDK-stage problems.
- Rule globs accept `{a,b}`.

## 0.8.0 (12 Nov 2026)

Analysis, implementation and global phases; baseline cache and scoped builds; Obsidian export;
breaking-change catalogue (255 rules); rule drafts; synthetic monorepo sample; first Bitwarden
analysis without builds.
