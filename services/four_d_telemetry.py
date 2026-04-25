"""
4D Lab telemetry service (scaffold).

Captures lightweight in-app events (chat turn, TTS start/stop, screen
awareness tick, replay rung transitions, mic hot/cold) on a bounded
deque. The 4D Lab tab reads the latest N events and overlays them on
the SRM visualizer to give a "the app is alive" feel without polluting
the main loop.

Design rules:
  * Ring buffer only. No unbounded state, no disk writes on the
    hot path (optional JSONL dump is a future feature; gated behind
    a settings flag).
  * Emit() never raises. Bad callers must not be able to crash the
    GUI through telemetry.
  * Reads are lock-protected and return a shallow copy so the GUI
    never iterates a mutating deque.
  * Zero external deps.

This is a SCAFFOLD: the GUI wire-in is minimal in this pass -- callers
can publish events but the 4D visualizer does not yet render them as a
second-layer overlay. That overlay is tracked in the Flourishin
backlog under "4D Lab -- live telemetry integration".
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass(frozen=True)
class TelemetryEvent:
    ts: float
    kind: str  # e.g. "chat", "tts", "vision", "replay", "mic"
    label: str
    meta: Dict[str, Any] = field(default_factory=dict)


class FourDTelemetry:
    """Thread-safe bounded telemetry buffer for 4D Lab."""

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = max(16, int(capacity))
        self._events: Deque[TelemetryEvent] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._subscribers: List[Any] = []

    def emit(self, kind: str, label: str, **meta: Any) -> None:
        """Best-effort event publish. Never raises."""
        try:
            evt = TelemetryEvent(ts=time.time(), kind=str(kind), label=str(label), meta=dict(meta))
        except Exception:
            return
        with self._lock:
            self._events.append(evt)
            subs = list(self._subscribers)
        # Fan-out is best-effort. A buggy subscriber must not stop
        # downstream ones from getting the event.
        for cb in subs:
            try:
                cb(evt)
            except Exception:
                continue

    def snapshot(self, limit: Optional[int] = None) -> List[TelemetryEvent]:
        with self._lock:
            if limit is None or limit >= len(self._events):
                return list(self._events)
            return list(self._events)[-int(limit):]

    def subscribe(self, callback: Any) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def capacity(self) -> int:
        return self._capacity


# Module-level singleton. Imports that want to publish events just use
# ``from services.four_d_telemetry import telemetry`` and call
# ``telemetry.emit(...)``; reads go through the same singleton so the
# GUI sees everything.
telemetry = FourDTelemetry()


__all__ = ["telemetry", "FourDTelemetry", "TelemetryEvent"]
