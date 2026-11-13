"""ICiExecutor: where restore/build/test run, and on which operating system.

Mock: LocalProcessExecutor runs them as local processes on this machine (through dmig.dotnet.Runner).
A production adapter would dispatch to a build agent with the required platform (e.g. Windows for
net*-windows projects).
"""
from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from pathlib import Path


class ICiExecutor(ABC):
    @abstractmethod
    def platforms(self) -> set[str]:
        """Platforms this executor can build and test on: subset of {"WINDOWS", "LINUX", "MACOS"}."""

    @abstractmethod
    def command_runner(self, dotnet: str | None, logs: Path):
        """The object the core uses to run dotnet/git commands (dmig.dotnet.Runner interface)."""

    def can_run(self, required_platform: str) -> bool:
        return required_platform == "ANY" or required_platform in self.platforms()


class LocalProcessExecutor(ICiExecutor):
    def platforms(self) -> set[str]:
        return {{"Windows": "WINDOWS", "Linux": "LINUX", "Darwin": "MACOS"}.get(platform.system(), "UNKNOWN")}

    def command_runner(self, dotnet: str | None, logs: Path):
        from ..dotnet import Runner as CommandRunner
        return CommandRunner(dotnet, logs)


Runner, LocalRunner = ICiExecutor, LocalProcessExecutor  # earlier names
