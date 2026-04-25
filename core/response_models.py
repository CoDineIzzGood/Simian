from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ErrorInfo(BaseModel):
    code: str = "internal_error"
    message: str
    details: Optional[Dict[str, Any]] = None


class ServiceResponse(BaseModel):
    status: str = Field(default="ok", pattern="^(ok|degraded|error)$")
    message: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[ErrorInfo] = None


class HealthComponent(BaseModel):
    name: str
    status: str = Field(pattern="^(ok|degraded|error|unknown)$")
    message: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: str = Field(pattern="^(ok|degraded|error)$")
    components: List[HealthComponent] = Field(default_factory=list)


class ChatIn(BaseModel):
    text: Optional[str] = None
    message: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    model: Optional[str] = None


class ChatOut(BaseModel):
    reply: str
    model: Optional[str] = None
