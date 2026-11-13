# Integration contract

This is where `dmig` connects to an orchestration pipeline. The pipeline prepares the machine,
calls `dmig`, reads its outputs, and does everything that touches shared systems (branches,
commits, merge requests, tickets, secrets). `dmig` never does those.

## 1. Who does what

| Concern | Orchestrator | `dmig` |
|---|---|---|
| Machine: SDK 10, runtimes 8 + 10, git, Python 3.11+ | provides | checks (`dmig doctor`); combines a .NET 8 runtime found in another dotnet folder with the SDK |
| Package feeds and their credentials | configures (`NuGet.config`, credential provider) | uses them through `dotnet` only; never stores, logs or embeds credentials |
| Package catalogue (which versions exist / are approved) | supplies a catalogue file or an adapter | decides versions only from it |
| Network | decides | **no network client of its own**: package traffic goes through `dotnet` and the configured feeds (probed by default, `--offline` skips it); the public catalogue only with `--allow-public-nuget` |
| Repository checkout | provides a clean clone | works in disposable git worktrees; never edits the clone |
| Approval of a step | a person | refuses repository-global phases without `--approve-global` |
| Branch, commit, push, merge request | does it, from the returned patch (`IDelivery`) | **never** |
| Ticket / work item | creates or links (`IWorkItemAdapter`) | **never**; the id may be passed through for naming |
| Test services (databases, queues) | provides and injects settings | runs the tests; unavailable ones are reported, never passed |
| Windows build agents | routes `BLOCKED_PLATFORM` groups to them | reports them; does not dispatch |

## 2. Adapters (names fixed)

The core depends only on these interfaces; the mocks are chosen in `src/dmig/adapters/__init__.py`
(`defaults()`) and by the command line, the only places that name a concrete adapter. Earlier names
remain as aliases.

| Interface | Responsibility | Mock |
|---|---|---|
| `IPackageCatalog` | versions, availability, TFM compatibility | `MockEnterprisePackageCatalog` reading `fixtures/package-catalog.json` |
| `ISourceControl` | worktree, diff, patch | `LocalGitSourceControl` (local git, hooks off) |
| `IWorkItemAdapter` | ticket id | `MockWorkItemAdapter` returns `TEST-123` |
| `ISecretProvider` | feed credentials | `EnvSecretProvider` (env vars, dummy values) |
| `ICiExecutor` | where build/test run | `LocalProcessExecutor` (local process) |
| `ITelemetry` | events | `JsonlTelemetry` (`<out>/telemetry.jsonl`, redacted) |
| `IDelivery` | branch / merge request | `LocalFileDelivery` writes `migration.patch` + `migration-report.md` only |

Catalogue format (never propose a version that is not in `availableVersions`):

```json
{ "sources": ["enterprise-mirror"],
  "packages": { "Example.Logging": { "availableVersions": ["8.0.2", "8.0.4", "10.0.0"],
                                     "approvedVersions": ["8.0.4", "10.0.0"] } } }
```

## 3. Calling it

Always `python -m dmig ...` (with `PYTHONPATH=src` from a source checkout, or the starter files
`dmig.cmd` / `dmig` from the packaged skill). Nothing needs `sh`, `bash` or `sed`.

```text
dmig doctor   <repo> [--package-catalog FILE] [--offline] [--dotnet-root DIR]
dmig analyze  <repo> [--target net10.0] [--package-catalog FILE] [--adapters LIST] [--offline]
                     [--allow-public-nuget] [--request request.json] [--max-minutes N]
dmig global   <repo> --phase PHASE_1_SDK_GLOBAL|PHASE_5_RETIRE_NET8|PHASE_6_CONVERGE|PHASE_8_CUTOVER
                     --approve-global [--max-minutes N]
dmig migrate  <repo> --group ID [--target net10.0] [--allow-newer-commit] [--max-minutes N]
dmig validate <repo> --scope group --group ID | --scope repository
dmig rollback <run folder>
```

Every command also takes `--dotnet <exe>` and `--dotnet-root <folder>` (repeatable; also
`DMIG_DOTNET_ROOTS`): when the active dotnet lacks a runtime (typically .NET 8) that another
installation has, a combined folder links them; nothing is installed or changed.

