# Reading the analysis results

## Statuses

| Where | Value | Meaning |
|---|---|---|
| `response.status` | `READY_FOR_PLAN` | Preflight READY, baseline and experiment ran, no blockers. |
| | `READY_FOR_PLAN_WITH_LIMITATIONS` | The plan is usable, but something was not verified (see `unresolved_items`). Most real runs land here. |
| | `BLOCKED` | The environment cannot even evaluate the projects (no SDK 10, unsatisfiable `global.json`). |
| `preflight.status` | `READY` / `READY_WITH_LIMITATIONS` / `BLOCKED` | Environment only. BLOCKED here means "stop before any edit", not necessarily "no analysis". |

## Group readiness (`migration-plan.json → groups[].readiness`)

| Readiness | Meaning | What to tell the person |
|---|---|---|
| `READY` | Built and tested on net10.0 in the experiment with no new errors or regressions. | Can go into implementation in its turn. |
| `NEEDS_KNOWN_REPAIR` | Its own projects failed on net10.0, and every failure matches a recipe rule with a defined repair. | Name the rule and the repair; implementation mode can apply it. |
| `WAITS_FOR_DEPENDENCY_REPAIR` | Its tests regressed only because of a problem in a group it depends on (usually shared-foundation). | Nothing to fix inside this group; it is unblocked when the dependency is repaired. |
| `NEEDS_HUMAN_REVIEW` | A rule matched whose repair is a design decision (hosting model, serializer…). | List the decisions needed. |
| `VALIDATION_REQUIRES_WINDOWS` | Windows-only projects could not be validated on this host. | Needs a Windows runner. |
| `BLOCKED_NEEDS_INVESTIGATION` | net10.0 errors or regressions that no rule explains. | Show the diagnostics; a person must investigate (and a new rule may be worth adding). |
| `NOT_VERIFIED_NO_EXPERIMENT` / `NOT_VERIFIED_NO_BASELINE` | The experiment or baseline did not run or did not restore. | Nothing about this group is verified. |

## Group kinds and order

- `SHARED_FOUNDATION`: libraries used by two or more roots. Always first; they become
  `net8.0;net10.0` (`MULTITARGET_UNTIL_LAST_CONSUMER`) and drop `net8.0` only when every consumer
  group listed in `retirement_condition_for_net8` has migrated.
- `APPLICATION`: an executable or web project with the libraries only it uses, plus its tests.
- `STANDALONE_LIBRARY`: a library nothing else consumes (tools, packages).
- Tests join the group of the production code they reference — never by name alone.

## Phases (`migration-plan.json → phases`)

`PHASE_0_ANALYSIS` → `1_SDK_GLOBAL` (global.json, CI SDK) → `2_SDK_BASELINE` (unchanged net8.0 repo
builds with SDK 10) → `3_MULTITARGET_SHARED` → `4_GROUP_MIGRATION` (groups in order) →
`5_RETIRE_NET8` → `6_CONVERGE` (central package versions, shared build files) →
`7_REPO_VALIDATION` → `8_CUTOVER`. Phases 1, 2, 6, 7, 8 are `REPOSITORY_GLOBAL`.

## Findings (`compatibility.json`)

| `effect` | Meaning |
|---|---|
| `build_break` | Fails to compile on net10.0. |
| `silent_behavior_change` | Compiles (maybe with a warning) but behaves differently; only tests catch it. The C# 14 `field` keyword is the classic case. |
| `runtime_failure` / `runtime_failure_possible` | Throws or may throw at run time. |
| `obsolete_warning` | Builds with warnings; breaks builds that treat warnings as errors. |
| `version_alignment` | Framework-versioned package (Microsoft.Extensions.*, ASP.NET Core, EF Core) to align in PHASE_6 using the central package strategy shown. |
| `ci_break` / `deployment_change` | CI images or container base images to move (global or per project). |
| `sdk_phase_required` | `global.json` must move to SDK 10 in PHASE_1. |
| `none_known` | `PKG-NOT-PROVEN`: no known issue, not proven either — unless the experiment upgraded it to `PKG-CONFIRMED-BY-EXPERIMENT`. |

Central package strategies: `ONE_VERSION_BOTH_TFMS` (one version works for net8.0 and net10.0),
`CONDITIONAL_PER_TFM` (versions conditioned on `$(TargetFramework)`), `DEFER_AS_CROSS_GROUP_BLOCKER`.

## Experiment

`experiment.json` shows exactly which files were changed in the throwaway worktree (only
TargetFramework values, and `global.json` only if the SDK required it), the build/test results,
`test_comparison` (regressions vs baseline failures still failing) and `rollback_verified`, which
compares commit, status hash, submodules and worktree list before and after.

## Implementation results (`<out>/result.json`)

| `status` | Meaning |
|---|---|
| `SUCCESS` | Every final-gate check passed; `<group_id>.patch` is ready for human review. |
| `ROLLED_BACK` | A gate check failed or a failure had no recipe rule; the worktree was discarded and no patch exists. `repair_attempts[-1].unknown_failures` and `unresolved_items` say why. |
| `BLOCKED_DEPENDENCY_ORDER` | A group this one depends on has not gained a net10.0 target yet. |
| `BLOCKED_GLOBAL_PHASE_REQUIRED` | The repository's SDK is not .NET 10; PHASE_1 must run first. |
| `REJECTED` | Unknown group, plan for another commit (without `--allow-newer-commit`), or a planned project disappeared. |
| `SKIPPED_NOT_APPLICABLE` | No project in the group targets net8.0. |

Final gate checks (all must pass): restore succeeds; build succeeds with no new errors; no
migration-caused test failures (compared per TFM with the group baseline); baseline failures kept
separate; no planned project silently excluded; diff only inside the group's project folders; no
repository-global file changed; repair budget respected; dependent tests ran; and, for shared
libraries, every temporary TFM compiles.

`repair_attempts[]`: each attempt lists the failures it saw, the recipe rules it applied, the files
it changed and whether validation then passed. At most three attempts; an unknown failure stops
immediately (`stop_for_human_review`) instead of guessing.

## Global-phase results (`dmig global`, `<out>/result.json`)

Same file shape as implementation results, with `mode: "global_phase"`, `phase`, `group_id: null`
and `manual_follow_ups`. Gate checks: restore succeeds; build succeeds with no new errors; no new
test failures; every test still runs; the diff touches only this phase's files; changed CI YAML
parses (custom GitLab tags such as `!reference` are accepted). PHASE_7 has no gate and no patch:
`validation` compares the current repository with the original analysis baseline (regressions,
pre-existing failures still failing, new errors, projects still on net8.0).
