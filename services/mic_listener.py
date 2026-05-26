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
    # "clip" stays first so replay export is never pre-empted by other
    # intents (replay-first rule from the project brief).
    "clip": re.compile(r"\bclip that\b", re.IGNORECASE),
    "buffer_start": re.compile(r"\b(start buffer|start replay)\b", re.IGNORECASE),
    "buffer_stop": re.compile(r"\b(stop buffer|stop replay)\b", re.IGNORECASE),
    # Screen Awareness voice intents. Only trigger when awareness is
    # enabled in settings; the GUI handler enforces that check so the
    # listener stays dumb.
    "screen_look": re.compile(
        # "at" and "on" are both accepted after "look(ing)" -- STT
        # commonly emits "look on my screen" for "look at my screen".
        r"\b(?:look(?:ing)? (?:at|on) (?:my |the )?screen"
        r"|what(?:'s| is|s)? on (?:my |the )?screen"
        r"|what am i looking at"
        r"|describe (?:my |the )?screen"
        r"|can you see (?:my |the )?screen"
        r"|see what(?:'s| is)? on (?:my |the )?screen"
        r"|(?:see|show me|read|check|analy[sz]e) (?:my |the )?screen)\b",
        re.IGNORECASE,
    ),
    "screen_pause": re.compile(
        r"\b(?:pause|stop|disable)\s+screen\s+(?:awareness|monitoring)\b",
        re.IGNORECASE,
    ),
    "screen_resume": re.compile(
        r"\b(?:resume|start|enable)\s+screen\s+(?:awareness|monitoring)\b",
        re.IGNORECASE,
    ),
}
IGNORE_UTTERANCES = {"huh", "uh", "um", "hmm", "hm", "mm", "oh"}
# Pass S-B: filler / politeness words that frequently lead a wake
# phrase ("hey simian", "ok simian", "please simian", "a simian"). We
# strip them after the wake-word split so "hey simian what time is it"
# leaves the cleaner command "what time is it" for the rest of the
# pipeline. Kept narrow on purpose -- random English fillers like
# "really" or "now" stay in because they often carry intent.
WAKE_LEADING_FILLER = {"hey", "ok", "okay", "yo", "please", "a", "the", "uh", "um", "well"}

# Pass T-A: how long the post-wake-acknowledge "follow-up" window stays
# open. The user said "a simian" -> "I'm listening" -> "what time is
# it" should land as a follow-up without re-speaking the wake word.
# 5s is short enough that ambient TV/radio in the background can't
# accidentally hijack chat for very long, but long enough for a normal
# breath between the ack and the actual question.
WAKE_GRACE_SEC = 5.0

# Pass T-B: small allow-list of short commands we never want the junk
# filter to drop. Vosk often returns these as one or two words and the
# generic "too short" heuristic would otherwise reject them.
VALID_SHORT_COMMANDS = {
    "clip that", "stop", "cancel", "yes", "no", "ok", "okay",
    "pause", "resume", "exit", "quit",
}

# Pass T-B: Vosk-prone background hallucinations the user has actually
# observed in their logs. The list is intentionally small and concrete
# rather than an open-ended language model: any phrase here is one we
# already know the small en-US Vosk model invents on silence/noise on
# this user's box. Extend as we collect more samples in the field.
JUNK_HALLUCINATIONS = re.compile(
    r"\b("
    r"four\s+girls\s+ran\s+to\s+my\s+head"
    r"|love\s+for\s+dan"
    r"|amazon\s+basin"
    r")\b",
    re.IGNORECASE,
)

