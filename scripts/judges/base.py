"""LongMemEval deterministic judge package.

Split from the monolithic longmemeval_judge.py for maintainability.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config


@dataclass
class LongMemEvalQuestion:
    session_id: int
    question: str
    answer: str
    subtask: str
    entity: str = ""
    attribute: str = ""
