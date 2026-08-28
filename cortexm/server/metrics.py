"""Prometheus-format metrics + structured operation counters.

Renders the process-wide counters from ``cortexm.metrics`` plus
server-side request metrics (status codes, latency histogram, rate-limit
rejections) in Prometheus text exposition format 0.0.4 — no client
libraries, no dependencies, scrape-ready.
"""

from __future__ import annotations

import threading
import time


class PromRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, float]] = {}
        self._histograms: dict[str, list[float]] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, labels: dict | None = None, value: float = 1) -> None:
        key = _label_key(labels)
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._histograms.setdefault(name, []).append(value)

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def render(self) -> str:
        from cortexm import metrics as core
        lines: list[str] = []
        c = core.counters()
        lines.append("# HELP contextm_ingested_tokens_total Total tokens ingested (mu=0 path).")
        lines.append("# TYPE contextm_ingested_tokens_total counter")
        lines.append(f"contextm_ingested_tokens_total {c['ingested_tokens']}")
        lines.append("# HELP contextm_ingested_messages_total Total messages ingested.")
        lines.append("# TYPE contextm_ingested_messages_total counter")
        lines.append(f"contextm_ingested_messages_total {c['ingested_messages']}")
        lines.append("# HELP contextm_extracted_facts_total Facts extracted.")
        lines.append("# TYPE contextm_extracted_facts_total counter")
        lines.append(f"contextm_extracted_facts_total {c['extracted_facts']}")
        lines.append("# HELP contextm_retrievals_total Search operations served.")
        lines.append("# TYPE contextm_retrievals_total counter")
        lines.append(f"contextm_retrievals_total {c['retrievals']}")
        lines.append("# HELP contextm_llm_calls_total LLM calls (must be 0 under mu=0).")
        lines.append("# TYPE contextm_llm_calls_total counter")
        lines.append(f"contextm_llm_calls_total {c['llm_calls']}")
        with self._lock:
            for name, bucket in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                for key, val in sorted(bucket.items()):
                    lines.append(f"{name}{key} {val}")
            for name, val in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {val}")
            for name, obs in sorted(self._histograms.items()):
                lines.append(f"# TYPE {name} histogram")
                bounds = [0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1,
                          0.25, 0.5, 1.0, 2.5, 5.0]
                for b in bounds:
                    cnt = sum(1 for o in obs if o <= b)
                    lines.append(f"{name}_bucket{{le=\"{b}\"}} {cnt}")
                lines.append(f"{name}_bucket{{le=\"+Inf\"}} {len(obs)}")
                if obs:
                    lines.append(f"{name}_sum {sum(obs):.6f}")
                    lines.append(f"{name}_count {len(obs)}")
        return "\n".join(lines) + "\n"


def _label_key(labels: dict | None) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_esc(str(v))}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


REGISTRY = PromRegistry()
