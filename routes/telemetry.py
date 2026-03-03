from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any, Dict

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

# Minimal in-memory telemetry sink (GUI can push SRM/4D values here).
# Later: persist to memory manager / vector store.

LATEST: Dict[str, Any] = {}

class TelemetryEvent(BaseModel):
    source: str = "gui"
    kind: str
    payload: Dict[str, Any]


@router.post("")
def push(ev: TelemetryEvent):
    LATEST[ev.kind] = {"source": ev.source, "payload": ev.payload}
    return {"ok": True}


@router.get("")
def get_latest():
    return {"latest": LATEST}
