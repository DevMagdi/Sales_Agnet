"""Tiny dependency-free metrics registry with Prometheus text rendering."""

from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}

    def incr(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value

    def gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{inner}}}"

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {**self._gauges, **self._counters}

    def render_prometheus(self) -> str:
        lines = ["# HELP mantiq_outreach Metric", "# TYPE mantiq_outreach gauge"]
        for key, value in sorted(self.snapshot().items()):
            safe = key.replace("{", "{").replace('"', '"')
            lines.append(f"mantiq_outreach_{safe} {value}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


metrics = Metrics()
