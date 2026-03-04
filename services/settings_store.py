from __future__ import annotations

from pathlib import Path
import json
from typing import Dict, Any

SETTINGS_PATH = Path("config/settings.json")
DEFAULT_SETTINGS: Dict[str, Any] = {
    "router": {
        "chat": "qwen3.5:9b",
        "vision": "qwen3-vl:8b-thinking",
        "code": "qwen2.5-coder:7b",
        "reasoning": "qwen3-vl:8b-thinking",
        "translate": "translategemma:4b",
        "embedding": "embeddinggemma:300m",
        "fallback": "qwen3.5:9b",
    }
}


def _ensure_parent() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS.copy()

    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_SETTINGS.copy()


def save_settings(data: Dict[str, Any]) -> None:
    _ensure_parent()
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
