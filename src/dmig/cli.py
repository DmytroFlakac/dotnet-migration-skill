"""Command line: python -m dmig <doctor|analyze|migrate|validate|rollback|global|report|preflight> <repository> [options]."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dotnet import Runner


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _note_budget(runner: Runner, result_file: Path, result: dict) -> int | None:
    """When the time budget ran out, say so in the result (the step has already failed safely)."""
    if not runner.budget_exhausted:
        return None
    note = "time budget (--max-minutes) exhausted: the step stopped early and changed nothing; rerun with more time"
    result.setdefault("unresolved_items", []).insert(0, note)
    result["ready_for_human_review"] = False
    if result_file.exists():
        _write(result_file, result)
    print(note, file=sys.stderr)
    return 4


def _catalog_spec(args, analysis_dir: Path | None = None) -> str | None:
    """--package-catalog, else $DMIG_PACKAGE_CATALOG, else the catalogue the analysis used."""
    import os
    spec = getattr(args, "package_catalog", None) or os.environ.get("DMIG_PACKAGE_CATALOG")
    if spec:
        return str(Path(spec).resolve()) if spec != "public" else spec
    resp = (analysis_dir / "response.json") if analysis_dir else None
    if resp and resp.exists():
        return (json.loads(resp.read_text(encoding="utf-8")).get("package_catalog") or {}).get("spec")
    return None


def _runner(dotnet: str | None, logs: Path, preflight: dict | None) -> Runner:
    """A runner that keeps the analysis's measurement decisions: when the analysis found that
    advisories published after the commit break even the unchanged code (BASELINE_NUGET_AUDIT_DRIFT),
    implementation and global phases measure with the audit off too, or every run would stop at
    restore for a reason that is not the migration's."""
    runner = Runner(dotnet, logs)
    if any(l.get("code") == "BASELINE_NUGET_AUDIT_DRIFT" for l in (preflight or {}).get("limitations", [])):
        runner.env["NuGetAudit"] = "false"
    return runner


def cmd_preflight(args) -> int:
    from .preflight import run_preflight
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    runner = Runner(args.dotnet, out / "logs")
    result = run_preflight(repo, out, runner, probe_network=not args.offline,
                           environment_contract=_load_json(args.environment))
    _write(out / "preflight.json", result)
    print(json.dumps({"status": result["status"], "preflight": str(out / "preflight.json"),
                      "blocking": [b["code"] for b in result["blocking_items"]],
                      "limitations": [l["code"] for l in result["limitations"]]}, indent=2))
    return 2 if result["status"] == "BLOCKED" else 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    report = run_doctor(repo, out, Runner(args.dotnet, out / "logs"))
    print((out / "doctor-report.md").read_text(encoding="utf-8"))
    return 0 if report["ready"] else 1


def cmd_analyze(args) -> int:
    from .analysis import run_analysis
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    request = _load_json(args.request) or {}
    request.setdefault("repository_path", str(repo))
    request.setdefault("mode", "analysis")
    request.setdefault("target_framework", "net10.0")
    request.setdefault("output_directory", str(out))
    request.setdefault("allow_reversible_experiments", not args.no_experiment)
    request["run_baseline"] = not args.no_baseline
    if args.obsidian_dir:
        request["obsidian_dir"] = str(Path(args.obsidian_dir).resolve())
    request["probe_network"] = not args.offline
    request["package_catalog"] = _catalog_spec(args)
    runner = Runner(args.dotnet, out / "logs")
    runner.set_budget(args.max_minutes)
    response = run_analysis(repo, out, runner, request, environment_contract=_load_json(args.environment))
    budget = _note_budget(runner, out / "response.json", response)
    print(json.dumps({k: response[k] for k in ("status", "human_report_path", "migration_plan_path",
                                               "changes_persisted", "rollback_verified")}, indent=2))
    return budget or (2 if response["status"] == "BLOCKED" else 0)


def cmd_report(args) -> int:
    from .analysis import rerender
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    request = {"probe_network": not args.offline, "package_catalog": _catalog_spec(args, Path(args.out).resolve())}
    if args.obsidian_dir:
        request["obsidian_dir"] = str(Path(args.obsidian_dir).resolve())
    response = rerender(repo, out, request)
    print(json.dumps({k: response.get(k) for k in ("status", "human_report_path", "obsidian_path")}, indent=2))
    return 0


