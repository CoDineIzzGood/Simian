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
    "warm_backends_on_launch": False,
    "auto_start_mic": False,
    # Replay buffer auto-start is ON by default so the user gets the
    # "it just records in the background" experience out of the box.
    # The actual FFmpeg start is deferred by replay_autostart_delay_ms
    # below, so this cannot choke app launch.
    "auto_start_replay": True,
    "safe_startup_delay_ms": 2500,
    # Deferred replay-buffer autostart delay, in milliseconds. When
    # ``auto_start_replay`` is true the GUI schedules the buffer this
    # many ms after the main window becomes interactive. 45s is a
    # deliberate floor so launcher heavy lifting (Ollama warmup, mic
    # listener, news refresh) settles before FFmpeg claims the screen
    # and audio capture pipe. Users can still flip to 0 to get the
    # old "start almost immediately" behaviour.
    "replay_autostart_delay_ms": 45000,
    # Screen Awareness -- OFF by default. When enabled the assistant may
    # capture the current screen on demand and optionally describe it with
    # the local vision model from router["vision"]. Exclusions are case-
    # insensitive substrings matched against the active window title.
    "screen_awareness_enabled": False,
    "screen_awareness_exclusions": [],
    # Vision call budget. Thinking-capable local vision models (e.g.
    # qwen3-vl:8b-thinking) can take well over 60s on first use or on
    # slower hardware, so the default is generous. Users can shrink the
    # timeout in Settings if they prefer faster "gave up" feedback.
    "screen_awareness_vision_timeout_sec": 180,
    # Max dimension (px) to which a screenshot is downscaled before it is
    # shipped to the vision model. Cuts base64 payload size and inference
    # cost dramatically vs. sending a raw 4K frame. 0 disables downscale.
    "screen_awareness_vision_max_dim": 1280,
    # Retry budget multiplier applied to the second vision attempt when
    # the first times out or returns empty. Default 1.0 keeps the retry
    # on the same timeout; the old 1.5 multiplier pushed worst-case wait
    # up to 180 + 30 + 270 = 480s on slow machines. Users who routinely
    # see the vision model succeed only on the stretched retry can bump
    # this back up in Settings.
    "screen_awareness_retry_budget_factor": 1.0,
    # Optional smaller vision model tried as the final fallback when the
    # primary model in router["vision"] times out on every attempt. Set
    # to e.g. "qwen2.5-vl:3b" or "llava:7b" to keep screen awareness
    # usable on a slow box. Empty string disables the fallback.
    "screen_awareness_lighter_vision_model": "",
    # Global "older-or-weaker machine" knob. When true, downstream
    # services are expected to self-degrade: smaller vision max_dim,
    # shorter vision timeout floor, slower SRM cadence, fewer replay
    # segments retained, etc. Individual settings still win if the
    # user sets them explicitly; this is a sensible-defaults switch
    # for machines where the default budgets are too aggressive.
    "low_resource_mode": False,
    # ---- Global theme (Pass O) -------------------------------------------
    # Centralised color palette applied by SimianApp._apply_theme(). These
    # defaults keep the current Simian look (dark, mild purple leaning,
    # blue accent). Users can override any key via the Settings tab theme
    # section or the global palette popup. ``theme_accent`` takes
    # precedence over the legacy ``accent_hex`` key; whichever is set wins,
    # and _apply_theme keeps them mirrored so existing consumers of
    # ``accent_hex`` keep working.
    "theme_bg": "#1a1625",
    "theme_panel": "#2a2333",
    "theme_accent": "#4da3ff",
    "theme_accent_hover": "#3b82f6",
    "theme_text": "#e4e2ea",
    "theme_entry": "#1e1a28",
    "theme_log_bg": "#14111c",
}


THEME_KEYS = (
    "theme_bg",
    "theme_panel",
    "theme_accent",
    "theme_accent_hover",
    "theme_text",
    "theme_entry",
    "theme_log_bg",
)

# Frozen copy of the defaults for "Reset to defaults" in the theme UI
# so the caller can round-trip without mutating DEFAULT_SETTINGS.
THEME_DEFAULTS: Dict[str, str] = {k: DEFAULT_SETTINGS[k] for k in THEME_KEYS}


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
            # utf-8-sig tolerates a BOM if one ever got written (we saw a
            # real-world settings.json with a stray BOM at byte 0).
            incoming_text = SETTINGS_PATH.read_text(encoding="utf-8-sig")
            incoming = json.loads(incoming_text)
            if isinstance(incoming, dict):
                _deep_update(base, incoming)
        except Exception as e:
            # Previous code swallowed parse failures silently, which meant
            # a truncated/corrupt settings.json masqueraded as "running on
            # defaults" with no visible reason. Surface it on stderr --
            # the GUI logger will miss this call because it may run
            # before the UILogger exists -- so the launcher and terminal
            # tail pick it up instead of silently losing user config.
            try:
                print(f"[settings] load failed ({e}); using defaults.", file=__import__("sys").stderr)
            except Exception:
                pass
    return Settings(base)


def save_settings(data: Dict[str, Any] | Settings) -> None:
    _ensure_parent()
    payload = data.to_dict() if isinstance(data, Settings) else dict(data)
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
