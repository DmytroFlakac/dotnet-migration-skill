"""Console output that never crashes on a Windows code page (cp1252 has no emoji and no arrows).

Files are always written as UTF-8; only what goes to the terminal passes through here. Labels on the
console are plain words (PASS / WARN / FAIL / BLOCK / INFO).
"""
from __future__ import annotations

import sys
import time

ASCII = {"→": "->", "←": "<-", "—": "-", "–": "-", "…": "...", "·": "-", "✅": "PASS", "⚠️": "WARN", "⚠": "WARN",
         "❌": "BLOCK", "ℹ️": "INFO", "ℹ": "INFO", "⛔": "BLOCK", "❔": "?", "🟢": "", "🟡": "", "🟠": "", "🔴": "",
         "⚪": "", "⚫": "", "☐": "[ ]", "⬜": "[ ]", "§": "section ", "’": "'", "“": '"', "”": '"'}


def install() -> None:
    """Make stdout/stderr replace characters the console cannot show instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # not a text stream (e.g. captured by a test)
            pass


def plain(text: str) -> str:
    """The text with symbols spelled out in ASCII; anything else non-ASCII becomes '?'."""
    for k, v in ASCII.items():
        text = text.replace(k, v)
    return text.encode("ascii", errors="replace").decode("ascii")


def say(text: str, err: bool = False) -> None:
    print(plain(text), file=sys.stderr if err else sys.stdout, flush=True)


def progress(step: str) -> None:
    """One line per step on stderr, so a background run can be followed; stdout stays JSON."""
    say(f"[dmig {time.strftime('%H:%M:%S')}] {step}", err=True)