def cmd_implement(args) -> int:
    from .analysis import RECIPES
    from .implement import run_implementation
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    preflight_path = plan_path.parent / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else None
    group = next((g for g in plan["groups"] if g["group_id"] == args.group), None)
    request = _load_json(args.request) or (dict(group["implementation_request"]) if group else {})
    request.setdefault("selected_group_id", args.group)
    request["selected_group_id"] = args.group
    request["max_repair_attempts"] = min(int(request.get("max_repair_attempts", 3)), 3)
    request["baseline_cache_dir"] = str(plan_path.parent / "baseline-cache")
    request["package_catalog"] = _catalog_spec(args, plan_path.parent)
    if args.no_cache:
        request["no_cache"] = True
    if args.no_scope:
        request["scoped_build"] = False
    if args.allow_newer_commit:
        request["allow_newer_commit"] = True
    if args.allow_out_of_order:
        request["allow_out_of_order"] = True
    runner = _runner(args.dotnet, out / "logs", preflight)
    runner.set_budget(args.max_minutes)
    result = run_implementation(repo, out, runner, plan, request, RECIPES, preflight)
    budget = _note_budget(runner, out / "result.json", result)
    from .contract import write_contract
    write_contract(out, result, plan_path, mode="implementation")
    print(json.dumps({k: result.get(k) for k in ("status", "group_id", "patch_path", "files_changed",
                                                 "changes_persisted", "unresolved_items")}, indent=2))
    return budget or (0 if result["status"] == "SUCCESS" else 3)


def cmd_global(args) -> int:
    from .catalog import load_catalog
    from .globalphase import run_global_phase
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    preflight_path = plan_path.parent / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else None
    baseline_path = plan_path.parent / "baseline.json"
    request = {"approved": args.approve_global, "allow_newer_commit": args.allow_newer_commit,
               "no_cache": args.no_cache,
               "baseline_cache_dir": str(plan_path.parent / "baseline-cache"),
               "reference_baseline": json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None,
               "original_inventory": json.loads((plan_path.parent / "inventory.json").read_text(encoding="utf-8"))
               if (plan_path.parent / "inventory.json").exists() else None}
    runner = _runner(args.dotnet, out / "logs", preflight)
    nuget = load_catalog(_catalog_spec(args, plan_path.parent), plan_path.parent / "nuget-cache", runner=runner, repo=repo)
    runner.set_budget(args.max_minutes)
    result = run_global_phase(repo, out, runner, plan, args.phase, request, preflight, nuget)
    budget = _note_budget(runner, out / "result.json", result)
    from .contract import write_contract
    write_contract(out, result, plan_path,
                   mode="validation" if args.phase == "PHASE_7_REPO_VALIDATION" else "implementation")
    print(json.dumps({k: result.get(k) for k in ("status", "phase", "patch_path", "files_changed",
                                                 "manual_follow_ups", "unresolved_items")}, indent=2))
    return budget or (0 if result["status"] in {"SUCCESS", "VALIDATION_PASSED", "NOTHING_TO_DO"} else 3)


def cmd_validate(args) -> int:
    from .contract import write_contract
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if args.scope == "repository":  # PHASE_7: read-only, needs no approval
        args.phase, args.approve_global, args.allow_newer_commit, args.no_cache = "PHASE_7_REPO_VALIDATION", True, True, False
        return cmd_global(args)
    from .validate import validate_group
    if not args.group:
        raise SystemExit("--scope group needs --group <group-id>")
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    preflight_path = plan_path.parent / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else None
    baseline_path = plan_path.parent / "baseline.json"
    reference = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    runner = _runner(args.dotnet, out / "logs", preflight)
    runner.set_budget(args.max_minutes)
    result = validate_group(repo, out, runner, plan, args.group, preflight, reference)
    _write(out / "result.json", result)
    write_contract(out, result, plan_path, mode="validation")
    budget = _note_budget(runner, out / "result.json", result)
    print(json.dumps({k: result.get(k) for k in ("status", "group_id", "unresolved_items")}, indent=2))
    return budget or (0 if result["status"] == "VALIDATION_PASSED" else 3)


