"""IWorkItemAdapter: the ticket a run belongs to. Mock: MockWorkItemAdapter always returns TEST-123."""
from __future__ import annotations

from abc import ABC, abstractmethod


class IWorkItemAdapter(ABC):
    @abstractmethod
    def ticket_for(self, title: str, description: str = "") -> str:
        """Return (create or find) the ticket id for this piece of work."""


class MockWorkItemAdapter(IWorkItemAdapter):
    def __init__(self, ticket: str = "TEST-123"):
        self.ticket = ticket
        self.requests: list[dict] = []

    def ticket_for(self, title: str, description: str = "") -> str:
        self.requests.append({"title": title, "description": description})
        return self.ticket


WorkItems, FixedWorkItems = IWorkItemAdapter, MockWorkItemAdapter  # earlier names
