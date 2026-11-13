"""ITelemetry: execution events. Mock: JsonlTelemetry appends one redacted JSON object per event."""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..secrets import redact


class ITelemetry(ABC):
    @abstractmethod
    def event(self, name: str, **fields) -> None: ...


class JsonlTelemetry(ITelemetry):
    def __init__(self, path: Path):
        self.path = Path(path)

    def event(self, name: str, **fields) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": name, **fields}, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(redact(line) + "\n")


Telemetry = ITelemetry  # earlier name