Outputs go to `<repo>.dmig/<step>/` unless `--out` is given (never inside the repository; for a
repository inside the tool's own folder, such as the samples, the temp folder is used instead).
`--plan` defaults to `<repo>.dmig/analysis/migration-plan.json`. Later steps reuse the analysis's
package catalogue and add-on choices.

## 4. Input: `request.json` (optional; command-line flags set the same fields)

Schema: `schemas/request.schema.json`. The camelCase names are the contract's; the older snake_case
names are still accepted.

```json
{ "repository": "path", "mode": "analysis|implementation|global|validate|rollback",
  "targetFramework": "net10.0", "group": "optional-id", "maxRepairs": 3,
  "outputDir": "path", "allowPublicNuget": false }
```

| Field (older name) | Meaning | Default |
|---|---|---|
| `repository` (`repository_path`) | repository root | required |
| `mode` | `analysis`, `implementation`, `global`, `validate`, `rollback` | required |
| `targetFramework` (`target_framework`) | `net10.0` | `net10.0` |
| `group` (`selected_group_id`) | the group to migrate or validate | — |
| `maxRepairs` (`max_repair_attempts`) | repair rounds per group run, **at most 3** | 3 |
| `outputDir` (`output_directory`) | where outputs go | `<repo>.dmig/<step>/` |
| `allowPublicNuget` (`allow_public_nuget`, `allow_network`) | the public catalogue may be used | `false` |
| `package_catalog` | path to a local catalogue JSON, or `public` (needs `allowPublicNuget`) | none: versions unknown |
| `adapters` | framework add-ons: `aspnet_core`, `efcore`, `windows_service` | none |
| `probe_feeds` | probe the configured feeds (`--offline` = false) | `true` |
| `allow_reversible_experiments` | run the .NET 10 experiment in a worktree | `true` |
| `allow_repository_global_changes` | group runs may change global files | `false` |
| `time_budget_minutes` | stop safely after this long (`--max-minutes`) | none |

## 5. Output

Every step writes into its output folder:

| File | For | Content |
|---|---|---|
| `dmig-result.json` | migrate / global / validate | the contract's result (below); schema `schemas/dmig-result.schema.json` |
| `migration.patch` | migrate / global, on success only | the change, for review; apply with `git apply` |
| `migration-report.md` | migrate / global / validate | short report: status, stop reason, open items |
| `result.json` | migrate / global / validate | detailed result; schema `schemas/result.schema.json` |
| `<group or phase>.patch` | migrate / global, on success only | the same patch under its group or phase name |
| `response.json`, `migration-report.md`, `migration-plan.json`, `inventory.json`, `project-graph.json`, `migration-groups.json`, `baseline.json`, `experiment.json`, `compatibility.json`, `human-todo.md/.json`, `obsidian/` | analyze | the analysis |
| `doctor-report.md`, `environment-capabilities.json` | doctor | readiness and named capabilities |
| `rollback.json` | rollback | what was cleaned |
| `telemetry.jsonl` | every command | start/finish events (redacted) |
| `logs/` | every command | each command's full output (redacted) |

The result:

```json
{ "status": "SUCCEEDED|PARTIAL|BLOCKED|FAILED|ROLLED_BACK", "projectsDiscovered": 0,
  "groups": [], "changes": [], "baseline": {}, "finalValidation": {},
  "repairs": [], "stopReason": "", "missingCapabilities": [],
  "unvalidated": [], "manualActions": [], "rollbackVerified": true,
  "artifacts": { "patch": "migration.patch", "report": "migration-report.md" } }
```

- `PARTIAL`: the gate passed, but part of it could not be checked here (`unvalidated` lists what).
- `stopReason`: one sentence on why a run stopped; a missing machine prerequisite comes first.
- `missingCapabilities`: e.g. `runtime8`, `reachable configured feed`, when they explain a failure.
- `repairs`: every repair round; a run that stopped before any repair has one entry with
  `attempt: 0`, `next_action: stop_for_human_review` and `stop_reason`.
- `rollbackVerified` is `true` only when the repository's commit, tracked status and worktree list
  are the same after the run as before it. Every run leaves the repository untouched; only the
  patch carries the change.

Doctor's `environment-capabilities.json` names the capabilities: `sdk10`, `runtime8`,
`aspnetcore_runtime8`, `reachable_configured_feed`, `git_repository`, `git_worktree`, `windows`
(and `missing_capabilities`).

### Status values

**analyze** (`response.json` → `status`):

| Status | Meaning |
|---|---|
| `READY_FOR_PLAN` | complete analysis, plan ready |
| `READY_FOR_PLAN_WITH_LIMITATIONS` | plan ready; some evidence is missing (see `unresolved_items`, e.g. no baseline, tests not runnable) |
| `BLOCKED` | the analysis could not run (e.g. no SDK 10, no reachable feed) |

**migrate / global / validate** (`result.json` → `status`; `dmig-result.json` maps it):

| Status | Meaning | `dmig-result` |
|---|---|---|
| `SUCCESS` | gate passed; a patch is ready for review | `SUCCEEDED` (or `PARTIAL`) |
| `ROLLED_BACK` | the gate failed; nothing kept, no patch; `stopReason` says why | `ROLLED_BACK` |
| `REJECTED` | refused before starting (unknown group, plan for another commit, worktree failure) | `BLOCKED` |
| `BLOCKED_DEPENDENCY_ORDER` | a group it depends on is not migrated yet | `BLOCKED` |
| `BLOCKED_GLOBAL_PHASE_REQUIRED` | the SDK phase must run first | `BLOCKED` |
| `BLOCKED_NOT_READY` | a global phase's precondition is not met (e.g. .NET 8 consumers remain) | `BLOCKED` |
| `SKIPPED_NOT_APPLICABLE` | nothing to do for this group, or the group is out of scope | `BLOCKED` |
| `NOTHING_TO_DO` | a global phase found nothing to change | `SUCCEEDED` |
| `VALIDATION_PASSED` / `VALIDATION_FAILED` | validation result | `SUCCEEDED` / `FAILED` |
| `BLOCKED_PLATFORM` | the group needs Windows and this machine has none; analysed, not validated | `BLOCKED` |

**Group readiness** in the plan: `READY`, `NEEDS_KNOWN_REPAIR`, `WAITS_FOR_DEPENDENCY_REPAIR`,
`NEEDS_HUMAN_REVIEW`, `BLOCKED_PLATFORM`, `BLOCKED_NEEDS_INVESTIGATION`,
`NOT_VERIFIED_NO_EXPERIMENT`, `NOT_VERIFIED_NO_BASELINE`, `OUT_OF_SCOPE_ADAPTER_DISABLED`,
`OUT_OF_SCOPE_REPORT_ONLY`.

**Scope:** supported by default: SDK-style C# class libraries, console apps, worker services, test
projects. Optional (off): ASP.NET Core, EF Core (and Windows services). Report only: .NET Framework,
WPF, WinForms, MAUI, Xamarin, Azure Functions.

**Tests** are never counted as passed unless they ran and passed. Tests that could not run
because of the environment are `NOT_EXECUTED_ENVIRONMENT_BLOCKED`; tests that failed twice with
timeouts or deadlocks are listed as environment-unstable; tests that failed once and passed on a
re-run are listed as flaky.

**Packages**: a package or version missing from the catalogue is `PACKAGE_UNAVAILABLE`; without a
catalogue (and without `--allow-public-nuget`) versions are `METADATA_UNAVAILABLE`; no other source
is tried.

## 6. Exit codes

| Code | Meaning |
|---|---|
| 0 | the step completed as intended (`SUCCESS`, `VALIDATION_PASSED`, `NOTHING_TO_DO`, analysis complete, doctor READY / READY_WITH_LIMITATIONS, rollback verified) |
| 1 | doctor: BLOCKED (fix the BLOCK items first) |
| 2 | analysis or preflight: BLOCKED |
| 3 | migrate / global / validate: any other status (see `result.json`); rollback not verified |
| 4 | the time budget (`--max-minutes`) ran out; the step stopped safely |

## 7. Guarantees

- No commit, push, branch, merge request or ticket operation, ever.
- No edit of the repository's working tree; changes come only as patches.
- No repository-global file (`global.json`, root `Directory.Build.props/targets`,
  `Directory.Packages.props`, `NuGet.config`, CI) changes in a group run; only in an approved global phase.
- Repairs never edit tests to make them pass; at most 3 repair rounds per run.
- No credentials in logs, results or patches (redacted; a patch that would add one is refused).
- No HTTP client in the tool; package traffic only through `dotnet` and the configured feeds.
- Console output is plain ASCII; files are written as UTF-8. git runs with hooks off.

## 8. Acceptance tests

Run with `python -m pytest`. Tests that need a capability the machine lacks are skipped, and the
reason names it. Feed-dependent tests run only with `DMIG_TEST_NETWORK=1`, or with
`DMIG_NUGET_SOURCE` pointing at a local feed folder. Tests never touch `samples/`.

| Requirement | Proven by |
|---|---|
| Every `.py` file compiles, also with warnings as errors (Python 3.12) | `tests/test_compile.py` |
| The full test run passes with Python networking disabled | `tests/conftest.py` (`no_network`, applies to every test) |
| No HTTP client in the tool | `tests/test_offline.py::test_the_tool_has_no_http_client_of_its_own` |
| `doctor` and `analyze` touch only the configured feeds; `--offline` none | `tests/test_offline.py::test_doctor_and_analysis_make_no_network_call` |
| The shared library ends up `net8.0;net10.0`, and only a later step removes `net8.0` | `tests/test_implementation.py::test_groups_migrate_in_order_and_patches_apply` |
| Global files change only in a global phase | same test; `tests/test_global.py::test_global_phase_needs_explicit_approval` |
| An injected unknown failure stops the run and `rollback_verified` is true | `tests/test_implementation.py::test_unrepairable_failure_rolls_back_without_a_patch` |
| A package missing from the local catalogue is `PACKAGE_UNAVAILABLE`, with no public fallback | `tests/test_offline.py::test_missing_package_has_no_public_fallback`, `tests/test_catalog_interface.py` |
| Each integration interface has a mock; the core imports none | `tests/test_adapters.py` |
| Framework add-ons are off by default; WPF/WinForms report-only; out-of-scope projects are listed | `tests/test_scope.py` |
| A .NET 8 runtime in another dotnet folder is used | `tests/test_dotnet_roots.py` |
| ASP.NET Core test-host packages move with the project to 10.x | `tests/test_real_repo_lessons.py::test_aspnetcore_test_host_moves_with_the_project_to_net10` |
| `samples/` is never changed by a test | `tests/conftest.py::samples_stay_untouched` |
