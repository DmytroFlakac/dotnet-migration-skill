"""ISecretProvider: feed credentials supplied at runtime, never created, stored or printed.

Mock: EnvSecretProvider reads DMIG_SECRET_<NAME> from the environment and falls back to an obvious
dummy value, so code paths run on any machine without real credentials.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod


class ISecretProvider(ABC):
    @abstractmethod
    def get(self, name: str) -> str: ...

    def describe(self, name: str) -> str:
        """Safe for logs: says whether a value exists, never what it is."""
        return f"{name}: {'set' if self.get(name) else 'missing'}"


class EnvSecretProvider(ISecretProvider):
    def __init__(self, prefix: str = "DMIG_SECRET_"):
        self.prefix = prefix

    def get(self, name: str) -> str:
        return os.environ.get(self.prefix + name.upper()) or f"dummy-{name.lower()}"


Secrets, EnvSecrets = ISecretProvider, EnvSecretProvider  # earlier names
