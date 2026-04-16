from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Any


@dataclass
class HealthState:
    name: str
    status: str  # ok|degraded|error|unknown
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class HealthRegistry:
    def __init__(self):
        self._checks: Dict[str, Callable[[], HealthState]] = {}

    def register(self, name: str, check: Callable[[], HealthState]) -> None:
        self._checks[name] = check

    def run(self) -> list[HealthState]:
        states: list[HealthState] = []
        for name, check in self._checks.items():
            try:
                state = check()
                if not state.name:
                    state.name = name
                states.append(state)
            except Exception as exc:
                states.append(HealthState(name=name, status="error", message=f"health_check_failed: {exc}"))
        return states


def rollup_status(states: list[HealthState]) -> str:
    if not states:
        return "degraded"
    statuses = {s.status for s in states}
    if "error" in statuses:
        return "error"
    if "degraded" in statuses or "unknown" in statuses:
        return "degraded"
    return "ok"
