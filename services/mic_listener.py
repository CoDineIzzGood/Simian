"""
Offline mic listener (background voice commands) using Vosk + sounddevice.

Commands supported (customizable later):
- "clip that"                      -> export last replay buffer window
- "clip that plus <N> seconds"      -> export last window + extra seconds
- "start buffer" / "stop buffer"    -> controls replay buffer recorder
- "start recording" / "stop recording" -> for one-off recorder (not implemented here)

This module is intentionally dependency-soft:
- If vosk/sounddevice are missing, MicListenerService reports unavailable instead of crashing.

Bundling notes (PyInstaller):
- include the Vosk model folder under dist (see your spec file)
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Any

from services.settings_store import load_settings

try:
    import sounddevice as sd  # type: ignore
    from vosk import Model, KaldiRecognizer  # type: ignore
except Exception:  # pragma: no cover
    sd = None
    Model = None
    KaldiRecognizer = None


CommandCallback = Callable[[str, Dict[str, Any]], None]


def _find_vosk_model_dir() -> Optional[Path]:
    # User can override
    env = os.environ.get("VOSK_MODEL_DIR")
    if env and Path(env).exists():
        return Path(env)

    # Typical locations in your project
    candidates = [
        Path("models") / "vosk-model-small-en-us-0.15",
        Path("models") / "vosk-model-en-us-0.22",
        Path("vosk-model-small-en-us-0.15"),
        Path("vosk-model-en-us-0.22"),
    ]
    for c in candidates:
        if c.exists():
            return c

    # PyInstaller _MEIPASS
    meipass = os.environ.get("_MEIPASS")
    if meipass:
        base = Path(meipass)
        for c in base.rglob("vosk-model*"):
            if c.is_dir():
                return c
    return None


@dataclass
class MicListenerConfig:
    samplerate: int = 16000
    device: Optional[int] = None  # sounddevice device index
    wake_word: str = "simian"     # optional wake word; if empty -> hot commands always on


class MicListenerService:
    def __init__(self, log_cb=None, command_cb: Optional[CommandCallback] = None, config: Optional[MicListenerConfig] = None):
        self.log = log_cb or (lambda msg: None)
        self.command_cb = command_cb or (lambda cmd, meta: None)
        self.config = config or MicListenerConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._available = sd is not None and Model is not None and KaldiRecognizer is not None

    def available(self) -> bool:
        return self._available and (_find_vosk_model_dir() is not None)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.available():
            self.log("[Voice] Mic listener unavailable (install vosk + sounddevice, and provide a Vosk model folder).")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MicListenerService", daemon=True)
        self._thread.start()
        self.log("[Voice] Mic listener started (Vosk).")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.log("[Voice] Mic listener stopped.")

    def _run(self) -> None:
        model_dir = _find_vosk_model_dir()
        if not model_dir:
            self.log("[Voice] No Vosk model directory found.")
            return

        model = Model(str(model_dir))
        rec = KaldiRecognizer(model, self.config.samplerate)
        rec.SetWords(False)

        def callback(indata, frames, time_info, status):  # pragma: no cover
            if self._stop.is_set():
                raise sd.CallbackStop()
            if status:
                # don't spam logs
                pass
            data = indata.tobytes()
            if rec.AcceptWaveform(data):
                try:
                    import json
                    j = json.loads(rec.Result())
                    text = (j.get("text") or "").strip().lower()
                    if text:
                        self._handle_text(text)
                except Exception:
                    pass

        with sd.RawInputStream(samplerate=self.config.samplerate, blocksize=8000, dtype="int16",
                               channels=1, callback=callback, device=self.config.device):
            while not self._stop.is_set():
                time.sleep(0.1)

    def _handle_text(self, text: str) -> None:
        # Optional wake word gating
        wake = (self.config.wake_word or "").strip().lower()
        if wake:
            if wake not in text:
                return
            # remove wake word
            text = text.replace(wake, "").strip()

        # Parse commands
        cmd = None
        meta: Dict[str, Any] = {"raw": text}

        if "clip that" in text:
            cmd = "clip"
            # parse optional extra seconds
            m = re.search(r"(?:plus|extra)\s+(\d+)\s*(seconds|second|sec|s)?", text)
            if m:
                meta["extra_seconds"] = int(m.group(1))
        elif "start buffer" in text or "start replay" in text:
            cmd = "buffer_start"
        elif "stop buffer" in text or "stop replay" in text:
            cmd = "buffer_stop"

        if cmd:
            self.log(f"[Voice] Command: {cmd} ({text})")
            self.command_cb(cmd, meta)
