"""Compatibility shim for legacy imports.

This module simply re-exports the chat_reply helper that now lives in
routes.chat. Keeping this thin wrapper avoids regressions for code paths
that still import from `chat`.
"""
from __future__ import annotations

from routes.chat import chat_reply as _chat_reply

__all__ = ["chat_reply"]


def chat_reply(message: str, session_id: str | None = None) -> str:
    """Return a chat response by delegating to routes.chat.chat_reply."""
    return _chat_reply(message=message, session_id=session_id)
