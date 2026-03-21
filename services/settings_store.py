from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

SETTINGS_PATH = Path("config/settings.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "router": {
        "chat": "simian:latest",
        "vision": "qwen3-vl:8b-thinking",
        "code": "qwen2.5-coder:7b",
        "reasoning": "qwen3.5:9b",
        "translate": "translategemma:4b",
        "embedding": "embeddinggemma:300m",
        "fallback": "simian:latest",
    },
    "voice_enabled": True,
    "stt_enabled": True,
    "voice_id": "en-US-GuyNeural",
    "vosk_model_dir": "",
    "wake_word": "simian",
    "chat_request_timeout": 180,
    "accent_hex": "#4da3ff",
    "clips_dir": "data/clips",
    "buffer_dir": "data/buffer",
    "replay_minutes": 5,
    "extra_seconds_default": 0,
    "segment_seconds": 5,
    "fps": 30,
    "width": 1920,
    "height": 1080,
    "export_upscale": "none",
    "news_refresh_seconds": 300,
    "news_default_category": "tech",
    "news_search_limit": 60,
    "stt_input_device": "default",
    "tts_output_device": "default",
    "replay_system_audio_device": "",
    "replay_mic_device": "",
    "auto_start_ollama": True,
    "auto_start_mic": False,
    "auto_start_replay": False,
    "warm_backends_on_launch": True,
    "safe_startup_delay_ms": 1500,
    "ollama_start_timeout": 8,
}


def _deep_update(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in (incoming or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)  # type: ignore[index]
        else:
            base[k] = v
    return base


class Settings(dict):
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            return super().__setattr__(name, value)
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


def _ensure_parent() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    base: Dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            incoming = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(incoming, dict):
                _deep_update(base, incoming)
        except Exception:
            pass
    return Settings(base)


def save_settings(data: Dict[str, Any] | Settings) -> None:
    _ensure_parent()
    payload = data.to_dict() if isinstance(data, Settings) else dict(data)
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
