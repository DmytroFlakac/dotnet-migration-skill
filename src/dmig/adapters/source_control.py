"""ISourceControl: the disposable worktree, the diff and the patch. Mock: LocalGitSourceControl (local git).

Every git command runs with hooks off (see dmig.dotnet.NO_HOOKS). Branches, commits, pushes and merge
requests are not here: they belong to IDelivery, which the orchestrator calls after a person approved
a result. The core itself never commits or pushes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ISourceControl(ABC):
    @abstractmethod
    def head(self, repo: Path) -> str: ...

    @abstractmethod
    def create_worktree(self, repo: Path, path: Path) -> None: ...

    @abstractmethod
    def remove_worktree(self, repo: Path, path: Path) -> None: ...

    @abstractmethod
    def diff(self, worktree: Path) -> str:
        """The change in the worktree against its commit, as a patch `git apply` accepts."""


class LocalGitSourceControl(ISourceControl):
    def __init__(self, runner=None):
        from ..dotnet import Runner as CommandRunner
        self.runner = runner or CommandRunner(None, None)

    def _git(self, args: list[str], cwd: Path, name: str):
        r = self.runner.git(args, cwd, name)
        if not r.ok:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.tail(3)}")
        return r

    def head(self, repo: Path) -> str:
        return self._git(["rev-parse", "HEAD"], repo, "sc-head").output.strip()

    def create_worktree(self, repo: Path, path: Path) -> None:
        self._git(["worktree", "add", "--detach", str(path), "HEAD"], repo, "sc-worktree-add")

    def remove_worktree(self, repo: Path, path: Path) -> None:
        self.runner.git(["worktree", "remove", "--force", str(path)], repo, "sc-worktree-remove")
        self.runner.git(["worktree", "prune"], repo, "sc-worktree-prune")

    def diff(self, worktree: Path) -> str:
        return self._git(["-c", "core.safecrlf=false", "diff", "--binary", "HEAD"], worktree, "sc-diff").stdout
