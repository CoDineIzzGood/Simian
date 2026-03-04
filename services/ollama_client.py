from __future__ import annotations

import os
from typing import Any, Dict, List

import requests

OLLAMA_API = os.getenv("OLLAMA_API", "http://127.0.0.1:11434")


def _get(url: str) -> Dict[str, Any]:
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def list_models() -> List[str]:
    payload = _get(f"{OLLAMA_API}/api/tags")
    models = payload.get("models", [])
    return [m.get("name", "") for m in models if m.get("name")]
