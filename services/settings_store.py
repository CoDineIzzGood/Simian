"""
Central settings store for Simian.

- Persists user preferences to data/settings.json
- Safe defaults if file missing/corrupt
- No GUI imports (keeps it usable by backend/services)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SETTINGS_PATH = Path("data") / "settings.json"


@dataclass
class Settings:
    # UI
    theme: str = "dark"
    accent_hex: str = "#1f6feb"  # used by GUI for accent elements

    # Voice / TTS
    voice_enabled: bool = True
    tts_engine: str = "edge"  # "edge" or "pyttsx3"
    voice_id: str = "en-US-GuyNeural"
    voice_rate: str = "+0%"  # Edge-TTS format

    # Replay buffer / clips
    clips_dir: str = r"D:\Project_C.H.I.M.P\Simian\data\clips"
    buffer_dir: str = r"D:\Project_C.H.I.M.P\Simian\data\buffer"
    replay_minutes: int = 5
    segment_seconds: int = 10
    fps: int = 60
    width: int = 1280
    height: int = 720
    export_upscale: str = "none"  # "none" | "1080p" | "4k"
    extra_seconds_default: int = 0

    # News
    news_refresh_seconds: int = 300
    news_default_category: str = "tech"


def _coerce_int(v: Any, fallback: int) -> int:
    try:
        return int(v)
    except Exception:
        return fallback


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    try:
        if not path.exists():
            return Settings()
        data = json.loads(path.read_text(encoding="utf-8"))
        s = Settings()
        for k, v in data.items():
            if not hasattr(s, k):
                continue
            # coerce some numeric fields
            if k in ("replay_minutes", "segment_seconds", "fps", "width", "height", "news_refresh_seconds", "extra_seconds_default"):
                v = _coerce_int(v, getattr(s, k))
            setattr(s, k, v)
        return s
    except Exception:
        # corrupt config -> defaults
        return Settings()


def save_settings(settings: Settings, path: Path = DEFAULT_SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def patch_settings(patch: Dict[str, Any], path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    s = load_settings(path)
    for k, v in patch.items():
        if hasattr(s, k):
            setattr(s, k, v)
    save_settings(s, path)
    return s
