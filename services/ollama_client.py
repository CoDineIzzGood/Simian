# services/ollama_client.py
"""Ollama client helpers + model auto-detect.

This keeps your existing async chat() helper, but also adds a small
non-async auto-detect for picking the best installed llama3.* model.

Ollama default URL: http://127.0.0.1:11434
"""

from __future__ import annotations

import os
import re
from typing import List, Dict, Any

import httpx

# URL to Ollama server
OLLAMA_URL = os.getenv("OLLAMA_URL") or os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434"

# Preferred model name (can be an alias you created in Ollama)
DEFAULT_TEXT_MODEL = (
    os.getenv("SIMIAN_MODEL")
    or os.getenv("OLLAMA_TEXT_MODEL")
    or os.getenv("OLLAMA_MODEL")
    or "llama3.1"
)


async def chat(
    messages: List[Dict[str, str]],
    model: str | None = None,
    options: Dict[str, Any] | None = None,
    url: str | None = None,
) -> str:
    """Call Ollama /api/chat and return assistant content."""

    url = (url or OLLAMA_URL).rstrip("/")
    if model is None or not str(model).strip():
        model = DEFAULT_TEXT_MODEL

    payload: Dict[str, Any] = {"model": model, "messages": messages or [], "stream": False}
    if options:
        payload["options"] = options

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict):
        msg = data.get("message") or {}
        content = msg.get("content")
        if content:
            return content

    if isinstance(data, list) and data and isinstance(data[-1], dict):
        last = data[-1]
        if "content" in last:
            return str(last["content"])

    return str(data)


def _parse_llama3_version(name: str) -> tuple[int, int, int]:
    """Return (major, minor, patch) for llama3.x[.y] names; unknown -> (0,0,0)."""
    m = re.search(r"(?i)\bllama3(?:\.(\d+))?(?:\.(\d+))?\b", name)
    if not m:
        return (0, 0, 0)
    major = 3
    minor = int(m.group(1) or 0)
    patch = int(m.group(2) or 0)
    return (major, minor, patch)


def list_ollama_models(url: str | None = None) -> list[str]:
    """Fetch installed model tags from Ollama (best-effort)."""
    url = (url or OLLAMA_URL).rstrip("/")
    try:
        import requests

        r = requests.get(f"{url}/api/tags", timeout=3)
        r.raise_for_status()
        data = r.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        out: list[str] = []
        for m in models:
            n = m.get("name") if isinstance(m, dict) else None
            if n:
                out.append(str(n))
        return out
    except Exception:
        return []


def autodetect_text_model(url: str | None = None, prefer: str | None = None) -> str:
    """Pick best available model from Ollama.

    Rules:
      1) If 'prefer' exists in tags (exact or with ':latest'), use it
      2) Else choose highest llama3.* model found
      3) Else fall back to DEFAULT_TEXT_MODEL

    """
    url = url or OLLAMA_URL
    prefer = (prefer or os.getenv("SIMIAN_MODEL") or os.getenv("OLLAMA_TEXT_MODEL") or "").strip() or None

    names = list_ollama_models(url=url)

    if prefer and names:
        for c in (prefer, prefer + ":latest"):
            if c in names:
                return c

    best = None
    best_ver = (0, 0, 0)
    for n in names:
        ver = _parse_llama3_version(n)
        if ver > best_ver:
            best_ver = ver
            best = n

    return best or DEFAULT_TEXT_MODEL