# Pass S-A: widened alias map. Vosk's small en-US model consistently
# mishears "Simian" as one of these surface forms on the user's box;
# normalize them before any wake-word / command logic so a 70%-accurate
# Vosk transcript still routes correctly. Order doesn't matter -- all
# variants collapse to the canonical "simian" via the same substitution.
# Multi-token forms ("semi him", "sim eon") are listed BEFORE single
# tokens with overlapping substrings so the regex engine prefers them
# (Python's re returns the leftmost-longest at each scan position).
SIMIAN_ALIASES = re.compile(
    r"\b(?:"
    r"semi\s+him|sim\s+eon|semi\s+on|"  # multi-token mishears
    r"semyon|semion|simion|simeon|symian|simiann|cimian|cymian"
    r")\b",
    re.IGNORECASE,
)
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
        # Pass R-C: pause flag for STT/replay-mic coordination. When set,
        # _run() releases the input stream so the replay buffer's
        # AudioFallbackRecorder can claim the same default mic device,
        # then waits for resume() before reopening. The Vosk model stays
        # loaded across the pause (model load is the expensive part), so
        # resume is near-instant.
        self._paused = threading.Event()
        self._available = sd is not None and Model is not None and KaldiRecognizer is not None
        self.hot_mode = False
        self._last_text = ""
        self._last_text_ts = 0.0
        self._pending_spoken = ""
        self._pending_meta: Dict[str, Any] = {}
        self._flush_timer: Optional[threading.Timer] = None
        self._pending_lock = threading.Lock()
        # Surface runtime-truthful startup failures (e.g. stale PortAudio
        # device id) to callers without requiring them to scrape logs.
        self._last_error: Optional[str] = None
        # Pass T-A: monotonic-ish epoch (time.time()) until which the
        # wake-word requirement is relaxed. 0.0 means "no grace open".
        # The GUI calls open_wake_grace() right after we emit a
        # wake_acknowledge command, and extend_wake_grace() after every
        # other accepted command. Reading code consults _in_wake_grace().
        self._wake_grace_until: float = 0.0

    def available(self) -> bool:
        return self._available and (_find_vosk_model_dir() is not None)

    def last_error(self) -> Optional[str]:
        """Return the last startup/stream error seen by _run(), if any.

        Callers use this after start() to confirm the listener actually
        opened the input device. None means 'no recorded failure'.
        """
        return self._last_error

    def unavailable_reason(self) -> str:
        if sd is None or Model is None or KaldiRecognizer is None:
            return "Mic listener unavailable: install both 'vosk' and 'sounddevice'."
        if _find_vosk_model_dir() is None:
            return "Mic listener unavailable: set VOSK_MODEL_DIR or add a Vosk model folder under ./models or ./voice."
        return "Mic listener unavailable."

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        """Release the input device but keep the listener thread + Vosk
        model alive. Used by the replay buffer's audio fallback so it can
        open the same default mic device without fighting the STT path.
        Idempotent. The listener thread observes ``_paused`` inside its
        stream loop and exits the ``with stream:`` block cleanly.
        """
        if self._paused.is_set():
            return
        self._paused.set()
        self.log("[STT] paused due to replay capture")

    def resume(self) -> None:
        """Reopen the input stream after a pause. Idempotent. If the
        listener thread already died (e.g. user stopped between pause
        and resume), this is a no-op -- _start_mic_listener restarts it.
        """
        if not self._paused.is_set():
            return
        self._paused.clear()
        self.log("[STT] resumed after replay capture")

    # ------------------------------------------------------------------
    # Pass T-A: wake grace window helpers
    # ------------------------------------------------------------------
    def _in_wake_grace(self) -> bool:
        """True if a post-ack follow-up window is currently open.

        Crossing the expiry boundary logs ``[Voice] Wake grace expired``
        exactly once -- the flag is reset to 0.0 on the same tick so we
        do not spam the textbox once per audio callback.
        """
        if self._wake_grace_until <= 0.0:
            return False
        if time.time() < self._wake_grace_until:
            return True
        self._wake_grace_until = 0.0
        self.log("[Voice] Wake grace expired")
        return False

    def open_wake_grace(self, seconds: float = WAKE_GRACE_SEC) -> None:
        """Open a fresh follow-up window. Called by the GUI after it
        replies "I'm listening" to a wake_acknowledge command. Idempotent:
        re-opens the same length window if already open.
        """
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = WAKE_GRACE_SEC
        sec = max(0.5, sec)
        self._wake_grace_until = time.time() + sec
        # Format compactly: integer seconds for the common 5s case,
        # one decimal otherwise (so test 0.5s windows still log clearly).
        sec_str = f"{int(sec)}s" if abs(sec - int(sec)) < 1e-6 else f"{sec:.1f}s"
        self.log(f"[Voice] Wake grace opened: {sec_str}")

    def extend_wake_grace(self, seconds: float = WAKE_GRACE_SEC) -> None:
        """Reset the grace window after an accepted follow-up command.
        Same effect as open_wake_grace but logged differently so the
        textbox shows a clear conversational rhythm. If no grace is
        currently open, this is a no-op (the grace state is opened only
        after an explicit wake acknowledgement).
        """
        if self._wake_grace_until <= 0.0:
            return
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = WAKE_GRACE_SEC
        sec = max(0.5, sec)
        self._wake_grace_until = time.time() + sec

    def _is_junk(self, text: str) -> bool:
        """Pass T-B: lightweight junk/low-confidence gate.

        Returns True if the (already normalized) text should be dropped
        before routing. The intent is conservative -- we'd rather pass a
        real command through than drop one. The allow-list of valid short
        commands runs first so 'stop'/'yes'/'no' never gets mistaken for
        a one-word fragment.
        """
        cleaned = (text or "").strip().lower()
        if not cleaned:
            return True
        if cleaned in VALID_SHORT_COMMANDS:
            return False
        # Known Vosk hallucinations from the user's logs.
        if JUNK_HALLUCINATIONS.search(cleaned):
            return True
        # Single-token nonsense fragments under 3 chars (e.g. "ah", "uh",
        # the letter "k"). The IGNORE_UTTERANCES set already catches the
        # most common ones; this is a wider net for anything similar.
        words = cleaned.split()
        if len(words) == 1 and len(cleaned) < 3:
            return True
        return False

    def set_hot_mode(self, enabled: bool) -> None:
        prev = self.hot_mode
        self.hot_mode = bool(enabled)
        # Pass R-B: when transitioning between modes, clear the
        # duplicate-suppression cache so a phrase the user just spoke as
        # the wake-word trigger doesn't get rejected as a "duplicate
        # within 2s" the moment hot mic flips on (a real symptom users
        # were hitting where the first hot-mic utterance silently dropped).
        if prev != self.hot_mode:
            self._last_text = ""
            self._last_text_ts = 0.0
            # Pass T-A: any pending grace window from the previous mode
            # is meaningless in the new mode (hot mic doesn't need it,
            # and a fresh wake-word session should start cold).
            self._wake_grace_until = 0.0
        self.log(
            f"[Voice] Listener mode: {'hot mic' if self.hot_mode else 'wake word'}."
        )

    def start(self) -> None:
        if self.is_running():
            return
        if not self.available():
            self.log(f"[Voice] {self.unavailable_reason()}")
            return
        self._last_error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MicListenerService", daemon=True)
        self._thread.start()
        self.log("[Voice] Mic listener started (Vosk).")

    def stop(self) -> None:
        self._stop.set()
        # Clear pause so the _run() pause loop falls through to the
        # _stop check and exits instead of spinning until is_running()
        # eventually reports false. Order matters: set _stop first so
        # the post-pause iteration bails out before reopening the stream.
        self._paused.clear()
        # Pass T-A: drop any open grace window so the next start() begins
        # cold without an inherited follow-up timer.
        self._wake_grace_until = 0.0
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

        # Pass R-A: surface every gate the user asked for. Absolute path
        # (so a wrong VOSK_MODEL_DIR is obvious in the log), the actual
        # device id we'll pass to PortAudio, the listener mode, and the
        # samplerate. Resolved device name is best-effort; PortAudio can
        # raise on query_devices() if the backend just got unplugged.
        try:
            model_abs = str(Path(model_dir).resolve())
        except Exception:
            model_abs = str(model_dir)
        device_label = self._describe_device(self.config.device)
        self.log(
            f"[Voice] Listener thread starting; device={self.config.device!r} "
            f"({device_label}); samplerate={self.config.samplerate}; "
            f"mode={'hot mic' if self.hot_mode else 'wake word'}."
        )
        self.log(f"[Voice] Vosk model path: {model_abs}")

        model = Model(str(model_dir))
        rec = KaldiRecognizer(model, self.config.samplerate)
        rec.SetWords(False)

        # Chunk counter for the "audio chunk received" gate. We emit a
        # single summary every CHUNK_LOG_EVERY chunks (~ once every 100s
        # at 16 kHz / 8000 frames per chunk) instead of one log line per
        # chunk -- the latter would drown the log textbox.
        chunk_state: Dict[str, int] = {"chunks": 0, "since_last_log": 0}
        CHUNK_LOG_EVERY = 200

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if self._stop.is_set() or self._paused.is_set():
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

            chunk_state["chunks"] += 1
            chunk_state["since_last_log"] += 1
            if chunk_state["since_last_log"] >= CHUNK_LOG_EVERY:
                self.log(
                    f"[Voice] Audio chunks received: {chunk_state['chunks']} "
                    f"(rolling)."
                )
                chunk_state["since_last_log"] = 0

            if rec.AcceptWaveform(data):
                try:
                    result = json.loads(rec.Result())
                    text = str(result.get("text") or "").strip().lower()
                    if text:
                        # Pass R-A: log the raw Vosk transcript before any
                        # normalization / dedupe / wake-word checks. Lets
                        # us tell "Vosk is silent" from "we filtered it".
                        self.log(f"[Voice] Vosk raw: {text}")
                        self._handle_text(text)
                except Exception:
                    return

        raw_stream = getattr(sd, "RawInputStream", None)
        if raw_stream is None:
            msg = "sounddevice.RawInputStream is unavailable"
            self._last_error = msg
            self.log(f"[Voice] {msg}.")
            return

        # Pass R-C: pause-aware outer loop. Each iteration opens a fresh
        # input stream. When pause() fires, the inner with-block exits
        # cleanly (the stream callback raises CallbackStop), the device
        # is released so the replay buffer's AudioFallbackRecorder can
        # claim the same default mic, and we spin in the pause loop
        # below until resume() clears the flag. _stop short-circuits the
        # whole thing so close-during-pause still tears down cleanly.
        first_open = True
        while not self._stop.is_set():
            if self._paused.is_set():
                # Sleep in small increments so stop() during a pause
                # joins promptly. 100ms matches the original poll cadence.
                time.sleep(0.1)
                continue

            try:
                stream = raw_stream(
                    samplerate=self.config.samplerate,
                    blocksize=8000,
                    dtype="int16",
                    channels=1,
                    callback=callback,
                    device=self.config.device,
                )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self.log(
                    f"[Voice] Mic listener could not open input device "
                    f"{self.config.device!r}: {exc}"
                )
                self._stop.set()
                return

            if first_open:
                self.log(
                    f"[Voice] Input stream opened on device={self.config.device!r}; "
                    f"listener active."
                )
                first_open = False
            else:
                self.log("[Voice] Input stream reopened after pause.")

            try:
                with stream:
                    while not self._stop.is_set() and not self._paused.is_set():
                        time.sleep(0.1)
            except Exception as exc:
                # CallbackStop from the pause path is expected and clean;
                # only log noisy stream errors.
                msg = f"{type(exc).__name__}: {exc}"
                if "CallbackStop" not in msg:
                    self._last_error = msg
                    self.log(f"[Voice] Mic listener stream error: {exc}")
                    self._stop.set()
                    return

            if self._paused.is_set() and not self._stop.is_set():
                self.log("[Voice] Input stream closed for pause; awaiting resume.")

        self.log("[Voice] Listener thread stopped.")

    def _describe_device(self, device: Optional[int]) -> str:
        """Best-effort PortAudio device name lookup for log lines.

        Returns ``"default"`` when ``device`` is None (PortAudio uses the
        OS default), the device's ``name`` field when query_devices()
        returns one, or ``"unknown"`` when the backend refuses (e.g. mid
        unplug). Never raises -- this is a logging convenience only.
        """
        if device is None:
            return "default"
        if sd is None:
            return "unknown"
        try:
            info = sd.query_devices(device)  # type: ignore[union-attr]
            name = ""
            if isinstance(info, dict):
                name = str(info.get("name") or "").strip()
            elif info is not None:
                name = str(getattr(info, "name", "") or "").strip()
            return name or f"index {device}"
        except Exception:
            return f"index {device}"

    def _normalize_text(self, text: str) -> str:
        heard = " ".join((text or "").strip().split())
        if not heard:
            return ""
        heard = SIMIAN_ALIASES.sub("simian", heard)
        if "animation" in heard and AUTOMATION_CONTEXT.search(heard) and not ANIMATION_CONTEXT.search(heard):
            heard = re.sub(r"\banimation\b", "automation", heard)
        return heard.strip().lower()

    def _extract_after_wake(self, text: str, in_grace: bool = False) -> str:
        cleaned = (text or "").strip(" ,.!?-")
        if not cleaned:
            return ""
        wake = (self.config.wake_word or "").strip().lower()
        if not wake:
            return self._strip_filler(cleaned)
        # Pass S-B / Pass T-A: in hot mode OR while a post-ack grace
        # window is open, peel a leading "[filler]* simian" prefix off
        # if present, otherwise treat the whole utterance as the command.
        # Filler is stripped both before AND after the wake split so
        # "hey simian please clip that" reduces to "clip that".
        if self.hot_mode or in_grace:
            if wake in cleaned:
                head, _, tail = cleaned.partition(wake)
                if not self._strip_filler(head):
                    return self._strip_filler(tail.strip(" ,.!?-"))
            return self._strip_filler(cleaned)
        # Wake-word mode: utterance MUST contain the wake word.
        if wake not in cleaned:
            return ""
        parts = cleaned.split(wake, 1)
        return self._strip_filler(parts[1].strip(" ,.!?-"))

    def _strip_filler(self, text: str) -> str:
        """Drop leading filler words like 'hey'/'please'/'a' that voice
        users naturally say before a command. Iterative so multi-token
        prefixes ("hey please") collapse on a single call.
        """
        cleaned = (text or "").strip(" ,.!?-")
        while True:
            tokens = cleaned.split()
            if not tokens:
                return ""
            head = tokens[0].lower()
            if head not in WAKE_LEADING_FILLER:
                return cleaned
            cleaned = " ".join(tokens[1:]).strip(" ,.!?-")

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
        # Pass S-A: log the truly raw transcript (before alias substitution)
        # so we can prove the alias map is doing what we expect. The
        # callback in _run already logs '[Voice] Vosk raw:'; this line
        # gives us the post-strip / pre-alias view too.
        raw = " ".join((text or "").strip().split())
        heard = self._normalize_text(text)
        wake = (self.config.wake_word or "").strip().lower()
        had_wake = bool(wake) and wake in heard
        # Pass T-A: consult grace state ONCE per utterance so the
        # logging is consistent if the timer happens to expire mid-route.
        in_grace = self._in_wake_grace()
        if heard != raw:
            self.log(
                f"[Voice] Normalized: raw='{raw}' -> normalized='{heard}' "
                f"(wake_match={had_wake})"
            )
        else:
            self.log(f"[Voice] Normalized: '{heard}' (wake_match={had_wake})")

        if not heard:
            self.log(f"[Voice] Rejected (empty after normalize): {text!r}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        if heard in IGNORE_UTTERANCES:
            self.log(f"[Voice] Rejected (ignore-utterance): {heard}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        if self._is_duplicate(heard):
            self.log(f"[Voice] Rejected (duplicate within 2s): {heard}")
            return
        # Pass T-B: junk/hallucination gate. Runs after dedupe so the
        # rejection log has a stable reason. Skips for transcripts that
        # contain a known command pattern (a real "clip that" should
        # never be killed by a length heuristic).
        if self._is_junk(heard) and not any(p.search(heard) for p in COMMAND_PATTERNS.values()):
            self.log(f"[Voice] Rejected: low_confidence/junk transcript: {heard}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
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
                # Pass R-D: explicit accept log so every transcript has
                # exactly one accepted/rejected line in the GUI textbox.
                self.log(f"[Voice] Command routed: {name} (raw='{heard}')")
                # Pass T-C: route label. CLIP gets its own label per the
                # user's spec; everything else collapses to a generic
                # COMMAND tag so the log is greppable but uncluttered.
                if name == "clip":
                    self.log("[Voice] Route: CLIP")
                else:
                    self.log(f"[Voice] Route: COMMAND/{name}")
                self.command_cb(name, meta)
                return

        spoken = self._extract_after_wake(heard, in_grace=in_grace)
        # Pass T-A: if grace caught a follow-up that lacked the wake
        # word, log it explicitly so the conversational handoff is
        # visible in the textbox.
        grace_used = bool(spoken) and in_grace and (not had_wake) and (not self.hot_mode)
        if grace_used:
            self.log(f"[Voice] Wake grace accepted: {spoken}")
        # Pass S-B: when the wake word fired but nothing meaningful
        # follows ("hey simian", "simian"), don't silently drop -- emit
        # a synthetic 'wake_acknowledge' command so the GUI can play a
        # ready/listening response. Only fires in wake-word mode (in hot
        # mode the heard text becomes the spoken command directly).
        if not spoken:
            if had_wake and not self.hot_mode:
                self.log(f"[Voice] Wake-only utterance: {heard}")
                self.log("[Voice] Route: WAKE_ACK")
                self._cancel_pending_flush()
                self.command_cb("wake_acknowledge", {"raw": heard})
                return
            mode = "hot mic" if self.hot_mode else "wake word"
            self.log(
                f"[Voice] Rejected (no '{self.config.wake_word}' wake word in "
                f"{mode} mode): {heard}"
            )
            self.log("[Voice] Route: REJECTED_NO_WAKE")
            return
        if spoken in IGNORE_UTTERANCES:
            self.log(f"[Voice] Rejected (ignore-utterance after wake): {spoken}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        if len(spoken) < 2:
            self.log(f"[Voice] Rejected (too short after wake): {spoken!r}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        # Pass T-B: re-run the junk gate against the post-wake-strip
        # spoken text. The pre-route check above sees the full transcript
        # ("hey simian amazon basin"), but a clean wake-word strip can
        # leave the hallucination alone ("amazon basin"). Allow-listed
        # short commands like "stop" still pass.
        if self._is_junk(spoken):
            self.log(f"[Voice] Rejected: low_confidence/junk transcript: {spoken}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        self.log(f"[Voice] Transcript queued for flush: {spoken}")
        self._queue_transcript(spoken, {"raw": heard, "hot_mode": self.hot_mode})
