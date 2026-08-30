"""Shared types for the judges package."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class LongMemEvalQuestion:
    """Minimal question record needed by judges that inspect q.question."""
    question: str = ""
    entity: str = ""
    attribute: str = ""
    subtask: str = ""