def cmd_rollback(args) -> int:
    from .validate import rollback_run
    run_dir = Path(args.run).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"{run_dir} is not a run folder")
    report = rollback_run(run_dir, Runner(args.dotnet, run_dir / "logs"))
    _write(run_dir / "rollback.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["verified"] else 3


CATALOG_HELP = ("package catalogue: a fixture JSON emulating the organisation mirror (approved versions), or 'public' "
                "(default: the configured NuGet feeds via dotnet; later steps reuse the analysis's choice)")


def _workspace(repo: Path) -> Path:
    """Default home for every output: <parent>/<repo name>.dmig/ (always outside the repository)."""
    return repo.parent / f"{repo.name}.dmig"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dmig", description="Analysis-first .NET 8 -> .NET 10 migration for monorepos. Typical order: "
        "doctor, analyze, global PHASE_1, migrate (each group), global PHASE_5/6, validate --scope repository, "
        "global PHASE_8. Outputs default to <repo>.dmig/ next to the repository.")
    from . import __version__
    parser.add_argument("--version", action="version", version=f"dmig {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, plan: bool = False, budget: bool = False, catalog: bool = False):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("repository", nargs="?", help="the repository root (or use --repo)")
        c.add_argument("--repo", help=argparse.SUPPRESS)
        c.add_argument("--out", help="output folder, outside the repository (default: <repo>.dmig/<step>)")
        c.add_argument("--dotnet", help="dotnet executable (default: $DMIG_DOTNET or PATH)")
        if plan:
            c.add_argument("--plan", help="migration-plan.json from the analysis (default: <repo>.dmig/analysis/)")
        if budget:
            c.add_argument("--max-minutes", type=float, help="time budget; on expiry the step stops safely (exit code 4)")
        if catalog:
            c.add_argument("--package-catalog", help=CATALOG_HELP)
        return c

    d = command("doctor", "is this machine and repository ready? a plain-language checklist with fixes (run first)")
    pf = command("preflight", "the environment check as JSON (doctor's machine-readable core)")
    for c in (pf,):
        c.add_argument("--environment", help="environment contract JSON (services, licensed tools)")
        c.add_argument("--offline", action="store_true", help="do not probe package feeds")
    a = command("analyze", "analyse the whole repository; changes nothing", budget=True, catalog=True)
    a.add_argument("--target", default="net10.0", choices=["net10.0"], help="target framework (net10.0)")
    a.add_argument("--environment", help="environment contract JSON (services, licensed tools)")
    a.add_argument("--offline", action="store_true", help="do not probe package feeds")
    a.add_argument("--request", help="neutral request JSON")
    a.add_argument("--no-baseline", action="store_true", help="skip restore/build/test")
    a.add_argument("--no-experiment", action="store_true", help="skip the net10.0 experiment")
    a.add_argument("--obsidian-dir", help="also write the Obsidian notes into this folder (default: <out>/obsidian)")
    r = command("report", "re-render plan, report and notes from an existing analysis (no builds)", catalog=True)
    r.add_argument("--obsidian-dir", help="write the notes into this folder (e.g. inside a vault)")
    r.add_argument("--offline", action="store_true", help="use only cached package metadata")
    for name, text in (("migrate", "migrate ONE approved group in a disposable worktree; returns a patch"),
                       ("implement", "same as migrate (older name)")):
        m = command(name, text, plan=True, budget=True, catalog=True)
        m.add_argument("--group", required=True, help="group id from the plan")
        m.add_argument("--target", default="net10.0", choices=["net10.0"], help="target framework (net10.0)")
        m.add_argument("--request", help="implementation request JSON (default: the plan's request for the group)")
        m.add_argument("--allow-newer-commit", action="store_true",
                       help="use the plan after earlier patches were applied (projects are re-read)")
        m.add_argument("--no-cache", action="store_true", help="always rebuild the group baseline")
        m.add_argument("--no-scope", action="store_true", help="build/test everything, not just the group's scope")
        m.add_argument("--allow-out-of-order", action="store_true", help="skip the dependency-order check")
    g = command("global", "a repository-global phase as a separate, explicitly approved patch",
                plan=True, budget=True, catalog=True)
    g.add_argument("--phase", required=True, choices=["PHASE_1_SDK_GLOBAL", "PHASE_5_RETIRE_NET8", "PHASE_6_CONVERGE",
                                                      "PHASE_7_REPO_VALIDATION", "PHASE_8_CUTOVER"])
    g.add_argument("--approve-global", action="store_true", help="required: confirms this phase was approved")
    g.add_argument("--allow-newer-commit", action="store_true")
    g.add_argument("--no-cache", action="store_true", help="always rebuild instead of reusing identical results")
    v = command("validate", "validate one group or the whole repository as it is now; changes nothing",
                plan=True, budget=True, catalog=True)
    v.add_argument("--scope", required=True, choices=["group", "repository"])
    v.add_argument("--group", help="group id (for --scope group)")
    rb = sub.add_parser("rollback", help="clean up a run's disposable workspace and say how to undo its patch")
    rb.add_argument("run", help="the run's output folder")
    rb.add_argument("--dotnet", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command != "rollback":
        repo = args.repository or args.repo
        if not repo:
            parser.error("give the repository: dmig <command> <repository>")
        args.repo = str(Path(repo).resolve())
        ws = _workspace(Path(args.repo))
        step = {"migrate": f"migrate-{getattr(args, 'group', '')}", "implement": f"migrate-{getattr(args, 'group', '')}",
                "global": getattr(args, "phase", "global"), "report": "analysis",
                "validate": f"validate-{getattr(args, 'scope', '')}" + (f"-{args.group}" if getattr(args, "group", None) else "")
                }.get(args.command, args.command if args.command != "analyze" else "analysis")
        args.out = str(Path(args.out).resolve()) if args.out else str(ws / step)
        if hasattr(args, "plan"):
            args.plan = str(Path(args.plan).resolve()) if args.plan else str(ws / "analysis" / "migration-plan.json")
        if Path(args.out).is_relative_to(Path(args.repo)):
            parser.error("--out must be outside the repository")
    handlers = {"preflight": cmd_preflight, "analyze": cmd_analyze, "report": cmd_report, "doctor": cmd_doctor,
                "implement": cmd_implement, "migrate": cmd_implement, "global": cmd_global,
                "validate": cmd_validate, "rollback": cmd_rollback}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
