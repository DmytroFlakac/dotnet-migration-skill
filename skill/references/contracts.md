# Neutral contracts

An organisation's orchestration adapter maps its own repository, branch, ticket, credentials and
delivery fields onto these. Schemas: `schemas/request.schema.json`, `schemas/response.schema.json`.

## Request (`--request request.json`)

```json
{
  "repository_path": "/absolute/path/to/repository",
  "mode": "analysis",
  "target_framework": "net10.0",
  "selected_group_id": null,
  "allow_reversible_experiments": true,
  "max_repair_attempts": 3,
  "output_directory": "/absolute/path/to/output",
  "network_policy": "public-package-sources-only"
}
```

## Response (`response.json`)

```json
{
  "schema_version": "1.0",
  "status": "READY_FOR_PLAN_WITH_LIMITATIONS",
  "mode": "analysis",
  "source_frameworks": ["net8.0", "net8.0-windows"],
  "target_framework": "net10.0",
  "inventory_path": "inventory.json",
  "baseline_path": "baseline.json",
  "migration_plan_path": "migration-plan.json",
  "human_report_path": "migration-report.md",
  "groups": [{"group_id": "shared-foundation", "order": 1, "readiness": "NEEDS_KNOWN_REPAIR"}],
  "changes_persisted": false,
  "rollback_verified": true,
  "unresolved_items": [],
  "ready_for_human_review": true
}
```

## Implementation request (one per group, in `migration-plan.json`)

```json
{
  "phase": "GROUP_MIGRATION",
  "selected_group_id": "app-appa-web",
  "shared_library_policy": "MULTITARGET_UNTIL_LAST_CONSUMER",
  "allow_repository_global_changes": false,
  "global_plan_id": "plan-…"
}
```

## Environment contract (`--environment contract.json`)

```json
{
  "external_test_services": [
    {"service_id": "orders-db", "available": false, "connection_injected_by_adapter": true,
     "test_data_available": false, "allowed_test_suites": [], "cleanup_required": true}
  ],
  "licensed_tools": []
}
```
