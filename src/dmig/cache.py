"""Baseline cache: never build and test the same code twice with the same toolchain.

An entry is keyed by the git *tree* hash of the code that was built (content, not commit, so a
patch applied and committed by a person maps to the tree the tool saw). Each entry records the
toolchain fingerprint and the scope (projects built/tested; None = whole repository). A cached
result is reused only when the tree and toolchain match exactly and its scope covers what is needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .dotnet import Runner


def fingerprint(runner: Runner, preflight: dict | None) -> dict:
    pf = preflight or {}
    return {"dotnet": str(runner.dotnet).lower(),
            "selected_sdk": pf.get("selected_sdk_in_repository"),
            "runtimes": sorted(r["version"] for r in pf.get("runtimes", [])
                               if r.get("name") == "Microsoft.NETCore.App")}


def head_tree(repo: Path, runner: Runner) -> str | None:
    r = runner.git(["rev-parse", "HEAD^{tree}"], repo, "git-head-tree")
    return r.stdout.strip() if r.ok else None


def working_tree_hash(worktree: Path, runner: Runner) -> str | None:
    """Tree hash of the worktree's current files (stages everything in the worktree's own index)."""
    add = runner.git(["add", "-A"], worktree, "git-add-all")
    tree = runner.git(["write-tree"], worktree, "git-write-tree")
    return tree.stdout.strip() if add.ok and tree.ok else None


def save(cache_dir: Path | None, tree: str | None, fp: dict, build_test: dict, scope: set[str] | None) -> str | None:
    if not cache_dir or not tree:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{tree}.json"
    path.write_text(json.dumps({"tree": tree, "fingerprint": fp,
                                "scope": sorted(scope) if scope is not None else None,
                                "build_test": build_test}) + "\n", encoding="utf-8")
    return str(path)


def load(cache_dir: Path | None, tree: str | None, fp: dict, needed: set[str]) -> tuple[dict | None, str]:
    """Returns (build_test, reason). build_test is None when nothing reusable exists."""
    if not cache_dir or not tree:
        return None, "no cache directory or tree hash"
    path = cache_dir / f"{tree}.json"
    if not path.exists():
        return None, f"no cached baseline for tree {tree[:12]}"
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry["fingerprint"] != fp:
        return None, "cached baseline used a different dotnet/SDK/runtimes"
    if entry["scope"] is not None and not needed <= set(entry["scope"]):
        return None, "cached baseline did not build/test every project this run needs"
    bt = entry["build_test"]
    if not bt.get("restore_succeeded"):
        return None, "cached baseline did not restore"
    return bt, f"reused cached baseline for tree {tree[:12]}"
