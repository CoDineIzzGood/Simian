"""
Offline mic listener (background voice commands) using Vosk + sounddevice.
Supports wake-word background listening and optional hot-mic chat dictation.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import sounddevice as sd  # type: ignore
    from vosk import KaldiRecognizer, Model  # type: ignore
except Exception:  # pragma: no cover
    sd = None
    Model = None
    KaldiRecognizer = None

CommandCallback = Callable[[str, Dict[str, Any]], None]
TranscriptCallback = Callable[[str, Dict[str, Any]], None]

COMMAND_PATTERNS = {
    "clip": re.compile(r"\bclip that\b", re.IGNORECASE),
    "buffer_start": re.compile(r"\b(start buffer|start replay)\b", re.IGNORECASE),
    "buffer_stop": re.compile(r"\b(stop buffer|stop replay)\b", re.IGNORECASE),
}
IGNORE_UTTERANCES = {"huh", "uh", "um", "hmm", "hm", "mm", "oh"}
SIMIAN_ALIASES = re.compile(r"\b(?:semyon|semion|semi on|sim eon|simiann)\b", re.IGNORECASE)
AUTOMATION_CONTEXT = re.compile(r"\b(script|scripts|task|tasks|workflow|workflows|system|systems|file|files|pipeline|pipelines|process|processes|automation)\b", re.IGNORECASE)
ANIMATION_CONTEXT = re.compile(r"\b(cartoon|movie|video|animate|animation studio|anime)\b", re.IGNORECASE)


def _find_vosk_model_dir() -> Optional[Path]:
    env = os.environ.get("VOSK_MODEL_DIR")
    if env and Path(env).exists():
        return Path(env)

    candidates = [
        Path("models") / "vosk-model-small-en-us-0.15",
        Path("models") / "vosk-model-en-us-0.22",
        Path("voice") / "vosk-model-small-en-us-0.15",
        Path("voice") / "vosk-model-en-us-0.22",
        Path("vosk-model-small-en-us-0.15"),
        Path("vosk-model-en-us-0.22"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    meipass = os.environ.get("_MEIPASS")
    if meipass:
        base = Path(meipass)
        for candidate in base.rglob("vosk-model*"):
            if candidate.is_dir():
                return candidate
    return None


@dataclass
class MicListenerConfig:
    samplerate: int = 16000
    device: Optional[int] = None
    wake_word: str = "simian"
    phrase_end_silence_sec: float = 1.15
    min_transcript_words: int = 2


class MicListenerService:
    def __init__(
        self,
        log_cb: Optional[Callable[[str], None]] = None,
        command_cb: Optional[CommandCallback] = None,
        transcript_cb: Optional[TranscriptCallback] = None,
        config: Optional[MicListenerConfig] = None,
    ):
        self.log = log_cb or (lambda _msg: None)
        self.command_cb = command_cb or (lambda _cmd, _meta: None)
        self.transcript_cb = transcript_cb or (lambda _text, _meta: None)
        self.config = config or MicListenerConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._available = sd is not None and Model is not None and KaldiRecognizer is not None
        self.hot_mode = False
        self._last_text = ""
        self._last_text_ts = 0.0
        self._pending_spoken = ""
        self._pending_meta: Dict[str, Any] = {}
        self._flush_timer: Optional[threading.Timer] = None
        self._pending_lock = threading.Lock()

    def available(self) -> bool:
        return self._available and (_find_vosk_model_dir() is not None)

    def unavailable_reason(self) -> str:
        if sd is None or Model is None or KaldiRecognizer is None:
            return "Mic listener unavailable: install both 'vosk' and 'sounddevice'."
        if _find_vosk_model_dir() is None:
            return "Mic listener unavailable: set VOSK_MODEL_DIR or add a Vosk model folder under ./models or ./voice."
        return "Mic listener unavailable."

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def set_hot_mode(self, enabled: bool) -> None:
        self.hot_mode = bool(enabled)
        self.log(f"[Voice] Listener mode: {'hot mic' if self.hot_mode else 'wake word'}.")

    def start(self) -> None:
        if self.is_running():
            return
        if not self.available():
            self.log(f"[Voice] {self.unavailable_reason()}")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MicListenerService", daemon=True)
        self._thread.start()
        self.log("[Voice] Mic listener started (Vosk).")

    def stop(self) -> None:
        self._stop.set()
        self._cancel_pending_flush()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.log("[Voice] Mic listener stopped.")

    def _run(self) -> None:
        if sd is None or Model is None or KaldiRecognizer is None:
            self.log("[Voice] Listener dependencies are unavailable.")
            return

        model_dir = _find_vosk_model_dir()
        if not model_dir:
            self.log("[Voice] No Vosk model directory found.")
            return

        model = Model(str(model_dir))
        rec = KaldiRecognizer(model, self.config.samplerate)
        rec.SetWords(False)

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if self._stop.is_set():
                callback_stop = getattr(sd, "CallbackStop", None)
                if callback_stop is not None:
                    raise callback_stop()
                return
            if status:
                return

            try:
                data = bytes(indata)
            except Exception:
                try:
                    data = memoryview(indata).tobytes()
                except Exception:
                    return

            if rec.AcceptWaveform(data):
                try:
                    result = json.loads(rec.Result())
                    text = str(result.get("text") or "").strip().lower()
                    if text:
                        self._handle_text(text)
                except Exception:
                    return

        raw_stream = getattr(sd, "RawInputStream", None)
        if raw_stream is None:
            self.log("[Voice] sounddevice.RawInputStream is unavailable.")
            return

        with raw_stream(
            samplerate=self.config.samplerate,
            blocksize=8000,
            dtype="int16",
            channels=1,
            callback=callback,
            device=self.config.device,
        ):
            while not self._stop.is_set():
                time.sleep(0.1)

    def _normalize_text(self, text: str) -> str:
        heard = " ".join((text or "").strip().split())
        if not heard:
            return ""
        heard = SIMIAN_ALIASES.sub("simian", heard)
        if "animation" in heard and AUTOMATION_CONTEXT.search(heard) and not ANIMATION_CONTEXT.search(heard):
            heard = re.sub(r"\banimation\b", "automation", heard)
        return heard.strip().lower()

    def _extract_after_wake(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        if self.hot_mode:
            return cleaned
        wake = (self.config.wake_word or "").strip().lower()
        if not wake:
            return cleaned
        if wake not in cleaned:
            return ""
        parts = cleaned.split(wake, 1)
        return parts[1].strip(" ,.!?-")

    def _is_duplicate(self, text: str) -> bool:
        now = time.time()
        if text == self._last_text and (now - self._last_text_ts) < 2.0:
            return True
        self._last_text = text
        self._last_text_ts = now
        return False

    def _cancel_pending_flush(self) -> None:
        timer = self._flush_timer
        self._flush_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _flush_pending_transcript(self) -> None:
        with self._pending_lock:
            spoken = self._pending_spoken.strip()
            meta = dict(self._pending_meta)
            self._pending_spoken = ""
            self._pending_meta = {}
            self._flush_timer = None
        if not spoken or self._stop.is_set():
            return
        words = [w for w in spoken.split() if w]
        if len(words) < max(1, int(getattr(self.config, "min_transcript_words", 1) or 1)):
            self.log(f"[Voice] Waiting for more speech, held transcript: {spoken}")
            return
        self.transcript_cb(spoken, meta)

    def _queue_transcript(self, spoken: str, meta: Dict[str, Any]) -> None:
        snippet = " ".join((spoken or "").strip().split())
        if not snippet:
            return
        with self._pending_lock:
            if self._pending_spoken:
                existing = self._pending_spoken
                if snippet == existing or snippet in existing:
                    merged = existing
                elif existing in snippet:
                    merged = snippet
                else:
                    merged = f"{existing} {snippet}".strip()
                self._pending_spoken = merged
            else:
                self._pending_spoken = snippet
            self._pending_meta = dict(meta)
            self._pending_meta["hot_mode"] = self.hot_mode
            self._cancel_pending_flush()
            delay = max(0.35, float(getattr(self.config, "phrase_end_silence_sec", 1.15) or 1.15))
            self._flush_timer = threading.Timer(delay, self._flush_pending_transcript)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _handle_text(self, text: str) -> None:
        heard = self._normalize_text(text)
        if not heard or heard in IGNORE_UTTERANCES:
            return
        if self._is_duplicate(heard):
            return

        self.log(f"[Voice] Heard: {heard}")

        for name, pattern in COMMAND_PATTERNS.items():
            if pattern.search(heard):
                meta: Dict[str, Any] = {"raw": heard}
                if name == "clip":
                    match = re.search(r"(?:plus|extra)\s+(\d+)\s*(seconds|second|sec|s)?", heard)
                    if match:
                        meta["extra_seconds"] = int(match.group(1))
                self._cancel_pending_flush()
                self.command_cb(name, meta)
                return

        spoken = self._extract_after_wake(heard)
        if not spoken or spoken in IGNORE_UTTERANCES or len(spoken) < 2:
            return
        self._queue_transcript(spoken, {"raw": heard, "hot_mode": self.hot_mode})
