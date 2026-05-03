"""MetricsCollector — counters, histograms, gauges in-memory.

Snapshot is JSON-serializable per master plan §5.2 (F10).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Mapping, Optional


def _label_key(labels: Mapping[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items())) if labels else ""


class MetricsCollector:
    def __init__(self):
        self._counters: Dict[str, Dict[str, int]] = {}
        self._gauges: Dict[str, Dict[str, float]] = {}
        self._histograms: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = Lock()

    def inc(self, name: str, labels: Optional[Mapping[str, Any]] = None, value: int = 1) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            self._counters.setdefault(name, {})
            self._counters[name][key] = self._counters[name].get(key, 0) + value

    def gauge(
        self,
        name: str,
        labels: Optional[Mapping[str, Any]] = None,
        value: float = 0.0,
    ) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            self._gauges.setdefault(name, {})
            self._gauges[name][key] = float(value)

    def observe(
        self,
        name: str,
        labels: Optional[Mapping[str, Any]] = None,
        value: float = 0.0,
    ) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            self._histograms.setdefault(name, {})
            entry = self._histograms[name].setdefault(
                key, {"count": 0, "sum": 0.0, "buckets": {}, "min": None, "max": None}
            )
            entry["count"] += 1
            entry["sum"] += value
            if entry["min"] is None or value < entry["min"]:
                entry["min"] = value
            if entry["max"] is None or value > entry["max"]:
                entry["max"] = value
            for boundary in (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0):
                if value <= boundary:
                    entry["buckets"][str(boundary)] = entry["buckets"].get(str(boundary), 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": {
                    name: [
                        {"labels": _decode_label_key(k), "value": v}
                        for k, v in series.items()
                    ]
                    for name, series in self._counters.items()
                },
                "gauges": {
                    name: [
                        {"labels": _decode_label_key(k), "value": v}
                        for k, v in series.items()
                    ]
                    for name, series in self._gauges.items()
                },
                "histograms": {
                    name: [
                        {"labels": _decode_label_key(k), **{kk: vv for kk, vv in v.items()}}
                        for k, v in series.items()
                    ]
                    for name, series in self._histograms.items()
                },
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }


def _decode_label_key(key: str) -> Dict[str, str]:
    if not key:
        return {}
    pairs = key.split(",")
    return {p.split("=", 1)[0]: p.split("=", 1)[1] for p in pairs if "=" in p}


class HistogramTimer:
    """Async-friendly context manager: `with metrics.timer(...): ...`."""

    def __init__(self, metrics: MetricsCollector, name: str, labels: Mapping[str, Any]):
        self._metrics = metrics
        self._name = name
        self._labels = labels
        self._start: float = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.monotonic() - self._start
        self._metrics.observe(self._name, self._labels, elapsed)


def timer(metrics: MetricsCollector, name: str, labels: Optional[Mapping[str, Any]] = None) -> HistogramTimer:
    return HistogramTimer(metrics, name, labels or {})
