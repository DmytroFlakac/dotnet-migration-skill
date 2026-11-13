# Scope and safety rules

These come from the migration design documents.

| Rule | Why |
|---|---|
| Analysis mode never edits, commits, pushes, or creates a pull request or work item. | The person needs an untouched repository and a plan to review; delivery belongs to the organisation's adapter after human approval. |
| Experiments run only in a disposable git worktree outside the repository, which is removed afterwards; the original state is compared before and after. | Proves the analysis left nothing behind (`rollback_verified`). |
| The output folder must be outside the repository. | Reports and worktrees must never become accidental changes. |
| Repository-global files (`global.json`, root `Directory.Build.props/.targets`, `Directory.Packages.props`, `NuGet.config`, CI SDK/runner config) are changed only in an approved repository-global phase. | They affect every group; a group run returns a *proposed* global change instead. |
| Shared libraries stay multi-targeted until their last .NET 8 consumer migrates. | A net10.0-only library cannot be referenced by net8.0 projects. |
| SDK first: SDK 10 must build the unchanged net8.0 repository before any target-framework change. | Separates SDK/toolchain failures from migration failures. |
| Baseline failures are recorded, never repaired, never absorbed into migration scope. | Otherwise migration results cannot be trusted. |
| Unavailable integration tests are `NOT_EXECUTED_ENVIRONMENT_BLOCKED`, never passed. | A skipped test is not evidence. |
| No credentials in source-controlled files, URLs or logs; no silent fallback to an unapproved feed. | Credentials come from the environment's adapter only. |
| Commercial tools and coding agents (Copilot, Moderne) are optional adapters, never required. | Nothing is confirmed licensed in the target environment. |
| Out of scope: vulnerability remediation, general dependency modernisation, unrelated refactoring, architecture changes. | The migration must stay reviewable and reversible. |
| Implementation mode (later): one approved group, at most three narrow repairs, mandatory final gate, rollback when it fails, never edit a test to make it pass. | Bounded, deterministic-first changes that a human can review. |
