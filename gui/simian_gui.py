"""
Simian GUI: Settings, Replay Buffer, File Summarizer, News, 4D Lab.

Run:
  python -m gui.simian_gui
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

# Make sibling packages importable even when this file is run directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import customtkinter as ctk

from core.task_runner import get_task_runner
from gui.widgets.status_badge import StatusBadge
from services.settings_store import Settings, load_settings, save_settings
from services.replay_buffer import CaptureDevices, ReplayBufferRecorder
from services.file_scanner import FileScannerService
from services.news_service import NewsItem, fetch_news
from services.simian import SYSTEM_PERSONA, time_of_day_greeting

try:
    from services.audio_devices import (
        DEFAULT_WASAPI_SYSTEM,
        list_dshow_audio_devices,
        list_replay_mic_choices,
        list_replay_system_choices,
        list_sounddevice_devices,
        pick_best_system_audio_choice,
    )
except Exception:
    DEFAULT_WASAPI_SYSTEM = "__DEFAULT_WASAPI__"  # type: ignore[assignment]
    list_dshow_audio_devices = None  # type: ignore[assignment]
    list_replay_mic_choices = None  # type: ignore[assignment]
    list_replay_system_choices = None  # type: ignore[assignment]
    list_sounddevice_devices = None  # type: ignore[assignment]
    pick_best_system_audio_choice = None  # type: ignore[assignment]

# Human-friendly label the picker renders for the WASAPI-loopback sentinel
# returned by list_replay_system_choices. Round-tripped back to the sentinel
# string when the user saves their choices so ReplayBufferRecorder still
# receives ``__DEFAULT_WASAPI__`` and invokes _pick_auto_system_audio().
DEFAULT_WASAPI_LABEL = "Default (auto-detect desktop audio)"

# TODO(audio-everywhere-picker): surface the replay desktop-audio and
# replay-mic pickers directly inline on the Services and Clips tabs
# instead of only via the Settings -> 'Open audio device picker' button,
# so the user can pick their capture inputs without leaving the workflow.
# TODO(screen-awareness-prepass): before the heavy vision call fires,
# run a cheap fast pre-pass that returns the active-window title + a
# short textual hint immediately, then stream the vision result when it
# lands. Today a 180s+ vision miss returns nothing at all.
# TODO(crash-audit-hook): wrap mainloop in a top-level except handler
# that writes a dated traceback to data/crashlogs/ before re-raising, so
# the next launch can surface 'last run crashed at X' in the Logs tab.

try:
    from services.mic_listener import MicListenerConfig, MicListenerService
except Exception:
    MicListenerConfig = None  # type: ignore[assignment]
    MicListenerService = None  # type: ignore[assignment]

try:
    from services.screen_awareness import get_screen_awareness
except Exception:
    get_screen_awareness = None  # type: ignore[assignment]

try:
    from voice.edge_tts_speak import speak as _edge_speak_text
except Exception:
    try:
        from voice.voice import speak_text as _edge_speak_text
    except Exception:
        _edge_speak_text = None

try:
    from services.tts_edge import synthesize_to_file as _service_tts_to_file
except Exception:
    _service_tts_to_file = None


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000

SIMIAN_SYSTEM_PROMPT = f"""{SYSTEM_PERSONA}
You are the local assistant for Project C.H.I.M.P.
Never say your name is Qwen, Alibaba Cloud, or Ollama.
When asked who you are, answer that you are Simian.
Stay practical, direct, calm, and slightly tech-noir.
"""

IDENTITY_QUERY_RE = re.compile(
    r"\b(what(?:'s| is) your name|who are you|what are you|your name|call yourself)\b",
    re.IGNORECASE,
)

SRM_QUERY_RE = re.compile(
    r"\b(4d|srm|telemetry|theta|phi|sigma|visualizer|visualiser)\b",
    re.IGNORECASE,
)

IMAGE_REQUEST_RE = re.compile(
    r"\b(?:make|create|generate|draw)\b.*\b(?:image|picture|photo|wallpaper|art|poster|logo|icon)\b|"
    r"^(?:image|picture|photo|wallpaper|art|poster|logo|icon)\b",
    re.IGNORECASE,
)

VIDEO_REQUEST_RE = re.compile(
    r"\b(?:make|create|generate)\b.*\b(?:video|animation|clip|movie)\b|"
    r"^(?:video|animation|clip|movie)\b",
    re.IGNORECASE,
)

HEALTH_QUERY_RE = re.compile(
    r"\b(system health|monitor system health|cpu usage|memory usage|disk space|network status|temperature)\b",
    re.IGNORECASE,
)

# Phrasings that should route through Screen Awareness instead of the
# generic chat / video-gen path. Widened vs v1 to tolerate common voice
# phrasings: "see my screen", "show me my screen", "read my screen",
# "analyse my screen" (en-GB spelling), plus "looking at" as well as
# "look at". Leading/trailing filler like "hey simian", "please", or
# "right now" is already tolerated because the regex uses \b anchors
# rather than ^/$.
SCREEN_QUESTION_RE = re.compile(
    r"\b(?:"
    r"what(?:'s| is|s)?\s+on\s+(?:my\s+|the\s+)?screen"
    # "look at my screen" and the STT-common "look on my screen" both
    # route here, along with the present-continuous "looking at/on".
    r"|look(?:ing)?\s+(?:at|on)\s+(?:my\s+|the\s+)?screen"
    r"|what\s+am\s+i\s+looking\s+at"
    r"|describe\s+(?:my\s+|the\s+)?screen"
    r"|can\s+you\s+see\s+(?:my\s+|the\s+)?screen"
    r"|see\s+what(?:'s| is)?\s+on\s+(?:my\s+|the\s+)?screen"
    r"|(?:see|show\s+me|read|check|analy[sz]e)\s+(?:my\s+|the\s+)?screen"
    r")\b",
    re.IGNORECASE,
)


class UILogger:
    def __init__(self, root: ctk.CTk, textbox: ctk.CTkTextbox, max_lines: int = 800, poll_ms: int = 120, batch_size: int = 80):
        self.root = root
        self.textbox = textbox
        self.max_lines = max_lines
        self.poll_ms = max(50, int(poll_ms))
        # When low_resource_mode is on we slow the UI-thread flush
        # cadence to roughly 2x. A single flush still coalesces any
        # lines that queued up, so throughput is unchanged -- what
        # changes is how often the main thread has to wake to
        # redraw the log widget.
        self._low_res_poll_ms = max(self.poll_ms, 250)
        self.batch_size = max(10, int(batch_size))
        # Perf instrumentation: sum of drain() wall-clock durations
        # since last _perf_flush_report. Reported once every N drains
        # to the log itself so slow textbox inserts show up visibly
        # without a profiler. Bounded to avoid log spam.
        self._perf_drain_total_ms = 0.0
        self._perf_drain_count = 0
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._scheduled = False
        self._line_count = 0
        # Pass Q: persistent disk mirror so we can keep `max_lines`
        # short in the textbox (cheap render) while still preserving
        # the full session log on disk for postmortem. Append-mode;
        # rotated naively when over ~5MB to bound disk usage.
        self._disk_path: Optional[Path] = None
        self._disk_fh: Optional[Any] = None
        self._disk_bytes_written = 0
        self._disk_max_bytes = 5 * 1024 * 1024  # 5MB
        try:
            log_dir = Path("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            self._disk_path = log_dir / "simian.log"
            # Rotate if existing file exceeds budget on startup.
            if self._disk_path.exists() and self._disk_path.stat().st_size > self._disk_max_bytes:
                rotated = log_dir / "simian.log.1"
                try:
                    if rotated.exists():
                        rotated.unlink()
                    self._disk_path.rename(rotated)
                except Exception:
                    pass
            self._disk_fh = open(self._disk_path, "a", encoding="utf-8", buffering=8192)
        except Exception:
            self._disk_fh = None
        # Pass Q: scroll-suspend flag. While the user is actively
        # scrolling the log textbox we suspend the auto-`see("end")`
        # so their viewport doesn't fight them. Re-armed by a release
        # event in SimianApp._build_logs (we don't have direct access
        # here so the flag is read-only by drain).
        self._scroll_locked = False
        self._schedule_drain()

    def _current_poll_ms(self) -> int:
        try:
            from services.settings_store import load_settings as _load_settings_ui
            if bool(_load_settings_ui().get("low_resource_mode", False)):
                return self._low_res_poll_ms
        except Exception:
            pass
        return self.poll_ms

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._queue.put(f"[{ts}] {msg}\n")
        self._schedule_drain()

    def _schedule_drain(self) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        try:
            self.root.after(self._current_poll_ms(), self._drain)
        except Exception:
            self._scheduled = False

    def _drain(self) -> None:
        self._scheduled = False
        if not self.textbox.winfo_exists():
            return

        # Perf instrumentation: measure the full drain wall-clock so we
        # can attribute real UI-thread time to log flushes when the
        # user reports lag. Bounded; the report line is itself routed
        # through the queue but only fires every 50 drains.
        _drain_start = time.perf_counter()

        items: list[str] = []
        for _ in range(self.batch_size):
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if items:
            joined = "".join(items)
            new_lines = joined.count("\n")
            # Pass Q: persistent disk mirror. Best-effort; failures are
            # silenced because losing a log line is preferable to taking
            # the GUI down. Rotation check lives here so it doesn't
            # need a dedicated thread.
            if self._disk_fh is not None:
                try:
                    self._disk_fh.write(joined)
                    self._disk_bytes_written += len(joined.encode("utf-8", errors="ignore"))
                    if self._disk_bytes_written > self._disk_max_bytes:
                        # Flush + close + rotate + reopen.
                        try:
                            self._disk_fh.flush()
                            self._disk_fh.close()
                        except Exception:
                            pass
                        try:
                            if self._disk_path is not None:
                                rotated = self._disk_path.with_suffix(".log.1")
                                if rotated.exists():
                                    rotated.unlink()
                                self._disk_path.rename(rotated)
                                self._disk_fh = open(self._disk_path, "a", encoding="utf-8", buffering=8192)
                        except Exception:
                            self._disk_fh = None
                        self._disk_bytes_written = 0
                except Exception:
                    pass
            self.textbox.configure(state="normal")
            self.textbox.insert("end", joined)
            self._line_count += new_lines
            if self._line_count > self.max_lines:
                try:
                    start_line = max(1, self._line_count - self.max_lines + 1)
                    self.textbox.delete("1.0", f"{start_line}.0")
                    self._line_count = self.max_lines
                except Exception:
                    try:
                        content = self.textbox.get("1.0", "end-1c")
                        lines = content.splitlines()
                        trimmed = lines[-self.max_lines:]
                        self.textbox.delete("1.0", "end")
                        self.textbox.insert("end", "\n".join(trimmed) + ("\n" if trimmed else ""))
                        self._line_count = len(trimmed)
                    except Exception:
                        pass
            # Pass Q: don't fight the user's scroll. When _scroll_locked
            # we skip see("end") so the viewport stays where they put
            # it; new lines still append, they just don't snap us back
            # to the bottom.
            if not self._scroll_locked:
                self.textbox.see("end")
            self.textbox.configure(state="disabled")

        # Track perf. Report one line every 50 drains so a slow
        # drain (widget full, trim active, heavy paint) is visible
        # without polluting the log.
        self._perf_drain_total_ms += (time.perf_counter() - _drain_start) * 1000.0
        self._perf_drain_count += 1
        if self._perf_drain_count >= 50:
            avg_ms = self._perf_drain_total_ms / max(1, self._perf_drain_count)
            if avg_ms >= 4.0:
                try:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._queue.put(
                        f"[{ts}] [Perf] UILogger flush avg "
                        f"{avg_ms:.1f}ms over {self._perf_drain_count} drains.\n"
                    )
                except Exception:
                    pass
            self._perf_drain_total_ms = 0.0
            self._perf_drain_count = 0

        self._schedule_drain()

def port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:
        return False


def sys_exe() -> str:
    return sys.executable


class SimianApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Simian — Project C.H.I.M.P")
        self.geometry("1100x760")
        ctk.set_appearance_mode("dark")

        self.settings: Settings = load_settings()
        self.task_runner = get_task_runner()

        # Telemetry throttling
        self._telemetry_last_ts = 0.0
        self._telemetry_min_interval = float(os.getenv("SIMIAN_TELEMETRY_INTERVAL", "10.0"))
        self._all_news_items: List[Any] = []
        self._news_status_text = ""
        self._news_filter_after_id: Any = None
        # Monotonic-ish wall-clock of last successful news fetch; used
        # by _on_tab_changed to decide whether to trigger a catch-up
        # refresh when the user comes back to the News tab after a
        # long absence. 0.0 = never fetched.
        self._news_last_refresh_ts: float = 0.0
        self._chat_history: List[tuple[str, str]] = []
        self._chat_inflight = False
        self._chat_mic_hot_mode = False
        self._selected_file_context: Optional[Dict[str, Any]] = None
        self._selected_file_summary = ""
        self._tts_lock = threading.Lock()
        self._clip_export_inflight = False
        self._ollama_proc: Optional[subprocess.Popen] = None
        self._closing = False
        self._startup_inflight = False
        self._replay_start_inflight = False
        # Holds the ``self.after(...)`` id for the deferred replay-buffer
        # autostart so _on_close can cancel it if the user quits during
        # the 45s warmup window. None when no autostart is pending.
        self._replay_autostart_after_id: Any = None

        # Layout. Row 0 hosts a small top bar (Pass O: global palette
        # button lives here so theme controls are reachable from every
        # tab without diving into Settings). Row 1 is the tabview.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.top_bar = ctk.CTkFrame(self, height=34, fg_color="transparent")
        self.top_bar.grid(row=0, column=0, sticky="new", padx=14, pady=(8, 0))
        self.top_bar.grid_columnconfigure(0, weight=1)
        self.theme_btn = ctk.CTkButton(
            self.top_bar,
            text="Theme",
            width=96,
            command=self._open_theme_popup,
        )
        self.theme_btn.grid(row=0, column=1, sticky="e", padx=2, pady=2)

        # ``command=`` fires on every tab switch. We use it to wake the
        # SRM tick when the user returns to 4D Lab -- the tick pauses
        # itself entirely when hidden to avoid ANY redraw cost on tab
        # switches (the previous 250ms slow-tick still showed up as
        # micro-stutters in the profiler).
        self.tabs = ctk.CTkTabview(self, command=self._on_tab_changed)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=12, pady=12)

        self.tab_chat = self.tabs.add("Chat")
        self.tab_clips = self.tabs.add("Clips")
        self.tab_files = self.tabs.add("Files")
        self.tab_news = self.tabs.add("World News")
        self.tab_4d = self.tabs.add("4D Lab")
        self.tab_services = self.tabs.add("Services")
        self.tab_settings = self.tabs.add("Settings")
        self.tab_logs = self.tabs.add("Logs")

        # Logs
        self.log_box = ctk.CTkTextbox(self.tab_logs, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box.configure(state="disabled")
        # Pass Q: cap the visible textbox to 400 lines (down from 700)
        # because we now mirror the full session log to logs/simian.log
        # for postmortem. Lower cap means cheaper trim at every flush
        # and visibly less paint cost when the log is hot.
        self._ui_logger = UILogger(self, self.log_box, max_lines=400, poll_ms=140, batch_size=100)
        # Pass Q: scroll-lock plumbing. When the user touches the log
        # scrollbar (mousewheel or drag) we suspend the see("end")
        # auto-snap. ButtonRelease re-arms after a 1.2s grace so a
        # quick scroll-and-let-go doesn't permanently freeze the
        # auto-follow behaviour.
        try:
            self._log_scroll_relock_after_id: Any = None
            self.log_box.bind("<MouseWheel>", self._on_log_scroll, add="+")
            self.log_box.bind("<Button-4>", self._on_log_scroll, add="+")  # x11 scroll up
            self.log_box.bind("<Button-5>", self._on_log_scroll, add="+")  # x11 scroll down
            self.log_box.bind("<ButtonRelease-1>", self._on_log_scroll_release, add="+")
            self.log_box.bind("<KeyPress>", self._on_log_scroll, add="+")
        except Exception:
            pass
        # Voice-log backpressure state. Hot-mic / Vosk emits a log line
        # for every chunk of partial speech, every "busy, ignored"
        # event, and every held-transcript retry. On a talkative user
        # that is 10-20+ lines/second which rides the UI flush cadence
        # hard even with the bounded queue. _log_dedup wraps the raw
        # UILogger.log and collapses runs of the same noisy key into a
        # single "(suppressed N similar lines)" epilogue.
        self._log_dedup_last_key: str = ""
        self._log_dedup_last_ts: float = 0.0
        self._log_dedup_suppressed: int = 0
        self._log_dedup_window_s: float = 2.0
        self.log = self._log_dedup
        self.log("GUI ready.")

        # Services / state
        self.api_proc: Optional[subprocess.Popen] = None
        # Pass R-C: hand the replay recorder pause/resume hooks so the
        # AudioFallbackRecorder can grab the mic without fighting STT
        # for exclusive PortAudio access on the same default device.
        self.replay = ReplayBufferRecorder(
            log_cb=self.log,
            stt_pause_cb=self._pause_mic_listener_for_replay,
            stt_resume_cb=self._resume_mic_listener_after_replay,
        )
        self.mic_listener: Any = None
        # Screen Awareness singleton. Wired early so health checks work
        # before the Settings tab is built. Flag is restored from saved
        # settings below; service stays off until the user opts in.
        self._sync_screen_awareness_from_settings()

        self.srm_running = False
        self.srm_theta = 0.0
        self.srm_phi = 0.0
        self.srm_sigma = 0.0
        self._srm_points: List[List[float]] = []

        # Lazy tab hydration. Chat is the default tab the user sees on
        # launch, so we build it inline. Clips / Files / Services /
        # Settings are small but referenced by _poll_status or save
        # paths, so they still build inline for safety. The heavier
        # tabs (News fetches, 4D canvas) defer via self.after so the
        # main thread can paint the window first. _on_tab_changed
        # force-builds a deferred tab if the user clicks into it
        # before its scheduled build fires, so the user never sees a
        # blank tab.
        self._news_tab_built: bool = False
        self._fourd_tab_built: bool = False
        self._startup_t0: float = time.perf_counter()
        self._build_chat()
        self._build_clips()
        self._build_files()
        # Heavier tabs deferred (see _lazy_build_*).
        self._build_services()
        self._build_settings()
        self._apply_accent_color()
        # Schedule the deferred builds. 220ms / 480ms spreads them out
        # over the startup paint window so neither the first paint nor
        # the first input event is blocked by a heavy layout pass.
        self.after(220, self._lazy_build_news)
        self.after(480, self._lazy_build_4d)

        # Auto-start behaviors
        startup_delay = max(1200, int(getattr(self.settings, "safe_startup_delay_ms", 2500) or 2500))
        self.after(startup_delay, self._auto_start)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # Keys identify classes of noisy voice log lines we want to
    # debounce. Any message that STARTS with one of these keys (after
    # skipping the [Voice] prefix) is grouped; the first one in a
    # 2s window goes through untouched, any follow-ups in the same
    # window with the same key are suppressed, and on key-change or
    # window-expiry we emit one summary line "(suppressed N similar
    # log lines)" so the user can see something was squelched.
    _LOG_DEDUP_KEYS = (
        "[Voice] Heard:",
        "[Voice] Waiting for more speech",
        "[Voice] Busy, ignored transcript",
        "[Voice] Partial:",
    )

    def _log_dedup_key_for(self, msg: str) -> str:
        m = (msg or "").lstrip()
        for k in self._LOG_DEDUP_KEYS:
            if m.startswith(k):
                return k
        return ""

    def _on_log_scroll(self, _event: Any = None) -> None:
        """Pass Q: enter scroll-lock so the log textbox stops fighting
        the user's manual scrolling. Re-armed by _on_log_scroll_release
        or by an idle timer after 1.2s of no further scroll input."""
        try:
            self._ui_logger._scroll_locked = True
        except Exception:
            return
        # Schedule a re-unlock so a single wheel event doesn't pin us.
        try:
            if self._log_scroll_relock_after_id is not None:
                self.after_cancel(self._log_scroll_relock_after_id)
        except Exception:
            pass
        try:
            self._log_scroll_relock_after_id = self.after(1200, self._on_log_scroll_release)
        except Exception:
            self._log_scroll_relock_after_id = None

    def _on_log_scroll_release(self, _event: Any = None) -> None:
        try:
            self._ui_logger._scroll_locked = False
        except Exception:
            pass
        self._log_scroll_relock_after_id = None

    def _log_dedup(self, msg: str) -> None:
        """Backpressure wrapper for ``self._ui_logger.log``.

        Cheap: a single startswith scan against 4 keys and a wall-clock
        compare. When nothing matches we fall straight through to the
        underlying bounded-queue logger, so normal log traffic is
        unaffected. Only the voice spam keys get collapsed.
        """
        try:
            key = self._log_dedup_key_for(msg)
        except Exception:
            key = ""
        if not key:
            # Non-noisy line. If we were suppressing, flush the summary
            # first so the user sees the collapse before the new line.
            if self._log_dedup_suppressed > 0 and self._log_dedup_last_key:
                try:
                    self._ui_logger.log(
                        f"{self._log_dedup_last_key} (suppressed "
                        f"{self._log_dedup_suppressed} similar log lines)"
                    )
                except Exception:
                    pass
                self._log_dedup_suppressed = 0
                self._log_dedup_last_key = ""
            try:
                self._ui_logger.log(msg)
            except Exception:
                pass
            return

        now = time.time()
        same_key = (key == self._log_dedup_last_key)
        in_window = (now - self._log_dedup_last_ts) < self._log_dedup_window_s

        if same_key and in_window:
            # Suppress. Refresh the window so a sustained burst stays
            # collapsed instead of stuttering through every 2s.
            self._log_dedup_suppressed += 1
            self._log_dedup_last_ts = now
            return

        # Different key or window expired -- flush any pending summary.
        if self._log_dedup_suppressed > 0 and self._log_dedup_last_key:
            try:
                self._ui_logger.log(
                    f"{self._log_dedup_last_key} (suppressed "
                    f"{self._log_dedup_suppressed} similar log lines)"
                )
            except Exception:
                pass

        self._log_dedup_last_key = key
        self._log_dedup_last_ts = now
        self._log_dedup_suppressed = 0
        try:
            self._ui_logger.log(msg)
        except Exception:
            pass

    def _chat_reply(self, text: str) -> None:
        self._remember_chat("assistant", text)
        self._chat_append("Simian", text)
        self._speak(text)

    # Markdown-ish scrubs applied ONLY to the spoken copy. Visible chat
    # text is written by ``_chat_append`` and is never rewritten by any
    # of these helpers -- formatting users put on the screen stays on
    # the screen; only the bytes handed to the TTS engine are cleaned.
    _TTS_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _TTS_HR_RE = re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
    _TTS_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
    _TTS_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*+\u2022\u25AA\u25CF]|\d+[.)])\s+")
    _TTS_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{1,3})([^*_\n][^*_\n]*?)\1")
    _TTS_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
    _TTS_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
    _TTS_STAR_RESIDUE_RE = re.compile(r"\*+")
    _TTS_UNDERSCORE_RESIDUE_RE = re.compile(r"(?<!\w)_+(?!\w)")
    _TTS_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")

    def _tts_sanitize(self, text: str) -> str:
        """Return a spoken-form copy of ``text`` with markdown stripped.

        Visible chat text is never passed through this function, so the
        rendered message keeps its bullets / bold / code samples intact.
        Only the string that's about to go to the TTS backend is cleaned,
        so Simian stops literally announcing ``asterisk`` / ``hash`` /
        bullet markers that were only meant as visual formatting.

        Intentionally conservative: punctuation that carries prosody
        (``.``, ``,``, ``;``, ``:``, ``?``, ``!``) is preserved so the
        voice still sounds natural on lists and headings.
        """
        if not text:
            return ""
        s = str(text)
        # Drop fenced code blocks entirely -- reading source out loud is
        # useless noise. Inline code keeps its content.
        s = self._TTS_CODE_FENCE_RE.sub(" ", s)
        s = self._TTS_HR_RE.sub(" ", s)
        s = self._TTS_HEADING_RE.sub("", s)
        s = self._TTS_BULLET_RE.sub("", s)
        s = self._TTS_LINK_RE.sub(r"\1", s)
        s = self._TTS_EMPHASIS_RE.sub(r"\2", s)
        s = self._TTS_INLINE_CODE_RE.sub(r"\1", s)
        # Any residual decorative symbols users sometimes leave behind.
        s = self._TTS_STAR_RESIDUE_RE.sub("", s)
        s = self._TTS_UNDERSCORE_RESIDUE_RE.sub("", s)
        # Collapse whitespace but keep paragraph breaks so the chunker
        # can still split at meaningful boundaries.
        s = re.sub(r"[ \t\u00A0]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    def _tts_split_for_speech(self, text: str, max_chunk: int = 260) -> List[str]:
        """Split ``text`` into speakable chunks no longer than ``max_chunk``.

        Prefers sentence-end boundaries, falls back to whitespace, never
        drops content. Used to drive sequential TTS playback so long
        chat replies are spoken in full instead of getting truncated at
        a hard character limit.
        """
        text = (text or "").strip()
        if not text:
            return []
        parts = [p.strip() for p in self._TTS_SENT_SPLIT_RE.split(text) if p and p.strip()]

        merged: List[str] = []
        buf = ""
        for p in parts:
            if not buf:
                buf = p
                continue
            if len(buf) + 1 + len(p) <= max_chunk:
                buf = f"{buf} {p}"
            else:
                merged.append(buf)
                buf = p
        if buf:
            merged.append(buf)

        # Hard-split anything still too long (single giant sentence) on
        # the last whitespace boundary under ``max_chunk``. Never truncate.
        out: List[str] = []
        for c in merged:
            while len(c) > max_chunk:
                split = c.rfind(" ", 0, max_chunk)
                if split <= 0:
                    split = max_chunk
                out.append(c[:split].strip())
                c = c[split:].strip()
            if c:
                out.append(c)
        return out

    def _trim_textbox(self, textbox: Any, max_chars: int = 12000) -> None:
        try:
            content = textbox.get("1.0", "end-1c")
            if len(content) > max_chars:
                textbox.delete("1.0", f"end-{max_chars}c")
        except Exception:
            pass

    def _selected_file_prompt_block(self) -> str:
        ctx = self._selected_file_context or {}
        if not ctx:
            return ""
        details = ctx.get("details") or {}
        detail_lines = []
        for k in ("name", "ext", "image_size", "image_mode", "note"):
            if k in details and details[k] not in (None, ""):
                detail_lines.append(f"- {k}: {details[k]}")
        detail_text = "\n".join(detail_lines)
        return (
            "\n[Selected file context]\n"
            f"Path: {ctx.get('path','')}\n"
            f"Mime: {ctx.get('mime','')}\n"
            f"Summary: {ctx.get('summary','')}\n"
            f"Details:\n{detail_text}\n"
            "Use this only as the current local file context. If the user asks to modify the file, explain the next concrete step or produce the edited output path if a tool supports it.\n"
        )

    def _resolve_audio_device(self, raw: Any, *, kind: str) -> Optional[int]:
        """Resolve a saved audio device setting to a valid sounddevice index.

        Tolerates the full range of values the settings store may hold:
          * None / ""  / "default"            -> system default (None)
          * int or numeric string ("25")      -> validated by index
          * combo strings ("25|Realtek ...")  -> try index first, then name
          * bare exact device name            -> matched against enumeration

        Any saved value that no longer corresponds to a usable device
        (disconnected, reindexed, or with zero channels of the required
        kind) falls back cleanly to None with a single actionable log
        line — so callers never hand PortAudio a stale id and get a raw
        PaError surfaced to the console.

        ``kind`` is "input" (STT) or "output" (TTS) and decides which
        channel-count attribute is required to be > 0.
        """
        if raw in (None, "", "default"):
            return None
        try:
            import sounddevice as sd  # type: ignore
        except Exception:
            # sounddevice not importable — can't validate; let the caller
            # use the system default.
            return None

        raw_str = str(raw).strip()
        if not raw_str or raw_str.lower() == "default":
            return None
        head = raw_str.split("|", 1)[0].strip()
        name_token = raw_str.split("|", 1)[1].strip() if "|" in raw_str else raw_str

        try:
            devices = list(sd.query_devices())
        except Exception as e:
            self.log(f"[Audio] Device enumeration failed ({kind}): {e}")
            return None

        channels_attr = "max_input_channels" if kind == "input" else "max_output_channels"

        def _usable(idx: int) -> bool:
            if idx < 0 or idx >= len(devices):
                return False
            try:
                return int(devices[idx].get(channels_attr, 0) or 0) > 0
            except Exception:
                return False

        # 1) Try the leading token as a numeric index.
        try:
            idx = int(head)
            if _usable(idx):
                return idx
            self.log(
                f"[Audio] Saved {kind} device id {idx} is not available; "
                f"using system default."
            )
        except ValueError:
            pass

        # 2) Try exact (then case-insensitive) device-name match against
        # the name token and, if no '|' was present, the whole string.
        candidates = [name_token]
        if "|" not in raw_str:
            candidates.append(raw_str)
        for cand in candidates:
            cand = (cand or "").strip()
            if not cand:
                continue
            for i, info in enumerate(devices):
                if str(info.get("name", "")) == cand and _usable(i):
                    return i
            low = cand.lower()
            for i, info in enumerate(devices):
                if str(info.get("name", "")).lower() == low and _usable(i):
                    return i

        self.log(
            f"[Audio] Saved {kind} device {raw_str!r} did not match any "
            f"available device; using system default."
        )
        return None

    def _get_selected_output_device(self) -> Optional[int]:
        return self._resolve_audio_device(
            getattr(self.settings, "tts_output_device", None), kind="output"
        )

    def _get_selected_input_device(self) -> Optional[int]:
        return self._resolve_audio_device(
            getattr(self.settings, "stt_input_device", None), kind="input"
        )

    def _ensure_ollama_running(self, timeout: float = 8.0) -> bool:
        if port_in_use("127.0.0.1", 11434):
            return True
        cmd = (os.environ.get("SIMIAN_OLLAMA_CMD") or "ollama serve").strip()
        try:
            self.log(f"[Chat] Starting Ollama backend: {cmd}")
            self._ollama_proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.log(f"[Chat] Could not start Ollama automatically: {e}")
            return False
        end = time.time() + timeout
        while time.time() < end:
            if port_in_use("127.0.0.1", 11434):
                return True
            time.sleep(0.4)
        return port_in_use("127.0.0.1", 11434)

    def _mp3_to_wav(self, mp3_path: str) -> Optional[str]:
        """Transcode MP3 -> sibling WAV via ffmpeg with no console window.

        Used only on the active TTS playback path so MP3-format edge-tts
        output can be routed through the existing silent WAV backends
        (sounddevice / simpleaudio / winsound) instead of being shell-opened
        in a foreground media player. Returns the wav path on success,
        or None on any failure (caller should degrade cleanly).
        """
        try:
            src = Path(mp3_path)
            if not src.exists() or src.stat().st_size == 0:
                return None
            dst = src.with_suffix(".wav")
            try:
                from services.replay_buffer import _find_ffmpeg
                ffmpeg_exe = _find_ffmpeg()
            except Exception:
                ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                [ffmpeg_exe, "-nostdin", "-y", "-loglevel", "error",
                 "-i", str(src), "-f", "wav", str(dst)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                check=True,
            )
            if dst.exists() and dst.stat().st_size > 512:
                return str(dst)
        except Exception as e:
            self.log(f"[TTS] ffmpeg mp3->wav decode failed: {e}")
        return None

    def _play_audio_file(self, path: str) -> None:
        if not path:
            return
        ext = Path(path).suffix.lower()
        output_device = self._get_selected_output_device()

        # edge-tts emits MP3-encoded audio. None of the silent in-process
        # WAV backends below can decode MP3, so transcode first via ffmpeg
        # (no console window) and fall through to the WAV ladder. This
        # replaces a previous os.startfile() fallback that opened a
        # foreground Media Player / Movies & TV window for MP3 payloads.
        if ext == ".mp3":
            wav_path = self._mp3_to_wav(path)
            if wav_path is None:
                self.log("[TTS] MP3 decode unavailable; playback skipped to avoid foreground media player.")
                return
            path = wav_path
            ext = ".wav"

        if ext == ".wav":
            try:
                import wave
                import numpy as np  # type: ignore
                import sounddevice as sd  # type: ignore

                with wave.open(path, "rb") as wf:
                    channels = wf.getnchannels()
                    samplerate = wf.getframerate()
                    sampwidth = wf.getsampwidth()
                    frames = wf.readframes(wf.getnframes())
                dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth)
                if dtype is not None:
                    data = np.frombuffer(frames, dtype=dtype)
                    if channels > 1:
                        data = data.reshape(-1, channels)
                    sd.play(data, samplerate=samplerate, device=output_device)
                    sd.wait()
                    return
            except Exception as e:
                self.log(f"[TTS] sounddevice playback failed: {e}")

            try:
                import simpleaudio as sa  # type: ignore
                play = sa.WaveObject.from_wave_file(path).play()
                play.wait_done()
                return
            except Exception:
                pass

        if os.name == "nt" and ext == ".wav":
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                return
            except Exception as e:
                self.log(f"[TTS] winsound playback failed: {e}")
        if os.name == "nt" and ext == ".wav":
            try:
                ps_path = path.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName System; "
                    f"$p = New-Object System.Media.SoundPlayer '{ps_path}'; "
                    "$p.PlaySync()"
                )
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                return
            except Exception as e:
                self.log(f"[TTS] PowerShell playback failed: {e}")

        # Intentionally no os.startfile() fallback on the active TTS path:
        # shell-opening an audio file launches a foreground media player
        # (Movies & TV / Windows Media Player), which violates the
        # background-only TTS requirement. Degrade cleanly instead.
        self.log("[TTS] No silent audio backend available; playback skipped.")

    def _speak(self, text: str) -> None:

        if not getattr(self.settings, "voice_enabled", False):
            return
        # Sanitize (strip markdown artifacts) and chunk for sequential
        # playback. The previous implementation applied a hard 360-char
        # truncation with "..." that cut long responses off mid-thought;
        # here we keep the full response and hand it to the backend as
        # sentence-aware chunks so playback finishes cleanly without
        # blocking the GUI.
        cleaned = self._tts_sanitize(text)
        chunks = self._tts_split_for_speech(cleaned)
        if not chunks:
            return

        voice_id = getattr(self.settings, "voice_id", None) or "en-US-GuyNeural"

        def _speak_one(chunk: str) -> None:
            if callable(_service_tts_to_file):
                try:
                    path = str(_service_tts_to_file(chunk, voice=voice_id))
                    self.log(f"[TTS] Audio ready: {path}")
                    self._play_audio_file(path)
                    return
                except Exception as e:
                    self.log(f"[TTS] service synthesis failed: {e}")

            speak_fn = _edge_speak_text
            if callable(speak_fn):
                try:
                    speak_fn(chunk, voice=voice_id)
                    return
                except TypeError:
                    try:
                        speak_fn(chunk)
                        return
                    except Exception as e:
                        self.log(f"[TTS] voice backend failed: {e}")
                except Exception as e:
                    self.log(f"[TTS] voice backend failed: {e}")

            if os.name == "nt":
                try:
                    safe_text = chunk.replace("'", "''")
                    ps = (
                        "Add-Type -AssemblyName System.Speech; "
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        f"$s.Speak('{safe_text}')"
                    )
                    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception as e:
                    self.log(f"[TTS] PowerShell fallback failed: {e}")

            try:
                import pyttsx3
                engine = pyttsx3.init()
                engine.say(chunk)
                engine.runAndWait()
            except Exception as e:
                self.log(f"[TTS] No working TTS backend: {e}")

        def worker() -> None:
            # Sequential playback under the existing lock so a second
            # `_speak()` call while we're mid-response queues cleanly
            # behind the first instead of producing overlapping audio.
            # Each chunk goes through the same backend ladder the
            # single-shot version used, so there's no new foreground
            # media-player path and the GUI thread is never blocked.
            with self._tts_lock:
                total = len(chunks)
                if total > 1:
                    self.log(f"[TTS] Speaking {total} chunks sequentially.")
                # Telemetry: speech started. Best-effort; never raises.
                try:
                    from services.four_d_telemetry import telemetry as _four_d_t
                    _four_d_t.emit("tts", "start", chunks=total)
                except Exception:
                    pass
                for idx, chunk in enumerate(chunks, 1):
                    try:
                        _speak_one(chunk)
                    except Exception as e:
                        # A single bad chunk must not abort the rest of
                        # the reply. Log and carry on so the user still
                        # hears the remainder of the response.
                        self.log(f"[TTS] chunk {idx}/{total} failed: {e}")
                if total > 1:
                    self.log(f"[TTS] Finished speaking all {total} chunks.")
                try:
                    from services.four_d_telemetry import telemetry as _four_d_t
                    _four_d_t.emit("tts", "done", chunks=total)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _remember_chat(self, role: str, text: str) -> None:
        msg = (text or "").strip()
        if not msg:
            return
        self._chat_history.append((role, msg))
        self._chat_history = self._chat_history[-12:]

    def _set_chat_busy(self, busy: bool) -> None:
        self._chat_inflight = busy
        state = "disabled" if busy else "normal"
        try:
            self.chat_send_btn.configure(state=state)
            self.chat_entry.configure(state=state)
        except Exception:
            pass
        try:
            if busy:
                self.chat_mic_btn.configure(text="Busy...")
            else:
                self._sync_chat_mic_controls()
        except Exception:
            pass

    def _build_prompt_from_history(self) -> str:
        lines: List[str] = []
        for role, msg in self._chat_history[-12:]:
            prefix = "User" if role == "user" else "Simian"
            lines.append(f"{prefix}: {msg}")
        block = self._selected_file_prompt_block()
        if block:
            lines.append(block)
        return "\n".join(lines)

    def _handle_local_query(self, text: str) -> Optional[str]:
        cleaned = (text or "").strip()
        # Pass S-C: time/date queries answer from the OS clock so the
        # model can't invent times. We try this BEFORE health queries
        # (cheap regex) and BEFORE any LLM call. Logs a single
        # [Time] line so the user can see the local-clock provider
        # actually short-circuited.
        try:
            from services.local_clock import maybe_answer as _local_clock_answer
        except Exception as exc:
            self.log(f"[Time] local_clock import failed ({exc}); falling through.")
            _local_clock_answer = None  # type: ignore[assignment]
        if _local_clock_answer is not None:
            answer = _local_clock_answer(cleaned)
            if answer:
                self.log("[Time] Answered from local system clock")
                return answer
        if HEALTH_QUERY_RE.search(cleaned):
            return self._get_system_health_summary()
        return None

    def _get_system_health_summary(self) -> str:
        import platform
        import shutil

        cpu = mem = temp = "unavailable"
        try:
            import psutil  # type: ignore
            cpu = f"{psutil.cpu_percent(interval=0.2)}%"
            vm = psutil.virtual_memory()
            mem = f"{vm.percent}% used ({round(vm.used/1024**3,1)} / {round(vm.total/1024**3,1)} GB)"
            try:
                sensor_fn = getattr(psutil, "sensors_temperatures", None)
                if callable(sensor_fn):
                    temps = sensor_fn()
                    if temps:
                        first = next(iter(temps.values()))
                        if first:
                            temp = f"{getattr(first[0], 'current', 'unavailable')}°C"
            except Exception:
                pass
        except Exception:
            pass

        disk = shutil.disk_usage(str(REPO_ROOT))
        disk_text = f"{round(disk.free/1024**3,1)} GB free / {round(disk.total/1024**3,1)} GB total"
        net_text = "online" if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT) else "API offline / network unknown"
        return (
            "Quick local health rundown:\n"
            f"- Host: {platform.node() or 'local machine'}\n"
            f"- CPU usage: {cpu}\n"
            f"- Memory usage: {mem}\n"
            f"- Disk space: {disk_text}\n"
            f"- Network/API status: {net_text}\n"
            f"- Temperature: {temp}"
        )
    # ----------------------- AUTO START -----------------------

    def _warm_backends(self) -> None:
        if self._closing:
            return
        if not bool(getattr(self.settings, "warm_backends_on_launch", False)):
            self.log("[Startup] Backend warmup skipped by settings.")
            return
        if not bool(getattr(self.settings, "auto_start_ollama", True)):
            self.log("[Startup] Ollama auto-start is disabled; skipping warmup.")
            return
        try:
            self._ensure_ollama_running(timeout=8.0)
        except Exception as e:
            self.log(f"[Startup] Ollama warmup skipped: {e}")
        try:
            from services.image_gen import ensure_image_backend_ready  # type: ignore
            ensure_image_backend_ready()
        except Exception as e:
            self.log(f"[Startup] Image backend warmup skipped: {e}")

    def _request_start_replay_buffer(self) -> None:
        if self._replay_start_inflight:
            self.log("[Replay] Replay start already in progress.")
            return
        self._replay_start_inflight = True

        def worker() -> None:
            try:
                self._start_replay_buffer()
            finally:
                self._replay_start_inflight = False

        threading.Thread(target=worker, name="SimianReplayStart", daemon=True).start()

    def _auto_start(self) -> None:
        if self._closing or self._startup_inflight:
            return
        self._startup_inflight = True
        # Total window-visible-to-staged-startup elapsed, measured
        # from SimianApp.__init__. Visible every launch so regressions
        # jump out in the log.
        try:
            dt_ms = (time.perf_counter() - getattr(self, "_startup_t0", time.perf_counter())) * 1000.0
            self.log(f"[Perf] Startup reached _auto_start at +{dt_ms:.0f}ms.")
        except Exception:
            pass
        self.log("[Startup] Beginning safe staged startup.")

        if bool(getattr(self.settings, "warm_backends_on_launch", False)):
            self.after(1200, lambda: threading.Thread(target=self._warm_backends, name="SimianWarmBackends", daemon=True).start())
        else:
            self.log("[Startup] Backend warmup is disabled.")

        if bool(getattr(self.settings, "auto_start_mic", False)) and getattr(self.settings, "stt_enabled", True):
            self.after(2200, lambda: threading.Thread(target=lambda: self._start_mic_listener(hot_mode=False), name="SimianAutoStartMic", daemon=True).start())
        else:
            self.log("[Startup] Mic listener auto-start is disabled.")

        if bool(getattr(self.settings, "auto_start_replay", False)):
            # Defer replay-buffer autostart to give heavier subsystems
            # (Ollama warmup, mic listener, vision model, news refresh)
            # room to settle before FFmpeg claims the screen + audio
            # capture pipe. Default is 45000ms; tunable via settings.
            try:
                delay_ms = int(getattr(self.settings, "replay_autostart_delay_ms", 45000) or 45000)
            except Exception:
                delay_ms = 45000
            delay_ms = max(0, delay_ms)
            self.log(
                f"[Startup] Replay buffer auto-start scheduled in {delay_ms} ms."
            )
            self._replay_autostart_after_id = self.after(
                delay_ms, self._maybe_autostart_replay_buffer
            )
        else:
            self.log("[Startup] Replay buffer auto-start is disabled.")

        self.after(5200, lambda: self._schedule_news_refresh(initial=True))
        self._startup_inflight = False

    def _maybe_autostart_replay_buffer(self) -> None:
        """Deferred replay-buffer autostart entry point.

        Runs on the Tk main thread after the ``replay_autostart_delay_ms``
        budget elapses. Guarded against every common "don't start now"
        condition: the user closed the window during the wait, the
        setting got toggled off in Settings, the buffer is already up,
        or a manual Start click already fired. Cancellation in
        _on_close clears the tracked after-id, but we re-check every
        invariant here as belt-and-suspenders."""
        self._replay_autostart_after_id = None
        if self._closing:
            return
        if not bool(getattr(self.settings, "auto_start_replay", False)):
            self.log("[Startup] Deferred replay autostart skipped: setting now disabled.")
            return
        if self._replay_start_inflight:
            self.log("[Startup] Deferred replay autostart skipped: start already in flight.")
            return
        try:
            if self.replay.is_running():
                self.log("[Startup] Deferred replay autostart skipped: buffer already running.")
                return
        except Exception:
            pass
        self.log("[Startup] Deferred replay autostart firing now.")
        try:
            self._request_start_replay_buffer()
        except Exception as e:
            self.log(f"[Startup] Deferred replay autostart failed cleanly: {e}")
    # ----------------------- CHAT -----------------------


    def _build_chat(self) -> None:
        # TODO(chat-drag-drop): hook a tkinterdnd2 drop target on the
        # chat frame so users can drag files from Explorer into chat and
        # have them summarized/attached as a selected-file context. Must
        # be optional (tkinterdnd2 is not a hard dep) and must not break
        # the stock CTk parent when the dep is missing.
        frame = ctk.CTkFrame(self.tab_chat)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.chat_log = ctk.CTkTextbox(frame, wrap="word")
        self.chat_log.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=8, pady=8)
        self.chat_log.configure(state="disabled")

        self.chat_entry = ctk.CTkEntry(frame, placeholder_text="Ask Simian...")
        self.chat_entry.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.chat_entry.bind("<Return>", lambda _e: self._send_chat())
        # Chat attachment scaffold (Pass K): Ctrl+O opens a file picker
        # and wires the chosen file into `_selected_file_context`, the
        # same channel the Files-tab "Use for chat" button already uses.
        # Full drag-and-drop onto the chat input requires TkinterDnD2
        # and is tracked in the Flourishin backlog; this binding gives
        # the workflow a keyboard entry point without adding a new
        # visible UI element or changing the styling.
        self.chat_entry.bind("<Control-o>", lambda _e: self._attach_file_for_chat())
        self.chat_entry.bind("<Control-O>", lambda _e: self._attach_file_for_chat())

        self.chat_mic_btn = ctk.CTkButton(frame, text="Mic Off", width=110, command=self._toggle_chat_mic)
        self.chat_mic_btn.grid(row=1, column=1, padx=8, pady=8)

        self.chat_send_btn = ctk.CTkButton(frame, text="Send", command=self._send_chat)
        self.chat_send_btn.grid(row=1, column=2, padx=8, pady=8)
        ctk.CTkButton(frame, text="Clear", command=self._clear_chat).grid(row=1, column=3, padx=8, pady=8)

        self.chat_voice_hint = ctk.CTkLabel(frame, text="Voice: say 'Simian ...' after turning the mic on.")
        self.chat_voice_hint.grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

        self._chat_append("Simian", "Online. (Chat uses Ollama if available.)")
        self._sync_chat_mic_controls()

    def _chat_append(self, who: str, msg: str) -> None:
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", f"{who}: {msg}\n\n")
        self._trim_textbox(self.chat_log, max_chars=22000)
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def _clear_chat(self) -> None:
        self._chat_history = []
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")

    def _attach_file_for_chat(self) -> None:
        """Pass K scaffold: pick a file and load it into chat context.

        Opens a standard tkinter file dialog. On selection, populates
        ``self._selected_file_context`` (the same channel the Files tab
        already uses) with path + short summary so the next chat turn
        sees the file as attached. Errors are swallowed and logged --
        a cancelled dialog or a broken file must not take the GUI
        down.

        This is deliberately a keyboard entry point (Ctrl+O on the
        chat entry); true drag-and-drop onto the chat input is
        tracked in the Flourishin backlog.
        """
        try:
            from tkinter import filedialog
            fp = filedialog.askopenfilename(
                title="Attach file to chat",
                filetypes=[("All files", "*.*")],
            )
        except Exception as exc:
            self.log(f"[Chat] Attach dialog failed: {exc}")
            return
        if not fp:
            return
        try:
            # Prefer the existing file_scanner summary pipeline when
            # available; otherwise fall back to a short head-of-file
            # preview so the chat still has SOMETHING to send.
            summary = ""
            try:
                from services.file_scanner import summarize_file_for_context  # type: ignore
                summary = str(summarize_file_for_context(fp) or "")
            except Exception:
                pass
            if not summary:
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        summary = fh.read(4000)
                except Exception:
                    summary = "(binary or unreadable file; path attached as reference only)"
            self._selected_file_context = {"path": fp, "summary": summary}
            self._selected_file_summary = summary
            try:
                from os.path import basename
                self._chat_append("Simian", f"Attached '{basename(fp)}' to the next chat turn.")
            except Exception:
                pass
            try:
                from services.four_d_telemetry import telemetry as _four_d_t
                _four_d_t.emit("chat", "file_attached", path=fp, bytes=len(summary or ""))
            except Exception:
                pass
        except Exception as exc:
            self.log(f"[Chat] Attach failed: {exc}")

    def _toggle_chat_mic(self) -> None:
        """Cycle the mic button through the three documented states.

        Pass R-B: the previous toggle only flipped between Off and Hot
        Mic, which hid the Wake Word state from users entirely (it only
        ever appeared when auto_start_mic was on at launch). The new
        cycle is Off -> Wake Word -> Hot Mic -> Off so the button
        surfaces all three modes the project brief calls out.
        """
        listener = self.mic_listener
        running = listener is not None and getattr(listener, "is_running", lambda: False)()
        current = "off"
        if running:
            current = "hot" if self._chat_mic_hot_mode else "wake"

        if current == "off":
            next_state = "wake"
            self._chat_mic_hot_mode = False
            self.log("[Voice] Mode change: off -> wake word.")
            self._start_mic_listener(hot_mode=False)
        elif current == "wake":
            next_state = "hot"
            self._chat_mic_hot_mode = True
            self.log("[Voice] Mode change: wake word -> hot mic.")
            # Listener is already running -- just flip hot_mode without
            # tearing down the input stream so we don't drop audio mid-
            # transition. _start_mic_listener handles the case where the
            # listener died unexpectedly (it'll restart cleanly).
            if listener is not None and running and hasattr(listener, "set_hot_mode"):
                try:
                    listener.set_hot_mode(True)
                except Exception:
                    self._start_mic_listener(hot_mode=True)
            else:
                self._start_mic_listener(hot_mode=True)
        else:  # current == "hot"
            next_state = "off"
            self._chat_mic_hot_mode = False
            self.log("[Voice] Mode change: hot mic -> off.")
            self._stop_mic_listener()

        # Belt-and-suspenders: keep the public flag mirrored to the new
        # state in case downstream code reads it before the next sync.
        self._chat_mic_hot_mode = (next_state == "hot")
        self._sync_chat_mic_controls()

    def _sync_chat_mic_controls(self) -> None:
        running = False
        listener = getattr(self, "mic_listener", None)
        if listener is not None:
            running = bool(getattr(listener, "is_running", lambda: False)())
        # Three-state UI label: Off / Wake Word / Hot Mic. Matches the
        # project brief's documented modes and the click cycle in
        # _toggle_chat_mic.
        if hasattr(self, "chat_mic_btn"):
            if running and self._chat_mic_hot_mode:
                label = "Hot Mic"
            elif running:
                label = "Wake Word"
            else:
                label = "Mic Off"
            try:
                self.chat_mic_btn.configure(text=label)
            except Exception:
                pass
        if hasattr(self, "chat_voice_hint"):
            if running and self._chat_mic_hot_mode:
                self.chat_voice_hint.configure(text="Voice: hot mic is on. Speak naturally, or say clip that.")
            elif running:
                self.chat_voice_hint.configure(text="Voice: wake-word listener is on. Say 'Simian ...' to talk, or click for hot mic.")
            else:
                self.chat_voice_hint.configure(text="Voice: mic is off. Click the mic button to enable wake word.")

    def _on_voice_transcript(self, text: str, meta: Dict[str, Any]) -> None:
        # Pass R-D: every transcript that reaches the GUI must end in
        # exactly one accept-or-reject log line, so users can trace why
        # a phrase did or didn't reach chat. Branches below each emit
        # their own [Voice]-prefixed accepted/rejected line.
        spoken = (text or "").strip()
        if not spoken:
            self.log("[Voice] GUI rejected (empty transcript).")
            return
        low = spoken.lower()
        if low in {"huh", "uh", "um", "hmm", "mm", "hm"}:
            self.log(f"[Voice] GUI rejected (filler utterance): {spoken}")
            self.log("[Voice] Route: REJECTED_LOW_CONFIDENCE")
            return
        last = getattr(self, "_last_voice_text", "")
        last_ts = float(getattr(self, "_last_voice_ts", 0.0) or 0.0)
        now = time.time()
        if low == str(last).lower() and (now - last_ts) < 2.0:
            self.log(f"[Voice] GUI rejected (duplicate within 2s): {spoken}")
            return
        self._last_voice_text = spoken
        self._last_voice_ts = now

        # Pass T-C: classify the route BEFORE handing off so the log
        # shows LOCAL_TIME / CHAT before any model call. The local-clock
        # check is a regex (cheap) -- doing it here means a follow-up
        # like "what time is it" lands in <50ms even when the listener
        # currently has a chat-busy spinner up against Ollama.
        try:
            from services.local_clock import maybe_answer as _local_clock_answer
        except Exception as exc:
            self.log(f"[Time] local_clock import failed ({exc}); falling through.")
            _local_clock_answer = None  # type: ignore[assignment]
        local_answer: Optional[str] = None
        if _local_clock_answer is not None:
            try:
                local_answer = _local_clock_answer(spoken)
            except Exception as exc:
                self.log(f"[Time] maybe_answer raised ({exc}); routing to chat.")
                local_answer = None

        def apply_transcript() -> None:
            if local_answer:
                # Pass T-C: LOCAL_TIME route. Skip the LLM entirely; the
                # answer is system-clock truth. Still extends grace so
                # the user can keep talking after the time/date reply.
                self.log(f"[Voice] Route: LOCAL_TIME -> {spoken}")
                self.log("[Time] Answered from local system clock")
                self._chat_append("You", spoken)
                self._remember_chat("user", spoken)
                self._chat_reply(local_answer)
                self._extend_voice_grace()
                return
            if self._chat_inflight:
                self.log(f"[Voice] GUI rejected (chat busy): {spoken}")
                return
            self.log(f"[Voice] Route: CHAT -> {spoken}")
            self.log(f"[Voice] GUI accepted transcript -> chat: {spoken}")
            self.chat_entry.delete(0, "end")
            self.chat_entry.insert(0, spoken)
            self._send_chat()
            # Pass T-A: extend the follow-up window so a quick chain
            # (chat -> "clip that" -> "what time is it") stays in grace.
            self._extend_voice_grace()

        self.after(0, apply_transcript)

    def _set_services_output(self, text: str) -> None:
        if hasattr(self, "services_out"):
            self.services_out.delete("1.0", "end")
            self.services_out.insert("end", text)
            self._trim_textbox(self.services_out, max_chars=16000)

    def _is_image_request(self, text: str) -> bool:
        t = (text or "").strip()
        return bool(IMAGE_REQUEST_RE.search(t))

    def _is_video_request(self, text: str) -> bool:
        t = (text or "").strip()
        return bool(VIDEO_REQUEST_RE.search(t))

    def _run_tool_prompt(self, path: str, payload: Dict[str, Any], label: str) -> None:
        try:
            import httpx, json

            r = httpx.post(
                f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}{path}",
                json=payload,
                timeout=240,
            )
            r.raise_for_status()
            data = r.json()
            pretty = json.dumps(data, indent=2)
            self.after(0, lambda pretty=pretty: self._set_services_output(pretty))

            if data.get("status") == "ok":
                out_path = data.get("path") or data.get("file")
                msg = f"{label} complete."
                if out_path:
                    msg += f" Saved to: {out_path}"
                self.after(0, lambda msg=msg: self._chat_reply(msg))
            else:
                detail = data.get("message") or data.get("detail") or pretty
                self.after(0, lambda detail=detail, label=label: self._chat_append("Simian", f"{label} tool did not finish cleanly: {detail}"))
        except Exception as e:
            err = str(e)
            self.after(0, lambda err=err, label=label: self._chat_append("Simian", f"{label} tool failed: {err}"))

    def _maybe_handle_tool_prompt(self, text: str) -> bool:
        prompt = (text or "").strip()
        if not prompt:
            return False

        if self._is_image_request(prompt):
            self._chat_append("Simian", "Routing that request to the image tool...")
            threading.Thread(
                target=self._run_tool_prompt,
                args=("/api/gen/txt2img", {"prompt": prompt}, "Image"),
                daemon=True,
            ).start()
            return True

        if self._is_video_request(prompt):
            self._chat_append("Simian", "Routing that request to the video tool...")
            threading.Thread(
                target=self._run_tool_prompt,
                args=("/api/gen/video", {"prompt": prompt, "seconds": 4}, "Video"),
                daemon=True,
            ).start()
            return True

        return False

    # ----------------------- SCREEN AWARENESS -----------------------

    def _screen_awareness(self):
        """Return the process singleton, wired to our logger, or None."""
        if get_screen_awareness is None:
            return None
        try:
            return get_screen_awareness(log_cb=self.log)
        except Exception as exc:
            self.log(f"[ScreenAwareness] init failed: {exc}")
            return None

    def _sync_screen_awareness_from_settings(self) -> None:
        """Push the saved enabled flag into the runtime service."""
        svc = self._screen_awareness()
        if svc is None:
            return
        try:
            svc.set_enabled(bool(getattr(self.settings, "screen_awareness_enabled", False)))
        except Exception as exc:
            self.log(f"[ScreenAwareness] sync failed: {exc}")

    def _maybe_handle_screen_query(self, text: str) -> bool:
        """Intercept screen-context questions when awareness is enabled.

        Returns True if handled (capture + vision call started in a
        worker thread). Returns False when awareness is off so the
        caller falls through to normal chat.
        """
        if not SCREEN_QUESTION_RE.search(text):
            return False
        svc = self._screen_awareness()
        if svc is None:
            return False
        if not svc.is_enabled():
            # Feature is off -- let the chat model answer honestly that
            # it can't see the screen rather than silently pretending.
            return False

        self._set_chat_busy(True)
        self._chat_append("Simian", "Looking at your screen...")
        prompt_text = text  # capture for closure

        def worker() -> None:
            try:
                vision_model = (
                    os.environ.get("OLLAMA_VISION_MODEL")
                    or getattr(self.settings, "router", {}).get("vision")
                    or "qwen3-vl:8b-thinking"
                )
                exclusions = list(getattr(self.settings, "screen_awareness_exclusions", []) or [])
                # Pull the live timeout / downscale budget from settings
                # so the Settings tab is authoritative and the old 120s
                # hardcode inside the service can't bite us on slow first
                # runs. Both values fall back to the service defaults if
                # somehow missing from the settings dict.
                try:
                    vision_timeout = float(
                        getattr(self.settings, "screen_awareness_vision_timeout_sec", 180)
                    )
                except Exception:
                    vision_timeout = 180.0
                try:
                    vision_max_dim = int(
                        getattr(self.settings, "screen_awareness_vision_max_dim", 1280)
                    )
                except Exception:
                    vision_max_dim = 1280

                # Self-window bias: when Simian itself is the active
                # window the model tends to just describe the Simian UI
                # (which is true but unhelpful). We peek at the active
                # window BEFORE capture so we can nudge the prompt to
                # de-emphasise Simian chrome and describe any other
                # visible content. We still rely on the service for the
                # actual capture + retry/warmup logic -- no service-side
                # behaviour is modified.
                try:
                    from services.screen_awareness import _active_window_info as _win_info
                    pre_title = (_win_info().get("title") or "")
                except Exception:
                    pre_title = ""
                self_owned_active = self._looks_like_self_window(pre_title)

                base_prompt = (
                    f"User asked: {prompt_text!r}\n\n"
                    + svc.DEFAULT_VISION_PROMPT
                    + "\nAnswer the user's question in one short paragraph after the structured fields."
                )
                if self_owned_active:
                    base_prompt += (
                        "\n\nNote: the currently focused window is the Simian assistant itself "
                        "(title starts with 'Simian'). Treat the Simian UI chrome as background. "
                        "Prioritise describing any OTHER visible application, document, browser "
                        "tab, editor, or content on the screen. If the screenshot contains nothing "
                        "other than Simian, say so honestly and briefly."
                    )

                snap = svc.capture_now(
                    analyze=True,
                    vision_model=vision_model,
                    exclusions=exclusions,
                    prompt=base_prompt,
                    timeout=vision_timeout,
                    max_dim=vision_max_dim,
                )
                if snap is None:
                    self.after(0, lambda: self._chat_reply(
                        "Screen awareness is paused or off. Turn it on in Settings to let me see the screen."
                    ))
                    return
                if snap.degraded_reason == "excluded_window":
                    self.after(0, lambda: self._chat_reply(
                        "The active window is on your Screen Awareness exclusion list, so I'm not capturing it."
                    ))
                    return
                if snap.degraded_reason == "capture_unavailable":
                    self.after(0, lambda: self._chat_reply(
                        "I couldn't capture the screen (mss or Pillow isn't available). The Screen Awareness feature is degraded."
                    ))
                    return

                body: List[str] = []
                head = snap.active_window_title or snap.active_app
                if head:
                    body.append(f"Active window: {head}")
                # When Simian itself was the foreground window we prepend
                # an honest note so the user understands why the vision
                # description may lean toward Simian's own UI. This is a
                # message in the chat body only -- the retry/timeout
                # service path is unchanged.
                if self._looks_like_self_window(snap.active_window_title or ""):
                    body.append(
                        "(My own window was in focus, so most of what I can see is Simian itself. "
                        "If you want me to describe something else, bring that window to the front and ask again.)"
                    )
                # 4D Lab telemetry: publish every vision outcome so the
                # Lab tab can render a rolling "last vision call" pill.
                try:
                    from services.four_d_telemetry import telemetry as _four_d_t
                    if snap.analysis_text:
                        _four_d_t.emit("vision", "ok", chars=len(snap.analysis_text or ""), model=str(snap.vision_model or ""))
                    elif snap.degraded_reason:
                        _four_d_t.emit("vision", "fail", reason=str(snap.degraded_reason), model=str(snap.vision_model or ""))
                except Exception:
                    pass
                if snap.analysis_text:
                    body.append(snap.analysis_text)
                elif snap.degraded_reason and snap.degraded_reason.startswith("vision:"):
                    reason = snap.degraded_reason[len("vision:"):]
                    body.append(self._vision_failure_message(reason, snap.vision_model))
                    # Vision-free fallback: if we have a local context
                    # summary (other visible windows, active app), include
                    # it so the user gets something concrete instead of a
                    # bare error. Skip if the active-window line already
                    # covers it.
                    extra = self._extra_local_context(snap)
                    if extra:
                        body.append(extra)
                elif snap.degraded_reason == "vision_unavailable":
                    # Back-compat branch in case an older service build is
                    # paired with this GUI during an in-place upgrade.
                    body.append(
                        "I captured the screen but the local vision model didn't respond. "
                        "You can check that Ollama has the vision model pulled."
                    )
                    extra = self._extra_local_context(snap)
                    if extra:
                        body.append(extra)
                else:
                    body.append("I captured the screen, but no analysis was produced.")

                self.after(0, lambda reply="\n\n".join(body): self._chat_reply(reply))
            except Exception as exc:
                err = str(exc)
                self.after(0, lambda err=err: self._chat_append(
                    "Simian", f"Screen awareness failed: {err}"
                ))
            finally:
                self.after(0, lambda: self._set_chat_busy(False))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _looks_like_self_window(self, title: str) -> bool:
        """True when ``title`` looks like one of Simian's own windows.

        Used by the screen-awareness intercept to reduce self-capture
        bias: if the currently-focused window is Simian itself, we tell
        both the vision model and the user so the answer stops being a
        description of Simian's own chrome. Match is intentionally loose
        (case-insensitive substring) so it tolerates title-bar suffixes,
        alternate tab suffixes, and the CTkToplevel audio picker window.
        """
        if not title:
            return False
        try:
            own = (self.title() or "").strip().lower()
        except Exception:
            own = ""
        t = title.strip().lower()
        # Always catch the main title prefix + project tag. Fall back to
        # the live self.title() for modal toplevels (picker, dialogs).
        if t.startswith("simian"):
            return True
        if "project c.h.i.m.p" in t or "project chimp" in t:
            return True
        if own and own in t:
            return True
        return False

    @staticmethod
    def _vision_failure_message(reason: str, model: Optional[str]) -> str:
        """Translate a screen_awareness vision error code into user text.

        Kept dumb and specific so the user sees the actual problem
        ("server unreachable", "model not installed", "timeout", ...)
        instead of the v1 collapsed "model didn't respond" message.
        The error codes come from services.screen_awareness._ollama_vision_request.
        """
        m = model or "the local vision model"
        if reason == "ollama_unreachable":
            return (
                "I captured the screen but couldn't reach Ollama on 127.0.0.1:11434. "
                "Start the Ollama server and try again."
            )
        if reason == "model_not_installed":
            return (
                f"I captured the screen but Ollama returned 404 for '{m}'. "
                f"Pull the model first, e.g. `ollama pull {m}`."
            )
        if reason == "timeout":
            return (
                f"I captured the screen but '{m}' did not respond in time. "
                "Vision thinking models can be slow on first use; try again "
                "(the warm model usually answers fast), raise the vision "
                "timeout in Settings > Screen Awareness, or pick a lighter "
                "vision model in Settings > Router."
            )
        if reason == "empty_response":
            return (
                f"I captured the screen and '{m}' responded, but the response text was empty. "
                "Try the same question again, or switch to a different vision model."
            )
        if reason == "httpx_unavailable":
            return (
                "I captured the screen but the Python 'httpx' library is missing, "
                "so I can't reach Ollama from inside Simian."
            )
        if reason == "bad_json":
            return (
                f"I captured the screen but Ollama returned a malformed response for '{m}'."
            )
        if reason == "no_pixels":
            return (
                "I tried to capture the screen but got zero pixels back. "
                "Check that mss/Pillow are installed and that the primary monitor is active."
            )
        if reason.startswith("memory_alloc"):
            # Dedicated classification emitted by
            # _ollama_vision_request when Ollama returns a memory /
            # layout allocation failure (typical shape: 500 body says
            # "memory layout cannot be allocated"). Retrying won't
            # help; point the user at a lighter model.
            return (
                f"I captured the screen but Ollama couldn't allocate memory for '{m}' "
                "(VRAM exhausted). "
                "Set `screen_awareness_lighter_vision_model` in settings.json to a "
                "smaller model (e.g. qwen2.5-vl:3b or llava-phi3), "
                "or close other GPU-hungry apps and retry."
            )
        if reason.startswith("model_load_failure"):
            return (
                f"I captured the screen but Ollama reported a model-load failure for '{m}'. "
                "Check `ollama list` shows the model, and that the Ollama server log "
                "doesn't show a shader/driver error. A smaller vision model usually "
                "recovers this."
            )
        if reason.startswith("http_500"):
            # Generic 500 (the two specific sub-shapes above are
            # classified first). Retry-resistant server-side failure.
            return (
                f"I captured the screen but Ollama returned HTTP 500 for '{m}'. "
                "This usually means the model is too heavy for available VRAM or the "
                "server hit an internal error. "
                "Try a lighter vision model in Settings > Router (e.g. qwen2.5-vl:3b or llava-phi3), "
                "or close other GPU-hungry apps and retry."
            )
        if reason.startswith("http_"):
            return (
                f"I captured the screen but Ollama returned an HTTP error: {reason[len('http_'):]}."
            )
        if reason.startswith("request_failed:"):
            return (
                f"I captured the screen but the request to Ollama failed "
                f"with {reason[len('request_failed:'):]}."
            )
        return (
            f"I captured the screen but the vision call failed ({reason}). "
            "Capture-only context was returned."
        )

    @staticmethod
    def _extra_local_context(snap: Any) -> str:
        """Return the local context block for vision-failure replies.

        Skips lines the chat body has already emitted (Active window,
        Active app) so the user doesn't see duplicate headers. Returns
        an empty string when nothing new is worth appending.
        """
        local = getattr(snap, "local_context", None) or ""
        if not local.strip():
            return ""
        active_title = (getattr(snap, "active_window_title", None) or "").strip()
        active_app = (getattr(snap, "active_app", None) or "").strip()
        kept: list[str] = []
        for line in local.splitlines():
            low = line.strip().lower()
            if not low:
                continue
            if active_title and low == f"active window: {active_title.lower()}":
                continue
            if active_app and low == f"active app: {active_app.lower()}":
                continue
            kept.append(line)
        if not kept:
            return ""
        return "Here is what I could still see without the vision model:\n" + "\n".join(kept)

    def _voice_screen_look(self) -> None:
        """Voice intent: 'look at my screen' / 'what's on my screen'."""
        svc = self._screen_awareness()
        if svc is None or not svc.is_enabled():
            self.log("[ScreenAwareness] Voice 'look at screen' ignored -- feature disabled.")
            try:
                self._speak("Screen awareness is off. Enable it in settings first.")
            except Exception:
                pass
            return
        # Reuse the chat pipeline so the reply is visible and logged like
        # any other assistant turn. We synthesize a canonical question.
        self._chat_append("You", "(voice) what's on my screen?")
        self._remember_chat("user", "what's on my screen?")
        self._maybe_handle_screen_query("what's on my screen?")

    def _voice_screen_pause(self) -> None:
        svc = self._screen_awareness()
        if svc is None:
            return
        svc.pause()
        try:
            self._speak("Screen awareness paused.")
        except Exception:
            pass

    def _voice_screen_resume(self) -> None:
        svc = self._screen_awareness()
        if svc is None:
            return
        svc.resume()
        try:
            self._speak("Screen awareness resumed.")
        except Exception:
            pass

    def _send_chat(self) -> None:
        text = self.chat_entry.get().strip()
        if not text or self._chat_inflight:
            return
        self.chat_entry.delete(0, "end")
        self._chat_append("You", text)
        self._remember_chat("user", text)
        # 4D Lab telemetry: publish the turn so the Lab tab can show
        # "the app is working" signals. Best-effort: emit() never raises.
        try:
            from services.four_d_telemetry import telemetry as _four_d_t
            _four_d_t.emit("chat", "user_turn", length=len(text))
        except Exception:
            pass

        if IDENTITY_QUERY_RE.search(text):
            self.after(0, lambda: self._chat_reply("I'm Simian, your local assistant for Project C.H.I.M.P."))
            return

        local = self._handle_local_query(text)
        if local:
            self.after(0, lambda local=local: self._chat_reply(local))
            return

        # Screen Awareness intercept MUST run before the tool-prompt
        # handler: "what's on my screen" would otherwise match nothing
        # useful, and the generic fallback path has no screen context.
        if self._maybe_handle_screen_query(text):
            return

        if self._maybe_handle_tool_prompt(text):
            return

        self._set_chat_busy(True)

        def worker() -> None:
            try:
                import httpx

                if not self._ensure_ollama_running(timeout=8.0):
                    self.after(0, lambda: self._chat_append(
                        "Simian",
                        "I couldn't reach the local Ollama backend on 127.0.0.1:11434. Start Ollama, or set SIMIAN_OLLAMA_CMD so Simian can auto-start it.",
                    ))
                    return

                model = os.environ.get("SIMIAN_MODEL") or os.environ.get("OLLAMA_TEXT_MODEL") or getattr(self.settings, "router", {}).get("chat", "simian:latest") or "simian:latest"
                telemetry_block = ""
                if getattr(self, "srm_running", False) and SRM_QUERY_RE.search(text):
                    telemetry_block = (
                        "\n\n[SRM telemetry snapshot]\n"
                        f"theta={self.srm_theta:.3f}\n"
                        f"phi={self.srm_phi:.3f}\n"
                        f"sigma={self.srm_sigma:.3f}\n"
                        "Interpret these as read-only visualization/debug values only.\n"
                        "Do not claim they directly control the system or grant hidden access."
                    )

                history_block = self._build_prompt_from_history()
                # Pass S-D: prepend live local-clock context so the model
                # never has to guess the time/date. The interceptor in
                # _handle_local_query already covers explicit queries;
                # this catches indirect ones ("schedule a reminder for
                # later today", "is it morning yet").
                try:
                    from services.local_clock import model_context_block as _clock_ctx
                    clock_block = _clock_ctx()
                except Exception as exc:
                    self.log(f"[Chat] Could not build local-clock context ({exc}); continuing without it.")
                    clock_block = ""
                prompt = (
                    f"{SIMIAN_SYSTEM_PROMPT}\n"
                    f"{clock_block}\n"
                    f"Current greeting context: {time_of_day_greeting()}\n"
                    "Be honest about what this desktop app can currently do.\n"
                    "Do not invent integrations, secret control paths, or fake live data.\n"
                    "Never invent the current time or date. If the user asks, "
                    "use the Local clock context above verbatim.\n"
                    "If the user asks for live system data, provide local data only when actually available.\n"
                    f"{telemetry_block}\n\n"
                    "Recent conversation:\n"
                    f"{history_block}\n"
                    "Simian:"
                )

                timeout_s = float(getattr(self.settings, "chat_request_timeout", 180) or 180)

                def _do_generate() -> str:
                    # Phase-split httpx.Timeout so a slow-to-think model
                    # does not trip connect/pool timeouts during the long
                    # read; only the read phase gets the big budget.
                    timeout = httpx.Timeout(connect=5.0, read=timeout_s, write=timeout_s, pool=5.0)
                    r = httpx.post(
                        "http://127.0.0.1:11434/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False},
                        timeout=timeout,
                    )
                    r.raise_for_status()
                    return (r.json().get("response") or "").strip() or "(No response)"

                try:
                    out = _do_generate()
                except httpx.TimeoutException as te:
                    # Single bounded retry on timeout: matches the
                    # vision-call hardening pattern. Many Ollama misses
                    # are cold-load stalls on the first token; a second
                    # try with the same budget usually lands. Any other
                    # exception (connection refused, 5xx, bad JSON) is
                    # surfaced honestly via the outer except block.
                    self.log(f"[Chat] Local model timed out once; warming then retrying: {te}")
                    # Warmup ping pins the model in memory so the retry
                    # doesn't pay the cold-load cost a second time.
                    # Short budget: if warmup itself times out, the model
                    # is truly unavailable and the retry will still
                    # surface a clean error.
                    try:
                        warm = httpx.post(
                            "http://127.0.0.1:11434/api/generate",
                            json={"model": model, "prompt": "ok", "stream": False, "keep_alive": "5m"},
                            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
                        )
                        warm.raise_for_status()
                        self.log("[Chat] Warmup ping ok before retry.")
                    except Exception as we:
                        self.log(f"[Chat] Warmup ping failed ({we}); retrying full call anyway.")
                    out = _do_generate()
                out = self._sanitize_model_reply(text, out)
                self.after(0, lambda out=out: self._chat_reply(out))
            except Exception as e:
                err = str(e)
                self.after(0, lambda err=err: self._chat_append("Simian", f"Local model request failed: {err}"))
            finally:
                self.after(0, lambda: self._set_chat_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _sanitize_model_reply(self, user_text: str, reply: str) -> str:
        low_reply = reply.lower()
        if IDENTITY_QUERY_RE.search(user_text):
            return "I'm Simian, your local assistant for Project C.H.I.M.P."
        if any(k in low_reply for k in ["i'm qwen", "my name is qwen", "alibaba cloud"]):
            return "I'm Simian, your local assistant for Project C.H.I.M.P. How can I assist you?"
        if SRM_QUERY_RE.search(user_text):
            unsafe_bits = ["trigger specific actions", "grant access", "access settings", "automation scripts"]
            if any(bit in low_reply for bit in unsafe_bits):
                return "Those SRM values are visualizer and debugging telemetry. They do not act as passwords, direct control codes, or hidden automation triggers by themselves."
        return reply

    # ----------------------- CLIPS -----------------------

    def _build_clips(self) -> None:
        outer = ctk.CTkFrame(self.tab_clips)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        ctrl = ctk.CTkFrame(outer)
        ctrl.pack(fill="x", padx=8, pady=8)

        self.lbl_replay = ctk.CTkLabel(ctrl, text="Replay buffer: stopped")
        self.lbl_replay.pack(side="left", padx=8)

        ctk.CTkButton(ctrl, text="Start Buffer", command=self._start_replay_buffer).pack(side="left", padx=6)
        ctk.CTkButton(ctrl, text="Stop Buffer", command=self._stop_replay_buffer).pack(side="left", padx=6)
        ctk.CTkButton(ctrl, text="Clip that", command=self._export_clip).pack(side="left", padx=6)

        self.extra_entry = ctk.CTkEntry(ctrl, width=120, placeholder_text="Extra sec")
        self.extra_entry.pack(side="left", padx=6)
        self.extra_entry.insert(0, str(getattr(self.settings, "extra_seconds_default", 0)))

        list_frame = ctk.CTkFrame(outer)
        list_frame.pack(fill="both", expand=True, padx=8, pady=8)
        list_frame.grid_rowconfigure(1, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(list_frame, text="Saved clips").grid(row=0, column=0, sticky="w", padx=8, pady=6)

        self.clips_box = ctk.CTkTextbox(list_frame, wrap="none")
        self.clips_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        self.clips_box.configure(state="disabled")

        ctk.CTkButton(list_frame, text="Refresh", command=self._refresh_clips).grid(
            row=2, column=0, sticky="e", padx=8, pady=8
        )

        self._refresh_clips()

    def _start_replay_buffer(self) -> None:
        sys_audio = (getattr(self.settings, "replay_system_audio_device", "") or os.environ.get("SIMIAN_SYSTEM_AUDIO") or "").strip()
        mic = (getattr(self.settings, "replay_mic_device", "") or os.environ.get("SIMIAN_MIC") or "").strip()
        self.replay.devices = CaptureDevices(
            system_audio=sys_audio or None,
            mic=mic or None,
        )
        # ReplayBufferRecorder.start() can raise if ffmpeg / dshow / audio
        # capture input is unavailable. Without this guard the exception
        # escapes as a raw Tk callback traceback (priority #4 in brief).
        try:
            self.replay.start()
        except Exception as e:
            self.log(f"[Replay] Buffer failed to start: {e}")
            try:
                self.lbl_replay.configure(text="Replay buffer: unavailable")
            except Exception:
                pass
            return
        self.lbl_replay.configure(text="Replay buffer: running")
        self.log("[Replay] Buffer running.")
        self._speak("Replay buffer running.")

    def _stop_replay_buffer(self) -> None:
        try:
            self.replay.stop()
        except Exception as e:
            self.log(f"[Replay] Buffer stop failed: {e}")
        try:
            self.lbl_replay.configure(text="Replay buffer: stopped")
        except Exception:
            pass
        self.log("[Replay] Buffer stopped.")

    def _export_clip(self) -> None:
        if self._clip_export_inflight:
            self.log("[Replay] Clip export already in progress.")
            return
        try:
            extra = int(self.extra_entry.get().strip() or "0")
        except Exception:
            extra = 0

        self._clip_export_inflight = True
        self.log("[Replay] Clip export queued.")

        def worker() -> None:
            try:
                p = self.replay.export_last(
                    minutes=getattr(self.settings, "replay_minutes", 5),
                    extra_seconds=extra,
                    upscale=getattr(self.settings, "export_upscale", "none"),
                )
                self.after(0, lambda p=p: self.log(f"[Replay] Exported clip: {p}"))
                self.after(0, self._refresh_clips)
                threading.Thread(target=lambda: self._speak("Clip saved."), daemon=True).start()
            except Exception as e:
                self.after(0, lambda e=e: self.log(f"[Replay] Export failed: {e}"))
                threading.Thread(target=lambda: self._speak("Clip export failed."), daemon=True).start()
            finally:
                self._clip_export_inflight = False

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_clips(self) -> None:
        clips_dir = Path(getattr(self.settings, "clips_dir", "data/clips"))
        clips_dir.mkdir(parents=True, exist_ok=True)
        clips = sorted(clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)

        self.clips_box.configure(state="normal")
        self.clips_box.delete("1.0", "end")
        for p in clips[:200]:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
            self.clips_box.insert("end", f"{p.name}\t{stamp}\n")
        self.clips_box.configure(state="disabled")

    # ----------------------- FILES -----------------------

    def _build_files(self) -> None:
        outer = ctk.CTkFrame(self.tab_files)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        row = ctk.CTkFrame(outer)
        row.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.file_path_entry = ctk.CTkEntry(row, placeholder_text="Pick a file or paste a path...")
        self.file_path_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)

        ctk.CTkButton(row, text="Browse", command=self._browse_file).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Import", command=self._import_file_to_simian).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Summarize", command=self._summarize_file).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Open", command=self._open_selected_file).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Send to Chat", command=self._send_selected_file_to_chat).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Copy Path", command=self._copy_selected_file_path).pack(side="left", padx=6)

        row2 = ctk.CTkFrame(outer)
        row2.grid(row=1, column=0, sticky="ew", padx=8, pady=4)

        self.dir_entry = ctk.CTkEntry(row2, placeholder_text="Directory path for batch scan (optional)")
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ctk.CTkButton(row2, text="Summarize Dir", command=self._summarize_dir).pack(side="left", padx=6)

        self.file_out = ctk.CTkTextbox(outer, wrap="word")
        self.file_out.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        self.file_status = ctk.CTkLabel(outer, text="No file loaded into chat context.")
        self.file_status.grid(row=3, column=0, sticky="w", padx=8, pady=(0,8))

    def _browse_file(self) -> None:
        import tkinter.filedialog as fd

        fp = fd.askopenfilename()
        if fp:
            self.file_path_entry.delete(0, "end")
            self.file_path_entry.insert(0, fp)

    def _import_file_to_simian(self) -> None:
        fp = self.file_path_entry.get().strip()
        if not fp:
            return
        src_path = Path(fp)
        if not src_path.exists() or not src_path.is_file():
            self._set_file_out(f"Error: file not found: {fp}")
            return
        uploads_dir = REPO_ROOT / "data" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        target = uploads_dir / src_path.name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = uploads_dir / f"{stem}_{int(time.time())}{suffix}"
        try:
            shutil.copy2(src_path, target)
            self.file_path_entry.delete(0, "end")
            self.file_path_entry.insert(0, str(target))
            self._set_file_out(f"Imported into Simian workspace:\n{target}")
            self.log(f"[Files] Imported: {target}")
        except Exception as e:
            self._set_file_out(f"Import failed: {e}")

    def _open_selected_file(self) -> None:
        fp = self.file_path_entry.get().strip()
        if not fp:
            return
        try:
            if os.name == "nt":
                os.startfile(fp)  # type: ignore[attr-defined]
            else:
                webbrowser.open(Path(fp).resolve().as_uri())
        except Exception as e:
            self._set_file_out(f"Open failed: {e}")

    def _copy_selected_file_path(self) -> None:
        fp = self.file_path_entry.get().strip()
        if not fp:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(fp)
            self.file_status.configure(text="Copied selected file path to clipboard.")
        except Exception as e:
            self._set_file_out(f"Clipboard failed: {e}")

    def _send_selected_file_to_chat(self) -> None:
        fp = self.file_path_entry.get().strip()
        if not fp:
            return
        if self._selected_file_context and self._selected_file_context.get("path") == fp:
            name = Path(fp).name
            self.tabs.set("Chat")
            self.chat_entry.delete(0, "end")
            self.chat_entry.insert(0, f"Use the selected file context for {name} and help me work on it.")
            self.file_status.configure(text=f"Selected file context armed for chat: {name}")
            return
        self._summarize_file(send_to_chat=True)

    def _summarize_file(self, send_to_chat: bool = False) -> None:
        fp = self.file_path_entry.get().strip()
        if not fp:
            return

        svc = FileScannerService(log_cb=self.log)
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", "Scanning...\n")

        def worker() -> None:
            try:
                res = svc.summarize_path(fp)
                txt = (
                    f"Path: {res.path}\n"
                    f"Mime: {res.mime}\n"
                    f"Size: {res.size_bytes}\n"
                    f"SHA256: {res.sha256}\n\n"
                    f"Summary:\n{res.summary}\n\n"
                    f"Details:\n{res.details}\n"
                )
                ctx = {"path": res.path, "mime": res.mime, "summary": res.summary, "details": res.details}
                def done() -> None:
                    self._selected_file_context = ctx
                    self._selected_file_summary = res.summary
                    self.file_status.configure(text=f"Loaded file context: {Path(res.path).name}")
                    self._set_file_out(txt)
                    if send_to_chat:
                        self.tabs.set("Chat")
                        self.chat_entry.delete(0, "end")
                        self.chat_entry.insert(0, f"Use the selected file context for {Path(res.path).name} and help me work on it.")
                self.after(0, done)
            except Exception as e:
                self.after(0, lambda: self._set_file_out(f"Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _summarize_dir(self) -> None:
        dp = self.dir_entry.get().strip()
        if not dp:
            return

        svc = FileScannerService(log_cb=self.log)
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", "Batch scanning...\n")

        def worker() -> None:
            try:
                payload = svc.summarize_directory(dp, recursive=True, max_files=200)
                lines = [f"Scanned {payload['count']} files\n"]
                for item in payload["results"]:
                    if "error" in item:
                        lines.append(f"- {item['path']}  ERROR: {item['error']}")
                    else:
                        name = Path(item["path"]).name
                        lines.append(f"- {name}: {item['mime']}  (sha={item['sha256'][:10]}...)")
                self.after(0, lambda: self._set_file_out("\n".join(lines)))
            except Exception as e:
                self.after(0, lambda: self._set_file_out(f"Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _set_file_out(self, text: str) -> None:
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", text)
        self._trim_textbox(self.file_out, max_chars=18000)

    # ----------------------- NEWS -----------------------

    def _build_news(self) -> None:
        outer = ctk.CTkFrame(self.tab_news)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        bar = ctk.CTkFrame(outer)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        bar.grid_columnconfigure(4, weight=1)

        self.news_category = ctk.CTkOptionMenu(bar, values=["tech", "world", "security", "science", "business"])
        self.news_category.set(getattr(self.settings, "news_default_category", "tech"))
        self.news_category.grid(row=0, column=0, padx=8, pady=8, sticky="w")

        ctk.CTkButton(bar, text="Refresh now", command=self._refresh_news).grid(row=0, column=1, padx=6, pady=8)

        self.news_search_entry = ctk.CTkEntry(bar, placeholder_text='Search topic, quote, or date terms like FBI, "Kash Patel", after:2023-01-01')
        self.news_search_entry.grid(row=0, column=2, padx=8, pady=8, sticky="ew")
        self.news_search_entry.bind("<KeyRelease>", self._queue_news_filter)
        self.news_search_entry.bind("<Return>", lambda _e: self._refresh_news())

        ctk.CTkButton(bar, text="Clear search", command=lambda: (self.news_search_entry.delete(0, "end"), self._refresh_news())).grid(row=0, column=3, padx=6, pady=8)

        self.news_status = ctk.CTkLabel(bar, text="")
        self.news_status.grid(row=0, column=4, padx=12, pady=8, sticky="e")

        self.news_box = ctk.CTkTextbox(outer, wrap="word")
        self.news_box.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        self.news_box.configure(cursor="xterm", state="disabled")

        ctk.CTkLabel(outer, text="Tip: click a link to open it in your default browser.").grid(row=3, column=0, sticky="w", padx=8, pady=4)

    def _schedule_news_refresh(self, initial: bool = False) -> None:
        # Skip the auto-refresh when the News tab is hidden. Manual
        # "Refresh now" (_refresh_news called directly from the button
        # binding) still works. We still reschedule so that a fresh
        # fetch runs shortly after the user returns to the tab -- the
        # _on_tab_changed hook calls _refresh_news when the user picks
        # the News tab back up.
        def _tab_is_active() -> bool:
            try:
                return str(self.tabs.get()) == "World News"
            except Exception:
                return True
        # Never call _refresh_news before the News tab has been
        # lazily hydrated (news_category / news_box don't exist yet).
        tab_ready = bool(getattr(self, "_news_tab_built", False))
        if tab_ready and initial and _tab_is_active():
            self.after(1000, self._refresh_news)
        elif tab_ready and not initial and _tab_is_active():
            # Only fire the scheduled refresh when actually visible.
            self._refresh_news()
        # low_resource_mode raises the refresh floor so older boxes
        # don't hammer RSS sources every minute. 180s floor vs 30s.
        base_s = max(30, int(getattr(self.settings, "news_refresh_seconds", 300)))
        try:
            from services.settings_store import load_settings as _load_settings_news
            if bool(_load_settings_news().get("low_resource_mode", False)):
                base_s = max(180, base_s)
        except Exception:
            pass
        self.after(base_s * 1000, self._schedule_news_refresh)

    def _search_public_news(self, query: str, limit: int = 60) -> List[Any]:
        import email.utils
        import html
        import urllib.parse
        import urllib.request
        import xml.etree.ElementTree as ET

        q = (query or "").strip()
        if not q:
            return []
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Simian/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items: List[Any] = []
        for item in root.findall(".//item")[:limit]:
            title = html.unescape((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            published = pub_raw
            try:
                dt = email.utils.parsedate_to_datetime(pub_raw)
                published = dt.isoformat()
            except Exception:
                pass
            source = "Google News"
            source_el = item.find("source")
            source_text = (getattr(source_el, "text", "") or "").strip() if source_el is not None else ""
            if source_text:
                source = source_text
            desc = html.unescape((item.findtext("description") or "").strip())
            items.append(SimpleNamespace(title=title, url=link, published=published, source=source, summary=desc))
        return items

    def _refresh_news(self) -> None:
        cat = self.news_category.get()
        query = self.news_search_entry.get().strip() if hasattr(self, "news_search_entry") else ""
        self.news_status.configure(text="Loading...")

        def worker() -> None:
            try:
                if query:
                    items = self._search_public_news(query, limit=int(getattr(self.settings, "news_search_limit", 60)))
                    status = f"{len(items)} search results"
                else:
                    items = fetch_news(cat, limit=int(getattr(self.settings, "news_search_limit", 60)))
                    status = f"{len(items)} items"
                self.after(0, lambda items=items, status=status: self._set_news_items(items, status))
            except Exception as e:
                self.after(0, lambda e=e: self._set_news_error(f"Error: {e}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _queue_news_filter(self, event: Any = None) -> None:
        if event is not None and getattr(event, "keysym", "") in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Left", "Right", "Up", "Down"}:
            return
        if self._news_filter_after_id is not None:
            try:
                self.after_cancel(self._news_filter_after_id)
            except Exception:
                pass
        self._news_filter_after_id = self.after(120, self._apply_news_filter)

    def _apply_news_filter(self, _event: Any = None) -> None:
        query = self.news_search_entry.get().strip().lower() if hasattr(self, "news_search_entry") else ""
        if not query:
            items = list(self._all_news_items)
        else:
            tokens = [t for t in re.split(r"\s+", query) if t]
            items = []
            for it in self._all_news_items:
                hay = " ".join([
                    (getattr(it, "title", "") or ""),
                    (getattr(it, "source", "") or ""),
                    (getattr(it, "summary", "") or ""),
                    (getattr(it, "url", "") or ""),
                    (getattr(it, "published", "") or ""),
                ]).lower()
                if all(tok in hay for tok in tokens):
                    items.append(it)
        self._render_news_items(items)

    def _set_news_items(self, items: List[Any], status: str) -> None:
        self._all_news_items = list(items)
        self._news_status_text = status
        # Record the successful-refresh timestamp so _on_tab_changed can
        # decide whether to trigger a fresh fetch when the user returns
        # to the News tab after a while.
        self._news_last_refresh_ts = time.time()
        self._apply_news_filter()

    def _render_news_items(self, items: List[Any]) -> None:
        self.news_box.configure(state="normal")
        self.news_box.delete("1.0", "end")

        if not items:
            self.news_box.insert("end", "No matching items found.")
            self.news_box.configure(state="disabled")
            self.news_status.configure(text="0 items")
            return

        for idx, it in enumerate(items):
            when = f" ({getattr(it, 'published', '')})" if getattr(it, "published", None) else ""
            self.news_box.insert("end", f"[{getattr(it, 'source', 'News')}]" + when + "\n" + (getattr(it, 'title', '') or '') + "\n")
            summary = getattr(it, "summary", None)
            if summary:
                summary = re.sub(r"<[^>]+>", " ", summary).replace("&#8230;", "...").strip()
                self.news_box.insert("end", f"{summary}\n")
            start = self.news_box.index("end-1c")
            url = getattr(it, "url", "") or ""
            self.news_box.insert("end", f"{url}\n\n")
            end = self.news_box.index("end-1c")
            tag = f"link_{idx}"
            self.news_box.tag_add(tag, start, end)
            # Pass O: link color follows the theme accent so News matches
            # whatever the user has picked in Settings / palette popup.
            try:
                _link_col = self._current_theme()["accent"]
            except Exception:
                _link_col = "#6aaeff"
            self.news_box.tag_config(tag, foreground=_link_col, underline=True)
            self.news_box.tag_bind(tag, "<Button-1>", lambda _e, url=url: webbrowser.open(url, new=2))
            self.news_box.tag_bind(tag, "<Enter>", lambda _e: self.news_box.configure(cursor="hand2"))
            self.news_box.tag_bind(tag, "<Leave>", lambda _e: self.news_box.configure(cursor="xterm"))

        shown = f"{len(items)} items"
        if getattr(self, "news_search_entry", None) and self.news_search_entry.get().strip():
            shown = f"{len(items)} shown / {len(self._all_news_items)} total"
        self.news_box.configure(state="disabled")
        self.news_status.configure(text=shown)

    def _set_news_error(self, text: str, status: str) -> None:
        self._all_news_items = []
        self._news_status_text = status
        self.news_box.configure(state="normal")
        self.news_box.delete("1.0", "end")
        self.news_box.insert("end", text)
        self.news_box.configure(state="disabled")
        self.news_status.configure(text=status)
    # ----------------------- 4D LAB -----------------------

    def _build_4d(self) -> None:
        # TODO(4d-lab-telemetry): extend the SRM panel with a live
        # recharts-style history ring (theta/phi/sigma over last N
        # samples) and a 'World Tracker' mini-map companion tab that
        # subscribes to the same telemetry stream. Keep the telemetry
        # POST path optional so the app still works with the API off.
        outer = ctk.CTkFrame(self.tab_4d)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(outer)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.lbl_srm = ctk.CTkLabel(bar, text="SRM: stopped")
        self.lbl_srm.pack(side="left", padx=8)

        ctk.CTkButton(bar, text="Start", command=self._srm_start).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="Stop", command=self._srm_stop).pack(side="left", padx=6)

        self.chk_push = ctk.CTkCheckBox(
            bar,
            text="Push telemetry to API (/api/telemetry)",
            onvalue=1,
            offvalue=0,
        )
        self.chk_push.pack(side="left", padx=12)

        import tkinter as tk

        # Pass O: canvas bg follows the theme log_bg so the 4D Lab
        # matches the rest of the app instead of a hardcoded #111111.
        try:
            _canvas_bg = self._current_theme()["log_bg"]
        except Exception:
            _canvas_bg = "#111111"
        self.canvas = tk.Canvas(outer, bg=_canvas_bg, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

    def _srm_start(self) -> None:
        if self.srm_running:
            return
        self.srm_running = True
        self.lbl_srm.configure(text="SRM: running")
        self.log("[4D] SRM visualizer started.")
        self._srm_tick()

    def _srm_stop(self) -> None:
        self.srm_running = False
        self.lbl_srm.configure(text="SRM: stopped")
        self.log("[4D] SRM visualizer stopped.")

    def _srm_tick(self) -> None:
        if not self.srm_running:
            return

        import math

        # Pause-when-hidden: if the 4D Lab tab is not the active tab we
        # stop rescheduling entirely. ``_on_tab_changed`` will resume
        # this tick the next time the user switches to 4D Lab. This
        # removes ALL SRM overhead from tab-switching latency, which
        # previously cost a redraw + telemetry post every 250ms even
        # while invisible.
        is_active_tab = True
        try:
            is_active_tab = str(self.tabs.get()) == "4D Lab"
        except Exception:
            is_active_tab = True
        if not is_active_tab:
            # Leave srm_running True so _on_tab_changed knows to resume.
            return

        self.srm_theta = (self.srm_theta + 0.03) % 6.283
        self.srm_phi = (self.srm_phi + 0.021) % 6.283
        self.srm_sigma = (self.srm_sigma + 0.017) % 6.283

        cx, cy = 520, 260
        scale = 160
        pts = []
        for i in range(24):
            a = (i / 24.0) * 6.283
            x = math.cos(a + self.srm_theta)
            y = math.sin(a + self.srm_phi)
            z = math.cos(2 * a + self.srm_sigma)
            w = math.sin(2 * a + self.srm_theta)
            px = cx + (x + 0.35 * z) * scale
            py = cy + (y + 0.35 * w) * scale
            pts.append((px, py))

        self._draw_points(pts)

        # Telemetry push throttle. On older hardware the 30 Hz SRM push
        # is a visible CPU cost, especially when the 4D Lab tab is the
        # frontmost one and the canvas redraws are competing with
        # vision/replay work. When low_resource_mode is on, emit every
        # 4th tick (~7.5 Hz) -- still smooth enough to read, ~4x less
        # queue churn. Per-tick counter is cheap; no state mutation on
        # the fast path when the feature is off.
        self._srm_tick_count = getattr(self, "_srm_tick_count", 0) + 1
        push_every = 1
        srm_interval_ms = 33
        try:
            from services.settings_store import load_settings as _load_settings_srm
            if bool(_load_settings_srm().get("low_resource_mode", False)):
                push_every = 4
                srm_interval_ms = 66  # ~15 Hz render, still fluid
        except Exception:
            pass

        if self.chk_push.get() == 1 and (self._srm_tick_count % push_every == 0):
            self._push_telemetry(
                {
                    "theta": self.srm_theta,
                    "phi": self.srm_phi,
                    "sigma": self.srm_sigma,
                    "points": pts[:8],
                    "ts": time.time(),
                }
            )

        self.after(srm_interval_ms, self._srm_tick)

    def _lazy_build_news(self) -> None:
        """Deferred _build_news wrapper. Runs ~220ms after __init__.

        Sets ``_news_tab_built`` so _on_tab_changed and
        _schedule_news_refresh can gate on it. Idempotent: a second
        invocation (e.g. from a first-visit force-build in
        _on_tab_changed) is a no-op. Themes the newly-built tab on the
        way out so the Pass N regression (buttons defaulted to CTk blue
        because _apply_accent_color had already run in __init__) is
        fixed: lazy-built widgets get theming at hydration time.
        """
        if getattr(self, "_news_tab_built", False):
            return
        t0 = time.perf_counter()
        try:
            self._build_news()
            self._news_tab_built = True
            # Pass O regression fix: theme the freshly built tab now that
            # its widgets exist. Without this, Refresh now / Clear search
            # / the search entry keep CTk's default palette.
            try:
                self._apply_theme(self.tab_news)
            except Exception:
                pass
            dt_ms = (time.perf_counter() - t0) * 1000.0
            if dt_ms >= 40.0:
                self.log(f"[Perf] News tab hydrated in {dt_ms:.0f}ms (lazy).")
        except Exception as e:
            self.log(f"[Startup] Deferred News tab build failed: {e}")

    def _lazy_build_4d(self) -> None:
        """Deferred _build_4d wrapper. Runs ~480ms after __init__.

        Same idempotency + perf-instrumentation contract as
        _lazy_build_news; also themes the newly-built widgets so the
        4D Lab buttons honour the global accent (Pass O regression fix).
        """
        if getattr(self, "_fourd_tab_built", False):
            return
        t0 = time.perf_counter()
        try:
            self._build_4d()
            self._fourd_tab_built = True
            try:
                self._apply_theme(self.tab_4d)
                # Canvas is raw tk.Canvas, re-apply bg explicitly.
                palette = self._current_theme()
                if hasattr(self, "canvas"):
                    self.canvas.configure(bg=palette["log_bg"])
            except Exception:
                pass
            dt_ms = (time.perf_counter() - t0) * 1000.0
            if dt_ms >= 40.0:
                self.log(f"[Perf] 4D Lab tab hydrated in {dt_ms:.0f}ms (lazy).")
        except Exception as e:
            self.log(f"[Startup] Deferred 4D Lab tab build failed: {e}")

    def _on_tab_changed(self) -> None:
        """CTkTabview command callback. Resume paused-while-hidden work.

        Pass Q: instrumented with a perf-warn over 50ms so a slow
        per-tab handler shows up in the log immediately. Heavy first-
        visit hydration is deferred to ``after_idle`` so the tab paints
        BEFORE the build runs -- the user sees the new tab immediately
        and the build fills it in on the next idle cycle.
        """
        _tab_t0 = time.perf_counter()
        try:
            current = str(self.tabs.get())
        except Exception:
            return

        # Pass Q: defer first-visit force-build so the new tab paints
        # immediately. _lazy_build_* is idempotent so if the scheduled
        # self.after already fired this is a no-op. ``after_idle`` runs
        # the heavy build right after the current event handler returns
        # AND the redraw completes, which is the difference between
        # "instant tab switch" and "frozen for 200ms".
        if current == "World News" and not getattr(self, "_news_tab_built", False):
            self.after_idle(self._lazy_build_news)
        if current == "4D Lab" and not getattr(self, "_fourd_tab_built", False):
            self.after_idle(self._lazy_build_4d)

        if current == "4D Lab" and getattr(self, "srm_running", False):
            # _srm_tick is idempotent; calling it here restarts the
            # self.after cadence that was paused while the tab was
            # hidden.
            self._srm_tick()
        elif current == "World News":
            try:
                # Cheap, non-blocking kick: _refresh_news does the fetch
                # on a worker thread so the UI thread stays responsive.
                last = getattr(self, "_news_last_refresh_ts", 0.0)
                stale = (time.time() - last) > max(30.0, float(
                    getattr(self.settings, "news_refresh_seconds", 300)
                ))
                if last == 0.0 or stale:
                    self._refresh_news()
            except Exception:
                pass

        # Pass Q: surface slow tab switches. 50ms is the user-perceived
        # smoothness threshold; anything above that warrants a glance.
        try:
            dt_ms = (time.perf_counter() - _tab_t0) * 1000.0
            if dt_ms >= 50.0:
                self.log(f"[Perf] Tab switch to {current!r} took {dt_ms:.0f}ms.")
        except Exception:
            pass

    def _draw_points(self, pts: List[tuple[float, float]]) -> None:
        # Pass O: point fill + label text pull from the theme so 4D Lab
        # visuals follow the global accent/text colors.
        try:
            _palette = self._current_theme()
            _pt_fill = _palette["accent"]
            _label_col = _palette["text"]
        except Exception:
            _pt_fill = "#4da3ff"
            _label_col = "#d0d0d0"
        self.canvas.delete("all")
        for (x, y) in pts:
            r = 3
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=_pt_fill, outline="")
        self.canvas.create_text(
            10,
            10,
            anchor="nw",
            fill=_label_col,
            text=f"θ={self.srm_theta:.3f}  φ={self.srm_phi:.3f}  σ={self.srm_sigma:.3f}",
        )

    def _push_telemetry(self, payload: Dict[str, Any]) -> None:
        now = time.time()
        if (now - self._telemetry_last_ts) < self._telemetry_min_interval:
            return
        self._telemetry_last_ts = now

        def worker() -> None:
            try:
                import httpx

                httpx.post(
                    f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/api/telemetry",
                    json={"source": "gui", "kind": "srm", "payload": payload},
                    timeout=1.2,
                )
            except Exception:
                pass

        self.task_runner.start_background(worker)

    # ----------------------- SERVICES -----------------------

    def _build_services(self) -> None:
        outer = ctk.CTkFrame(self.tab_services)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(3, weight=1)

        api = ctk.CTkFrame(outer)
        api.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        self.lbl_api = ctk.CTkLabel(api, text="API: unknown")
        self.lbl_api.pack(side="left", padx=8)
        self.badge_api = StatusBadge(api, label="API", status="unknown")
        self.badge_api.pack(side="left", padx=(4, 8))
        ctk.CTkButton(api, text="Start API", command=self._start_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Stop API", command=self._stop_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Open Swagger", command=self._open_swagger).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Open OpenAPI JSON", command=lambda: webbrowser.open(f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/openapi.json")).pack(side="left", padx=6)

        mic = ctk.CTkFrame(outer)
        mic.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.lbl_mic = ctk.CTkLabel(mic, text="Mic listener: stopped")
        self.lbl_mic.pack(side="left", padx=8)
        self.badge_mic = StatusBadge(mic, label="Mic", status="unknown")
        self.badge_mic.pack(side="left", padx=(4, 8))
        ctk.CTkButton(mic, text="Start listener", command=self._start_mic_listener).pack(side="left", padx=6)
        ctk.CTkButton(mic, text="Stop listener", command=self._stop_mic_listener).pack(side="left", padx=6)

        gen = ctk.CTkFrame(outer)
        gen.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        gen.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(gen, text="Generative AI tools").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.services_prompt_entry = ctk.CTkEntry(gen, placeholder_text="Prompt for chat / speech / image / video tools...")
        self.services_prompt_entry.grid(row=1, column=0, columnspan=6, sticky="ew", padx=8, pady=8)
        ctk.CTkButton(gen, text="Send to Chat", command=self._services_prompt_to_chat).grid(row=2, column=0, padx=6, pady=6, sticky="w")
        ctk.CTkButton(gen, text="Speak Prompt", command=self._services_speak_prompt).grid(row=2, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkButton(gen, text="Text2Img API", command=lambda: self._call_api_json("/api/gen/txt2img", {"prompt": self._services_prompt_text()})).grid(row=2, column=2, padx=6, pady=6, sticky="w")
        ctk.CTkButton(gen, text="Video API", command=lambda: self._call_api_json("/api/gen/video", {"prompt": self._services_prompt_text(), "seconds": 4})).grid(row=2, column=3, padx=6, pady=6, sticky="w")
        ctk.CTkButton(gen, text="TTS API", command=lambda: self._call_api_json("/api/gen/tts", {"text": self._services_prompt_text(), "voice": getattr(self.settings, "voice_id", "en-US-GuyNeural")})).grid(row=2, column=4, padx=6, pady=6, sticky="w")
        ctk.CTkButton(gen, text="Model Router", command=lambda: webbrowser.open(f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/docs#/model-router")).grid(row=2, column=5, padx=6, pady=6, sticky="w")

        self.services_out = ctk.CTkTextbox(outer, wrap="word")
        self.services_out.grid(row=3, column=0, sticky="nsew", padx=8, pady=8)
        self.services_out.insert("end", "Service output will appear here.\n")

        self.after(800, self._poll_status)

    def _services_prompt_text(self) -> str:
        return self.services_prompt_entry.get().strip() if hasattr(self, "services_prompt_entry") else ""

    def _services_prompt_to_chat(self) -> None:
        prompt = self._services_prompt_text()
        if not prompt:
            return
        self.chat_entry.delete(0, "end")
        self.chat_entry.insert(0, prompt)
        self.tabs.set("Chat")
        self._send_chat()

    def _services_speak_prompt(self) -> None:
        prompt = self._services_prompt_text() or "Simian voice test online."
        self._speak(prompt)

    def _call_api_json(self, path: str, payload: Dict[str, Any]) -> None:
        payload = dict(payload or {})
        promptish = payload.get("prompt") or payload.get("text") or payload.get("input_path")
        if not promptish:
            self._set_services_output("Enter the required input first.")
            return
        self._set_services_output(f"POST {path}\n\nPayload:\n{payload}\n\nWaiting for response...")

        def worker() -> None:
            try:
                import httpx, json

                url = f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}{path}"
                r = httpx.post(url, json=payload, timeout=240)
                r.raise_for_status()
                try:
                    data = r.json()
                    pretty = json.dumps(data, indent=2)
                except Exception:
                    data = None
                    pretty = r.text
                self.after(0, lambda pretty=pretty: self._set_services_output(pretty))
                if isinstance(data, dict) and data.get("status") == "ok":
                    out_path = data.get("path") or data.get("file")
                    if out_path:
                        self.log(f"[Services] Output saved: {out_path}")
                        if path.endswith("/tts"):
                            self.task_runner.start_background(self._play_audio_file, out_path)
            except Exception as e:
                self.after(0, lambda e=e: self._set_services_output(f"Error calling {path}: {e}"))

        self.task_runner.start_background(worker)

    def _poll_status(self) -> None:
        # Hidden-tab throttle. The Services tab is the ONLY place these
        # labels are visible, so when the user is on any other tab we
        # don't need 1.8s cadence -- 6s is plenty to catch state
        # transitions the next time they flip back. Saves a port probe
        # + Vosk path lookup + label configure 2/3 of the time in a
        # normal session (Chat-heavy usage). low_resource_mode pushes
        # the hidden cadence even further (12s) so older hardware sees
        # less poll overhead too.
        visible = True
        try:
            visible = str(self.tabs.get()) == "Services"
        except Exception:
            visible = True

        api_running = port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT)
        try:
            self.lbl_api.configure(text=f"API: {'running' if api_running else 'stopped'}")
            self.badge_api.set_status("ok" if api_running else "degraded")
        except Exception:
            pass

        listener = getattr(self, "mic_listener", None)
        if listener is not None and getattr(listener, "is_running", lambda: False)():
            mic_text = "Mic listener: running"
            mic_state = "ok"
        elif MicListenerService is None or self._resolve_vosk_model_dir() is None:
            mic_text = "Mic listener: unavailable"
            mic_state = "degraded"
        else:
            mic_text = "Mic listener: stopped"
            mic_state = "unknown"
        try:
            self.lbl_mic.configure(text=mic_text)
            self.badge_mic.set_status(mic_state)
        except Exception:
            pass

        self._sync_chat_mic_controls()

        visible_ms = 1800
        hidden_ms = 6000
        try:
            from services.settings_store import load_settings as _load_settings_poll
            if bool(_load_settings_poll().get("low_resource_mode", False)):
                visible_ms = 3000
                hidden_ms = 12000
        except Exception:
            pass

        self.after(visible_ms if visible else hidden_ms, self._poll_status)
    def _start_api(self) -> None:
        if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT):
            self.log("[API] Already running.")
            return

        cmd = [sys_exe(), "-m", "uvicorn", "main:app", "--host", DEFAULT_API_HOST, "--port", str(DEFAULT_API_PORT)]
        self.log(f"[API] Starting: {' '.join(cmd)}")

        def worker() -> None:
            try:
                self.api_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT))
            except Exception as e:
                self.after(0, lambda e=e: self.log(f"[API] Start failed: {e}"))
                return

            end = time.time() + 6.0
            while time.time() < end:
                if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT):
                    self.after(0, lambda: self.log("[API] Started."))
                    return
                time.sleep(0.2)
            self.after(0, lambda: self.log("[API] Failed to bind (check logs / port conflicts)."))

        threading.Thread(target=worker, daemon=True).start()

    def _stop_api(self) -> None:
        if self.api_proc and self.api_proc.poll() is None:
            self.log("[API] Stopping...")
            try:
                self.api_proc.terminate()
            except Exception:
                pass
        self.api_proc = None

    def _open_swagger(self) -> None:
        webbrowser.open(f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/docs")

    # ----------------------- SETTINGS -----------------------

    def _build_settings(self) -> None:
        outer = ctk.CTkFrame(self.tab_settings)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        action_bar = ctk.CTkFrame(outer)
        action_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkButton(action_bar, text="Apply + Save", command=self._apply_settings).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(action_bar, text="Reload Saved", command=self._reload_settings_form).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(action_bar, text="Test TTS", command=self._test_tts_from_settings).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(action_bar, text="Start STT", command=self._start_mic_listener).pack(side="left", padx=8, pady=8)
        self.settings_status_label = ctk.CTkLabel(action_bar, text="")
        self.settings_status_label.pack(side="right", padx=8, pady=8)

        scroll = ctk.CTkScrollableFrame(outer)
        scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        scroll.grid_columnconfigure(0, weight=1)

        voice = ctk.CTkFrame(scroll)
        voice.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        voice.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(voice, text="Voice / TTS").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.chk_voice = ctk.CTkCheckBox(voice, text="TTS enabled", onvalue=1, offvalue=0)
        self.chk_voice.select() if getattr(self.settings, "voice_enabled", False) else self.chk_voice.deselect()
        self.chk_voice.grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.chk_stt = ctk.CTkCheckBox(voice, text="STT / mic listener enabled", onvalue=1, offvalue=0)
        self.chk_stt.select() if getattr(self.settings, "stt_enabled", True) else self.chk_stt.deselect()
        self.chk_stt.grid(row=1, column=1, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(voice, text="Voice ID (Edge TTS)").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.voice_id_entry = ctk.CTkEntry(voice)
        self.voice_id_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        self.voice_id_entry.insert(0, getattr(self.settings, "voice_id", ""))
        ctk.CTkLabel(voice, text="Vosk model dir").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self.vosk_model_entry = ctk.CTkEntry(voice)
        self.vosk_model_entry.grid(row=3, column=1, sticky="ew", padx=8, pady=6)
        self.vosk_model_entry.insert(0, getattr(self.settings, "vosk_model_dir", os.environ.get("VOSK_MODEL_DIR", "")))
        ctk.CTkButton(voice, text="Browse", command=self._browse_vosk_model).grid(row=3, column=2, padx=8, pady=6)

        theme = ctk.CTkFrame(scroll)
        theme.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        theme.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(theme, text="UI Theme").grid(row=0, column=0, sticky="w", padx=8, pady=8, columnspan=3)

        # Legacy accent entry retained so save/load keeps working; the
        # new palette rows below live-sync with this entry for accent.
        ctk.CTkLabel(theme, text="Accent (hex)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.accent_entry = ctk.CTkEntry(theme)
        self.accent_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        self.accent_entry.insert(0, getattr(self.settings, "theme_accent", "") or getattr(self.settings, "accent_hex", "#4da3ff"))
        ctk.CTkButton(theme, text="Pick", width=70, command=self._pick_color).grid(row=1, column=2, padx=8, pady=4)

        # Pass O: full palette editor. Each row = one theme slot, with
        # a read-only-ish hex label and a Pick button that opens the
        # standard tkinter color chooser. On pick we live-apply + write
        # through to self.settings so the preview is instant; on
        # Apply+Save the value is persisted via save_settings.
        self._theme_pick_labels: Dict[str, Any] = {}
        palette_rows = [
            ("theme_bg", "Background"),
            ("theme_panel", "Panel / frames"),
            ("theme_accent", "Accent (primary)"),
            ("theme_accent_hover", "Accent hover"),
            ("theme_text", "Text"),
            ("theme_entry", "Entry / textbox bg"),
            ("theme_log_bg", "Log / canvas bg"),
        ]
        row_base = 2
        for i, (key, label) in enumerate(palette_rows):
            r = row_base + i
            ctk.CTkLabel(theme, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=3)
            val_lbl = ctk.CTkLabel(
                theme,
                text=str(getattr(self.settings, key, "") or ""),
                anchor="w",
            )
            val_lbl.grid(row=r, column=1, sticky="ew", padx=8, pady=3)
            self._theme_pick_labels[key] = val_lbl
            ctk.CTkButton(
                theme,
                text="Pick",
                width=70,
                command=lambda k=key: self._pick_theme_color(k),
            ).grid(row=r, column=2, padx=8, pady=3)

        # Reset + quick-apply buttons.
        btns_row = row_base + len(palette_rows)
        ctk.CTkButton(
            theme,
            text="Reset to defaults",
            command=self._reset_theme_to_defaults,
        ).grid(row=btns_row, column=0, sticky="w", padx=8, pady=(10, 8))
        ctk.CTkButton(
            theme,
            text="Apply theme now",
            command=self._apply_and_save_theme,
        ).grid(row=btns_row, column=1, sticky="w", padx=8, pady=(10, 8))

        clips = ctk.CTkFrame(scroll)
        clips.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        clips.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(clips, text="Replay buffer / Clips").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.entry_clips_dir = ctk.CTkEntry(clips)
        self.entry_clips_dir.grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        self.entry_clips_dir.insert(0, getattr(self.settings, "clips_dir", "data/clips"))
        ctk.CTkLabel(clips, text="Clips dir").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.entry_buffer_min = ctk.CTkEntry(clips, width=120)
        self.entry_buffer_min.grid(row=2, column=1, sticky="w", padx=8, pady=6)
        self.entry_buffer_min.insert(0, str(getattr(self.settings, "replay_minutes", 5)))
        ctk.CTkLabel(clips, text="Replay minutes (default 5)").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.entry_seg_sec = ctk.CTkEntry(clips, width=120)
        self.entry_seg_sec.grid(row=3, column=1, sticky="w", padx=8, pady=6)
        self.entry_seg_sec.insert(0, str(getattr(self.settings, "segment_seconds", 5)))
        ctk.CTkLabel(clips, text="Segment seconds").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self.entry_fps = ctk.CTkEntry(clips, width=120)
        self.entry_fps.grid(row=4, column=1, sticky="w", padx=8, pady=6)
        self.entry_fps.insert(0, str(getattr(self.settings, "fps", 30)))
        ctk.CTkLabel(clips, text="FPS").grid(row=4, column=0, sticky="w", padx=8, pady=6)
        self.entry_res = ctk.CTkEntry(clips, width=200)
        self.entry_res.grid(row=5, column=1, sticky="w", padx=8, pady=6)
        self.entry_res.insert(0, f"{getattr(self.settings, 'width', 1920)}x{getattr(self.settings, 'height', 1080)}")
        ctk.CTkLabel(clips, text="Resolution (WxH)").grid(row=5, column=0, sticky="w", padx=8, pady=6)
        self.upscale_opt = ctk.CTkOptionMenu(clips, values=["none", "1080p", "4k"])
        self.upscale_opt.set(getattr(self.settings, "export_upscale", "none"))
        self.upscale_opt.grid(row=6, column=1, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(clips, text="Export upscale").grid(row=6, column=0, sticky="w", padx=8, pady=6)

        news = ctk.CTkFrame(scroll)
        news.grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        news.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(news, text="News refresh seconds").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.entry_news_refresh = ctk.CTkEntry(news, width=140)
        self.entry_news_refresh.grid(row=0, column=1, sticky="w", padx=8, pady=8)
        self.entry_news_refresh.insert(0, str(getattr(self.settings, "news_refresh_seconds", 300)))

        # -- Screen Awareness ------------------------------------------------
        # Opt-in feature: off by default. Single-section layout that matches
        # the surrounding voice/theme/clips/news frames. No new tab.
        screen = ctk.CTkFrame(scroll)
        screen.grid(row=4, column=0, sticky="ew", padx=8, pady=8)
        screen.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(screen, text="Screen Awareness").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.chk_screen_awareness = ctk.CTkCheckBox(
            screen, text="Enable (capture on demand, off by default)", onvalue=1, offvalue=0
        )
        if bool(getattr(self.settings, "screen_awareness_enabled", False)):
            self.chk_screen_awareness.select()
        else:
            self.chk_screen_awareness.deselect()
        self.chk_screen_awareness.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(
            screen,
            text="Exclude window titles (comma-separated substrings)",
        ).grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.entry_screen_exclusions = ctk.CTkEntry(screen)
        self.entry_screen_exclusions.grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        current_exclusions = getattr(self.settings, "screen_awareness_exclusions", []) or []
        self.entry_screen_exclusions.insert(0, ", ".join(str(x) for x in current_exclusions))

        # Vision call tuning. Left alone the defaults (180s / 1280px)
        # cover a cold-start thinking model comfortably; power users on
        # slow hardware may want to raise the timeout, and users who
        # care about throughput can shrink the max dimension further.
        ctk.CTkLabel(
            screen,
            text="Vision timeout (seconds)",
        ).grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self.entry_screen_vision_timeout = ctk.CTkEntry(screen, width=120)
        self.entry_screen_vision_timeout.grid(row=3, column=1, sticky="w", padx=8, pady=6)
        self.entry_screen_vision_timeout.insert(
            0, str(getattr(self.settings, "screen_awareness_vision_timeout_sec", 180))
        )
        ctk.CTkLabel(
            screen,
            text="Vision max image dimension (px, 0 = no downscale)",
        ).grid(row=4, column=0, sticky="w", padx=8, pady=6)
        self.entry_screen_vision_max_dim = ctk.CTkEntry(screen, width=120)
        self.entry_screen_vision_max_dim.grid(row=4, column=1, sticky="w", padx=8, pady=6)
        self.entry_screen_vision_max_dim.insert(
            0, str(getattr(self.settings, "screen_awareness_vision_max_dim", 1280))
        )

        ctk.CTkButton(scroll, text="Open audio device picker", command=self._open_audio_device_picker).grid(row=5, column=0, sticky="w", padx=8, pady=12)

    def _darken_hex(self, color: str, factor: float = 0.85) -> str:
        color = (color or "#4da3ff").lstrip("#")
        if len(color) != 6:
            return "#3b82f6"
        r = max(0, min(255, int(int(color[0:2], 16) * factor)))
        g = max(0, min(255, int(int(color[2:4], 16) * factor)))
        b = max(0, min(255, int(int(color[4:6], 16) * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ---- Global theme system (Pass O) ---------------------------------
    # Resolves the current palette from settings, applying backwards-compat
    # bridging between the legacy ``accent_hex`` key and the new
    # ``theme_accent`` key. Returns a dict with every theme slot filled
    # so downstream callers never have to worry about None / empty.
    def _current_theme(self) -> Dict[str, str]:
        try:
            from services.settings_store import THEME_DEFAULTS
        except Exception:
            THEME_DEFAULTS = {
                "theme_bg": "#1a1625",
                "theme_panel": "#2a2333",
                "theme_accent": "#4da3ff",
                "theme_accent_hover": "#3b82f6",
                "theme_text": "#e4e2ea",
                "theme_entry": "#1e1a28",
                "theme_log_bg": "#14111c",
            }
        s = self.settings
        # theme_accent wins; fall back to legacy accent_hex for users
        # upgrading from pre-Pass-O settings.json.
        accent = (getattr(s, "theme_accent", "") or getattr(s, "accent_hex", "") or THEME_DEFAULTS["theme_accent"]).strip()
        hover = (getattr(s, "theme_accent_hover", "") or self._darken_hex(accent, 0.82)).strip()
        return {
            "bg": (getattr(s, "theme_bg", "") or THEME_DEFAULTS["theme_bg"]).strip(),
            "panel": (getattr(s, "theme_panel", "") or THEME_DEFAULTS["theme_panel"]).strip(),
            "accent": accent,
            "hover": hover,
            "text": (getattr(s, "theme_text", "") or THEME_DEFAULTS["theme_text"]).strip(),
            "entry": (getattr(s, "theme_entry", "") or THEME_DEFAULTS["theme_entry"]).strip(),
            "log_bg": (getattr(s, "theme_log_bg", "") or THEME_DEFAULTS["theme_log_bg"]).strip(),
        }

    def _apply_theme(self, root_widget: Optional[Any] = None) -> None:
        """Recursively apply the current palette to every widget under
        ``root_widget`` (defaults to the main window).

        Pass Q: time the walk and warn over 50ms; coalesce rapid calls
        with a 60ms debounce so a settings-apply flurry doesn't trigger
        N back-to-back full traversals. Skip entirely while the user is
        actively scrolling the log (cheap heuristic that keeps scrolls
        smooth).
        """
        # Coalesce: when called with no root, debounce; consecutive
        # global theme calls inside a 60ms window are merged. Per-tab
        # calls (root_widget != None) still run immediately because
        # those are typically lazy-build hand-offs.
        if root_widget is None:
            now = time.perf_counter()
            last = getattr(self, "_theme_last_global_ts", 0.0)
            if now - last < 0.06:
                return
            self._theme_last_global_ts = now
        # Skip while user is actively scrolling the log textbox -- the
        # walk is the single most expensive sync op the GUI runs and
        # we don't need it to finish to keep visuals correct.
        try:
            if getattr(self._ui_logger, "_scroll_locked", False) and root_widget is None:
                return
        except Exception:
            pass
        _theme_t0 = time.perf_counter()
        palette = self._current_theme()
        accent = palette["accent"]
        hover = palette["hover"]
        bg = palette["bg"]
        panel = palette["panel"]
        text_col = palette["text"]
        entry_col = palette["entry"]
        log_bg = palette["log_bg"]

        if root_widget is None:
            root_widget = self
            # Root window (Tk) background. CTk ignores this on child
            # frames, but setting it on the root removes the brief
            # white flash during tab rebuilds.
            try:
                self.configure(fg_color=bg)
            except Exception:
                pass
            # Tabview segmented button (tab header row).
            try:
                self.tabs._segmented_button.configure(
                    selected_color=accent, selected_hover_color=hover
                )
            except Exception:
                pass
            try:
                self.tabs.configure(fg_color=panel, segmented_button_fg_color=panel)
            except Exception:
                pass
            # 4D Lab canvas: it's a raw tk.Canvas, not CTk, so it doesn't
            # get hit by the recursive walk below.
            canvas = getattr(self, "canvas", None)
            if canvas is not None:
                try:
                    canvas.configure(bg=log_bg)
                    # Redraw the label text + oval fill with theme colors
                    # if the SRM is running. Points + text get recreated
                    # on the next _srm_tick pass so this only fixes the
                    # static background until then.
                    canvas.itemconfig("all", fill=accent)
                except Exception:
                    pass
            # News link tags (CTkTextbox, but color applied via tag_config
            # which is raw Tk). Re-apply to every existing link_* tag.
            news_box = getattr(self, "news_box", None)
            if news_box is not None:
                try:
                    for tag in news_box.tag_names():
                        if str(tag).startswith("link_"):
                            news_box.tag_config(tag, foreground=accent)
                except Exception:
                    pass

        def apply_widget(widget: Any) -> None:
            try:
                if isinstance(widget, ctk.CTkButton):
                    widget.configure(fg_color=accent, hover_color=hover, text_color=text_col)
                elif isinstance(widget, ctk.CTkOptionMenu):
                    widget.configure(
                        fg_color=accent,
                        button_color=accent,
                        button_hover_color=hover,
                        text_color=text_col,
                    )
                elif isinstance(widget, ctk.CTkCheckBox):
                    widget.configure(fg_color=accent, hover_color=hover, border_color=accent, text_color=text_col)
                elif isinstance(widget, ctk.CTkEntry):
                    widget.configure(fg_color=entry_col, text_color=text_col, border_color=panel)
                elif isinstance(widget, ctk.CTkTextbox):
                    widget.configure(fg_color=log_bg, text_color=text_col, border_color=panel)
                elif isinstance(widget, ctk.CTkScrollableFrame):
                    widget.configure(fg_color=panel)
                elif isinstance(widget, ctk.CTkFrame):
                    widget.configure(fg_color=panel)
                elif isinstance(widget, ctk.CTkLabel):
                    widget.configure(text_color=text_col)
            except Exception:
                pass
            try:
                for child in widget.winfo_children():
                    apply_widget(child)
            except Exception:
                pass

        apply_widget(root_widget)

        # Pass Q: surface a slow theme walk so a runaway widget tree
        # shows up immediately. 50ms is the perceived-smoothness floor;
        # the walk usually clocks ~5-15ms even on a hot main window.
        try:
            _theme_dt_ms = (time.perf_counter() - _theme_t0) * 1000.0
            if _theme_dt_ms >= 50.0:
                scope = "global" if root_widget is self else getattr(root_widget, "_name", "scoped")
                self.log(f"[Perf] _apply_theme ({scope}) took {_theme_dt_ms:.0f}ms.")
        except Exception:
            pass

    # Backwards-compat shim. Older code (and several call sites below)
    # still invokes ``_apply_accent_color`` / ``_apply_accent_to_widget``;
    # both now just forward to the full theme applier so nothing is
    # un-themed after a settings apply.
    def _apply_accent_color(self) -> None:
        self._apply_theme()

    def _apply_accent_to_widget(self, root_widget: Any, accent: Optional[str] = None, hover: Optional[str] = None) -> None:
        # Kept for signature compatibility; the accent/hover args are
        # ignored because the central palette is authoritative now. A
        # caller that really wants a one-off color override can just
        # configure the widget directly.
        self._apply_theme(root_widget)
    def _browse_vosk_model(self) -> None:
        import tkinter.filedialog as fd
        dp = fd.askdirectory(title="Pick Vosk model directory")
        if dp:
            self.vosk_model_entry.delete(0, "end")
            self.vosk_model_entry.insert(0, dp)

    def _reload_settings_form(self) -> None:
        self.settings = load_settings()
        self.tabs.delete("Settings")
        self.tab_settings = self.tabs.add("Settings")
        self._build_settings()
        self._apply_accent_color()
        self.tabs.set("Settings")

    def _test_tts_from_settings(self) -> None:
        self._apply_settings(restart_listener=False)
        self._speak("Simian voice test online.")

    def _resolve_vosk_model_dir(self) -> Optional[Path]:
        raw = (getattr(self.settings, "vosk_model_dir", "") or os.environ.get("VOSK_MODEL_DIR", "")).strip().strip('"')
        if raw:
            p = Path(raw)
            if p.exists():
                return p
        candidates = [
            REPO_ROOT / "voice" / "vosk-model-small-en-us-0.15",
            REPO_ROOT / "voice" / "vosk-model-en-us-0.22",
            REPO_ROOT / "models" / "vosk-model-small-en-us-0.15",
            REPO_ROOT / "models" / "vosk-model-en-us-0.22",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None
    def _pick_color(self) -> None:
        import tkinter.colorchooser as cc

        col = cc.askcolor(title="Pick accent color")
        if col and col[1]:
            self.accent_entry.delete(0, "end")
            self.accent_entry.insert(0, col[1])
            # Keep the new palette accent in sync with the legacy entry.
            try:
                self.settings.theme_accent = col[1]
                if "theme_accent" in getattr(self, "_theme_pick_labels", {}):
                    self._theme_pick_labels["theme_accent"].configure(text=col[1])
                self._apply_theme()
            except Exception:
                pass

    # -- Pass O: per-slot color picker used by the Settings Theme frame
    # and the global palette popup. Live-applies the picked color
    # (preview) and updates the small label next to the pick button so
    # the user sees what they chose without reopening the dialog.
    def _pick_theme_color(self, key: str) -> None:
        import tkinter.colorchooser as cc

        initial = getattr(self.settings, key, "") or ""
        col = cc.askcolor(
            color=initial if initial else None,
            title=f"Pick {key.replace('theme_', '').replace('_', ' ')} color",
        )
        if not (col and col[1]):
            return
        chosen = col[1]
        try:
            setattr(self.settings, key, chosen)
        except Exception:
            pass
        lbls = getattr(self, "_theme_pick_labels", {})
        if key in lbls:
            try:
                lbls[key].configure(text=chosen)
            except Exception:
                pass
        # Mirror accent writes into the legacy accent_entry so the
        # existing save path keeps emitting an accent_hex value.
        if key == "theme_accent" and hasattr(self, "accent_entry"):
            try:
                self.accent_entry.delete(0, "end")
                self.accent_entry.insert(0, chosen)
            except Exception:
                pass
        try:
            self._apply_theme()
        except Exception:
            pass

    def _reset_theme_to_defaults(self) -> None:
        """Reset every theme_* key to the THEME_DEFAULTS in
        settings_store, refresh the Settings labels, and live-apply."""
        try:
            from services.settings_store import THEME_DEFAULTS
        except Exception:
            return
        for k, v in THEME_DEFAULTS.items():
            try:
                setattr(self.settings, k, v)
            except Exception:
                pass
        # Keep accent_hex (legacy) in step with theme_accent.
        try:
            self.settings.accent_hex = THEME_DEFAULTS.get("theme_accent", "#4da3ff")
        except Exception:
            pass
        lbls = getattr(self, "_theme_pick_labels", {})
        for k, lbl in lbls.items():
            try:
                lbl.configure(text=THEME_DEFAULTS.get(k, ""))
            except Exception:
                pass
        if hasattr(self, "accent_entry"):
            try:
                self.accent_entry.delete(0, "end")
                self.accent_entry.insert(0, THEME_DEFAULTS.get("theme_accent", "#4da3ff"))
            except Exception:
                pass
        try:
            self._apply_theme()
        except Exception:
            pass

    def _apply_and_save_theme(self) -> None:
        """Apply the current palette live AND persist it.

        Does not call the full _apply_settings path -- we only want
        theme side effects, not a mic listener restart, when the user
        clicks the theme-only apply button.
        """
        try:
            save_settings(self.settings)
        except Exception as e:
            self.log(f"[Theme] Save failed: {e}")
            return
        try:
            self._apply_theme()
            self.log("[Theme] Saved and applied globally.")
        except Exception as e:
            self.log(f"[Theme] Apply failed: {e}")

    # Pass O: global palette popup reachable from the top-bar "Theme"
    # button. Lives as a CTkToplevel; all rows share the same
    # _pick_theme_color / _reset_theme_to_defaults / _apply_and_save_theme
    # helpers that the Settings tab uses, so behavior is identical.
    # Idempotent: clicking the button twice raises the existing popup
    # instead of stacking duplicates.
    def _open_theme_popup(self) -> None:
        existing = getattr(self, "_theme_popup", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return
            except Exception:
                pass

        try:
            from services.settings_store import THEME_KEYS, THEME_DEFAULTS
        except Exception:
            THEME_KEYS = (
                "theme_bg", "theme_panel", "theme_accent",
                "theme_accent_hover", "theme_text", "theme_entry",
                "theme_log_bg",
            )
            THEME_DEFAULTS = {}

        win = ctk.CTkToplevel(self)
        win.title("Simian — Theme")
        win.geometry("420x400")
        # Popup-scoped label registry so _pick_theme_color can update
        # either the Settings row or the popup row depending on which
        # label exists when the callback fires. We merge the popup
        # labels into the existing dict so _pick_theme_color stays
        # agnostic about where the row lives.
        if not hasattr(self, "_theme_pick_labels"):
            self._theme_pick_labels = {}

        root = ctk.CTkFrame(win)
        root.pack(fill="both", expand=True, padx=10, pady=10)
        root.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(root, text="Global theme").grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 8))

        label_for = {
            "theme_bg": "Background",
            "theme_panel": "Panel / frames",
            "theme_accent": "Accent (primary)",
            "theme_accent_hover": "Accent hover",
            "theme_text": "Text",
            "theme_entry": "Entry / textbox bg",
            "theme_log_bg": "Log / canvas bg",
        }
        for i, key in enumerate(THEME_KEYS):
            r = i + 1
            ctk.CTkLabel(root, text=label_for.get(key, key)).grid(row=r, column=0, sticky="w", padx=6, pady=3)
            lbl = ctk.CTkLabel(root, text=str(getattr(self.settings, key, "") or ""), anchor="w")
            lbl.grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            # Pick-button callback: update popup label, live-apply, and
            # also push into the Settings tab row if it exists.
            self._theme_pick_labels[key] = lbl  # latest label wins for the key
            ctk.CTkButton(
                root,
                text="Pick",
                width=70,
                command=lambda k=key: self._pick_theme_color(k),
            ).grid(row=r, column=2, padx=6, pady=3)

        btn_row = len(THEME_KEYS) + 1
        ctk.CTkButton(
            root,
            text="Reset to defaults",
            command=self._reset_theme_to_defaults,
        ).grid(row=btn_row, column=0, sticky="w", padx=6, pady=(12, 4))
        ctk.CTkButton(
            root,
            text="Apply + Save",
            command=self._apply_and_save_theme,
        ).grid(row=btn_row, column=1, sticky="w", padx=6, pady=(12, 4))
        ctk.CTkButton(
            root,
            text="Close",
            command=win.destroy,
            width=80,
        ).grid(row=btn_row, column=2, sticky="e", padx=6, pady=(12, 4))

        self._theme_popup = win

        # Apply current theme to the popup surface itself so it
        # matches the rest of the app immediately on open.
        try:
            self._apply_theme(win)
        except Exception:
            pass

    def _open_audio_device_picker(self) -> None:
        if list_sounddevice_devices is None or list_dshow_audio_devices is None:
            self.log("[Audio] Device picker unavailable (services.audio_devices import failed).")
            return
        try:
            sd_info = list_sounddevice_devices()
            dshow_names = list_dshow_audio_devices()
            # Categorized helpers: put the WASAPI-loopback sentinel first,
            # then all dshow devices, then sounddevice inputs whose name
            # looks like a loopback/monitor. This is the list the FFmpeg
            # pipeline can actually consume; the raw dshow-only list that
            # used to power this picker hid the sentinel entirely, which
            # is why "Replay desktop audio" looked broken on modern
            # Windows machines without Stereo Mix enabled.
            if list_replay_system_choices is not None:
                replay_sys_choices = list_replay_system_choices()
            else:
                replay_sys_choices = [DEFAULT_WASAPI_SYSTEM] + list(dshow_names)
            if list_replay_mic_choices is not None:
                replay_mic_choices = list_replay_mic_choices()
            else:
                replay_mic_choices = [""] + list(dshow_names)
        except Exception as e:
            self.log(f"[Audio] Could not query devices: {e}")
            return

        win = ctk.CTkToplevel(self)
        win.title("Simian audio device picker")
        win.geometry("900x560")
        win.transient(self)
        win.grab_set()
        win.grid_columnconfigure(1, weight=1)
        win.grid_rowconfigure(4, weight=1)

        inputs = ["default"] + [f"{d['index']} | {d['name']}" for d in sd_info.get('inputs', [])]
        outputs = ["default"] + [f"{d['index']} | {d['name']}" for d in sd_info.get('outputs', [])]

        def label_for_sys(value: str) -> str:
            return DEFAULT_WASAPI_LABEL if value == DEFAULT_WASAPI_SYSTEM else (value or "")

        def value_for_sys_label(label: str) -> str:
            return DEFAULT_WASAPI_SYSTEM if label == DEFAULT_WASAPI_LABEL else label

        def label_for_mic(value: str) -> str:
            return "(none - skip microphone)" if value == "" else value

        def value_for_mic_label(label: str) -> str:
            return "" if label == "(none - skip microphone)" else label

        sys_labels = [label_for_sys(v) for v in replay_sys_choices] or [DEFAULT_WASAPI_LABEL]
        mic_labels = [label_for_mic(v) for v in replay_mic_choices] or ["(none - skip microphone)"]

        stt_current = str(getattr(self.settings, "stt_input_device", "default") or "default")
        tts_current = str(getattr(self.settings, "tts_output_device", "default") or "default")
        replay_sys_current = str(getattr(self.settings, "replay_system_audio_device", "") or "")
        replay_mic_current = str(getattr(self.settings, "replay_mic_device", "") or "")

        if not replay_sys_current:
            replay_sys_current = DEFAULT_WASAPI_SYSTEM

        def normalize_choice(values, current):
            if current in values:
                return current
            for value in values:
                if current and str(value).startswith(str(current)):
                    return value
            return values[0] if values else ""

        ctk.CTkLabel(win, text="STT input device").grid(row=0, column=0, sticky="w", padx=12, pady=10)
        stt_opt = ctk.CTkOptionMenu(win, values=inputs)
        stt_opt.grid(row=0, column=1, sticky="ew", padx=12, pady=10)
        stt_opt.set(normalize_choice(inputs, stt_current))

        ctk.CTkLabel(win, text="TTS output device").grid(row=1, column=0, sticky="w", padx=12, pady=10)
        tts_opt = ctk.CTkOptionMenu(win, values=outputs)
        tts_opt.grid(row=1, column=1, sticky="ew", padx=12, pady=10)
        tts_opt.set(normalize_choice(outputs, tts_current))

        ctk.CTkLabel(win, text="Replay desktop audio").grid(row=2, column=0, sticky="w", padx=12, pady=10)
        sys_opt = ctk.CTkOptionMenu(win, values=sys_labels)
        sys_opt.grid(row=2, column=1, sticky="ew", padx=12, pady=10)
        sys_opt.set(normalize_choice(sys_labels, label_for_sys(replay_sys_current)))

        ctk.CTkLabel(win, text="Replay microphone").grid(row=3, column=0, sticky="w", padx=12, pady=10)
        mic_opt = ctk.CTkOptionMenu(win, values=mic_labels)
        mic_opt.grid(row=3, column=1, sticky="ew", padx=12, pady=10)
        mic_opt.set(normalize_choice(mic_labels, label_for_mic(replay_mic_current)))

        textbox = ctk.CTkTextbox(win, wrap="word")
        textbox.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=12, pady=12)
        textbox.insert(
            "end",
            "Desktop audio picks:\n"
            "- 'Default (auto-detect desktop audio)' maps to the WASAPI loopback \n"
            "  probe in ReplayBufferRecorder; it falls back to screen-only if no \n"
            "  loopback input is available.\n"
            "- DirectShow names come from 'ffmpeg -list_devices true -f dshow'. Stereo \n"
            "  Mix, 'What U Hear', or VB-Audio Cable are the usual picks on Windows.\n\n",
        )
        textbox.insert("end", "Replay desktop-audio choices:\n")
        for value in replay_sys_choices:
            textbox.insert("end", f"- {label_for_sys(value)}\n")
        textbox.insert("end", "\nReplay microphone choices:\n")
        for value in replay_mic_choices:
            textbox.insert("end", f"- {label_for_mic(value)}\n")
        textbox.insert("end", "\nAvailable sounddevice inputs:\n")
        for d in sd_info.get("inputs", []):
            textbox.insert("end", f"- {d['index']} | {d['name']}\n")
        textbox.insert("end", "\nAvailable sounddevice outputs:\n")
        for d in sd_info.get("outputs", []):
            textbox.insert("end", f"- {d['index']} | {d['name']}\n")
        textbox.insert("end", "\nAvailable FFmpeg DirectShow audio names:\n")
        for name in dshow_names:
            textbox.insert("end", f"- {name}\n")

        def use_best_guess() -> None:
            if pick_best_system_audio_choice is None:
                self.log("[Audio] Best-guess helper unavailable.")
                return
            try:
                guess = pick_best_system_audio_choice()
            except Exception as exc:
                self.log(f"[Audio] Best-guess failed: {exc}")
                return
            label = label_for_sys(guess)
            if label in sys_labels:
                current = sys_opt.get()
                sys_opt.set(label)
                # Only log when the guess actually changes the picker
                # selection. Repeat presses of the 'Best guess' button
                # used to spam identical log lines on every click.
                if label != current:
                    self.log(f"[Audio] Best-guess desktop audio: {label}")
            else:
                self.log(
                    f"[Audio] Best-guess returned '{guess}' which is not in the current choice list."
                )

        def apply_and_close() -> None:
            self.settings.stt_input_device = stt_opt.get()
            self.settings.tts_output_device = tts_opt.get()
            # Round-trip friendly labels back to the raw values that
            # ReplayBufferRecorder / FFmpeg actually understand, so the
            # __DEFAULT_WASAPI__ sentinel keeps triggering the auto
            # loopback-detect branch in replay_buffer.py.
            self.settings.replay_system_audio_device = value_for_sys_label(sys_opt.get())
            self.settings.replay_mic_device = value_for_mic_label(mic_opt.get())
            save_settings(self.settings)
            self.log("[Audio] Device preferences saved.")
            win.destroy()

        btn = ctk.CTkFrame(win)
        btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        ctk.CTkButton(btn, text="Save device choices", command=apply_and_close).pack(side="left", padx=8)
        ctk.CTkButton(btn, text="Best guess desktop audio", command=use_best_guess).pack(side="left", padx=8)
        ctk.CTkButton(btn, text="Close", command=win.destroy).pack(side="left", padx=8)

        # Propagate the user's accent color onto this toplevel. CTkToplevel
        # is a sibling of the root window in the widget tree, so the main
        # _apply_accent_color walk (which starts at ``self``) never reaches
        # it. Calling the extracted helper explicitly keeps the picker
        # visually consistent with the rest of the app.
        try:
            self._apply_accent_to_widget(win)
        except Exception:
            pass

    def _apply_settings(self, restart_listener: bool = True) -> None:


        old = {
            "clips_dir": getattr(self.settings, "clips_dir", "data/clips"),
            "replay_minutes": getattr(self.settings, "replay_minutes", 5),
            "segment_seconds": getattr(self.settings, "segment_seconds", 5),
            "fps": getattr(self.settings, "fps", 30),
            "width": getattr(self.settings, "width", 1920),
            "height": getattr(self.settings, "height", 1080),
            "export_upscale": getattr(self.settings, "export_upscale", "none"),
        }
        # Snapshot listener-relevant fields so we can skip a needless
        # stop/start cycle when nothing that affects the mic listener
        # changed (priority: kill mic-listener thrash on every apply).
        prev_listener_cfg = (
            getattr(self.settings, "stt_input_device", None),
            getattr(self.settings, "wake_word", "simian"),
            getattr(self.settings, "stt_enabled", True),
            getattr(self.settings, "voice_enabled", False),
        )

        self.settings.voice_enabled = self.chk_voice.get() == 1
        self.settings.stt_enabled = self.chk_stt.get() == 1
        self.settings.voice_id = self.voice_id_entry.get().strip() or getattr(self.settings, "voice_id", "")
        self.settings.vosk_model_dir = self.vosk_model_entry.get().strip()
        # Accent written to both legacy accent_hex AND new theme_accent
        # so consumers of either key see the same value. theme_* fields
        # that got picked via _pick_theme_color were already mutated
        # in-place on self.settings, so save_settings below persists
        # them as-is.
        _accent_entry_val = self.accent_entry.get().strip() or getattr(self.settings, "theme_accent", "") or getattr(self.settings, "accent_hex", "#4da3ff")
        self.settings.accent_hex = _accent_entry_val
        self.settings.theme_accent = _accent_entry_val
        self.settings.clips_dir = self.entry_clips_dir.get().strip() or getattr(self.settings, "clips_dir", "data/clips")
        self.settings.news_default_category = self.news_category.get()

        # Screen Awareness -- only present if _build_settings put the row
        # in. Guard with hasattr so older partial forms don't break apply.
        if hasattr(self, "chk_screen_awareness"):
            self.settings.screen_awareness_enabled = self.chk_screen_awareness.get() == 1
        if hasattr(self, "entry_screen_exclusions"):
            raw = self.entry_screen_exclusions.get().strip()
            self.settings.screen_awareness_exclusions = [
                p.strip() for p in raw.split(",") if p.strip()
            ] if raw else []
        if hasattr(self, "entry_screen_vision_timeout"):
            try:
                v = int(self.entry_screen_vision_timeout.get().strip())
                if v > 0:
                    self.settings.screen_awareness_vision_timeout_sec = v
            except Exception:
                pass
        if hasattr(self, "entry_screen_vision_max_dim"):
            try:
                v = int(self.entry_screen_vision_max_dim.get().strip())
                if v >= 0:
                    self.settings.screen_awareness_vision_max_dim = v
            except Exception:
                pass

        try:
            self.settings.replay_minutes = int(self.entry_buffer_min.get().strip())
        except Exception:
            pass
        try:
            self.settings.segment_seconds = int(self.entry_seg_sec.get().strip())
        except Exception:
            pass
        try:
            self.settings.fps = int(self.entry_fps.get().strip())
        except Exception:
            pass
        try:
            w, h = self.entry_res.get().strip().lower().split("x")
            self.settings.width = int(w)
            self.settings.height = int(h)
        except Exception:
            pass

        self.settings.export_upscale = self.upscale_opt.get()
        try:
            self.settings.news_refresh_seconds = int(self.entry_news_refresh.get().strip())
        except Exception:
            pass

        if self.settings.vosk_model_dir:
            os.environ["VOSK_MODEL_DIR"] = self.settings.vosk_model_dir

        save_settings(self.settings)
        self._apply_accent_color()
        self._refresh_clips()
        self._apply_news_filter()
        # Push the (possibly new) screen-awareness flag into the runtime
        # service so the next chat/voice intent sees the current value.
        self._sync_screen_awareness_from_settings()

        replay_changed = any(old[k] != getattr(self.settings, k) for k in old)
        if replay_changed and self.replay.is_running():
            try:
                self._stop_replay_buffer()
                self._start_replay_buffer()
            except Exception as e:
                self.log(f"[Replay] Restart after settings change failed: {e}")

        cur_listener_cfg = (
            getattr(self.settings, "stt_input_device", None),
            getattr(self.settings, "wake_word", "simian"),
            getattr(self.settings, "stt_enabled", True),
            getattr(self.settings, "voice_enabled", False),
        )
        if restart_listener and cur_listener_cfg != prev_listener_cfg:
            self._stop_mic_listener()
            if self.settings.stt_enabled:
                self._start_mic_listener()

        stamp = time.strftime("%H:%M:%S")
        self.log("Settings applied and saved.")
        try:
            self.settings_status_label.configure(text=f"Saved at {stamp}")
        except Exception:
            pass

    # ----------------------- MIC LISTENER -----------------------

    def _start_mic_listener(self, hot_mode: bool = False) -> None:
        model_dir = self._resolve_vosk_model_dir()
        if model_dir is not None:
            os.environ["VOSK_MODEL_DIR"] = str(model_dir)
        if MicListenerService is None:
            self.lbl_mic.configure(text="Mic listener: unavailable")
            self.log("[Voice] Mic listener unavailable (install vosk + sounddevice and provide a Vosk model folder).")
            self._sync_chat_mic_controls()
            return

        device_index = self._get_selected_input_device()
        cfg = MicListenerConfig(device=device_index, wake_word=getattr(self.settings, "wake_word", "simian")) if MicListenerConfig is not None else None

        recreate = False
        if self.mic_listener is None:
            recreate = True
        else:
            current_cfg = getattr(self.mic_listener, "config", None)
            if current_cfg is None or getattr(current_cfg, "device", None) != device_index or getattr(current_cfg, "wake_word", None) != getattr(self.settings, "wake_word", "simian"):
                try:
                    self.mic_listener.stop()
                except Exception:
                    pass
                recreate = True

        if recreate:
            self.mic_listener = MicListenerService(
                log_cb=self.log,
                command_cb=self._on_voice_command,
                transcript_cb=self._on_voice_transcript,
                config=cfg,
            )

        listener = self.mic_listener
        if listener is None:
            self.lbl_mic.configure(text="Mic listener: unavailable")
            self._sync_chat_mic_controls()
            return

        if not listener.available():
            reason = listener.unavailable_reason()
            self.lbl_mic.configure(text="Mic listener: unavailable")
            if model_dir is None:
                self.log("[Voice] Mic listener unavailable: set VOSK_MODEL_DIR or choose a model in Settings.")
            self.log(f"[Voice] {reason}")
            # Listener never came up -- keep hot mic state honest.
            self._chat_mic_hot_mode = False
            self._sync_chat_mic_controls()
            return

        is_running_fn = getattr(listener, "is_running", lambda: False)
        last_error_fn = getattr(listener, "last_error", lambda: None)

        if not is_running_fn():
            listener.start()
            # start() spawns a daemon thread; the actual input-stream open
            # (which can raise on a stale PortAudio device id) happens
            # inside that thread. Poll briefly so we report runtime truth
            # instead of optimistic 'running' state (priority #3).
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if is_running_fn():
                    break
                if last_error_fn():
                    break
                time.sleep(0.05)

        if not is_running_fn():
            err = last_error_fn() or "listener thread did not start"
            self.log(f"[Voice] Mic listener did not start: {err}")
            self._chat_mic_hot_mode = False
            try:
                self.lbl_mic.configure(text="Mic listener: unavailable")
            except Exception:
                pass
            self._sync_chat_mic_controls()
            return

        # Only now that the stream is confirmed open do we commit the
        # hot-mic state. This prevents a 'Mic On' UI with no working mic.
        if hasattr(listener, "set_hot_mode"):
            try:
                listener.set_hot_mode(bool(hot_mode))
            except Exception:
                pass
        self._chat_mic_hot_mode = bool(hot_mode)
        self.lbl_mic.configure(text="Mic listener: running")
        self._sync_chat_mic_controls()

    def _stop_mic_listener(self) -> None:
        listener = self.mic_listener
        self._chat_mic_hot_mode = False
        if listener is not None:
            try:
                if hasattr(listener, "set_hot_mode"):
                    listener.set_hot_mode(False)
            except Exception:
                pass
            listener.stop()
        self.lbl_mic.configure(text="Mic listener: stopped")
        self._sync_chat_mic_controls()

    def _pause_mic_listener_for_replay(self) -> None:
        """Pause STT so the replay buffer's AudioFallbackRecorder can
        claim the same default mic device. Called by the replay
        recorder via its stt_pause_cb hook (Pass R-C). Idempotent --
        safe to invoke multiple times across the same fallback session.
        """
        listener = getattr(self, "mic_listener", None)
        if listener is None:
            return
        if not getattr(listener, "is_running", lambda: False)():
            return
        pause_fn = getattr(listener, "pause", None)
        if pause_fn is None:
            return
        try:
            pause_fn()
        except Exception as exc:
            self.log(f"[STT] Pause failed: {exc}")

    def _resume_mic_listener_after_replay(self) -> None:
        """Reopen the STT input stream after the replay fallback has
        released the mic. Called by the replay recorder via
        stt_resume_cb (Pass R-C). The listener thread reopens the
        stream on its own once the pause flag clears -- this just flips
        that flag and lets _run() take over.
        """
        listener = getattr(self, "mic_listener", None)
        if listener is None:
            return
        resume_fn = getattr(listener, "resume", None)
        if resume_fn is None:
            return
        try:
            resume_fn()
        except Exception as exc:
            self.log(f"[STT] Resume failed: {exc}")

    def _on_voice_command(self, cmd: str, meta: Dict[str, Any]) -> None:
        # Pass R-D: log accept/reject for every voice command that
        # reaches the GUI router. Unknown commands are exceedingly rare
        # (only the keys in mic_listener.COMMAND_PATTERNS can fire),
        # but we still surface a "rejected" line if one slips through
        # so the log never goes silent on a heard command.
        raw = str(meta.get("raw") or "").strip()
        if cmd == "clip":
            extra = int(meta.get("extra_seconds", getattr(self.settings, "extra_seconds_default", 0)) or 0)
            self.log(f"[Voice] GUI accepted command: clip (extra={extra}s, raw='{raw}')")
            self.after(0, lambda: self.extra_entry.delete(0, "end"))
            self.after(0, lambda: self.extra_entry.insert(0, str(extra)))
            self.after(0, self._export_clip)
            # Pass T-A: keep the conversational thread alive after a
            # successful follow-up command so the user can chain
            # "clip that" -> "what time is it" without re-saying simian.
            self._extend_voice_grace()
        elif cmd == "buffer_start":
            self.log(f"[Voice] GUI accepted command: buffer_start (raw='{raw}')")
            self.after(0, self._start_replay_buffer)
            self._extend_voice_grace()
        elif cmd == "buffer_stop":
            self.log(f"[Voice] GUI accepted command: buffer_stop (raw='{raw}')")
            self.after(0, self._stop_replay_buffer)
            self._extend_voice_grace()
        elif cmd == "screen_look":
            self.log(f"[Voice] GUI accepted command: screen_look (raw='{raw}')")
            self.after(0, self._voice_screen_look)
            self._extend_voice_grace()
        elif cmd == "screen_pause":
            self.log(f"[Voice] GUI accepted command: screen_pause (raw='{raw}')")
            self.after(0, self._voice_screen_pause)
            self._extend_voice_grace()
        elif cmd == "screen_resume":
            self.log(f"[Voice] GUI accepted command: screen_resume (raw='{raw}')")
            self.after(0, self._voice_screen_resume)
            self._extend_voice_grace()
        elif cmd == "wake_acknowledge":
            # Pass S-B: user said the wake word with nothing useful
            # following it. Reply with a short ready/listening line
            # rather than a model call so we don't burn an LLM round
            # trip on "hey simian". TTS is wired through _chat_reply
            # already, so this also speaks the response when sound is on.
            self.log(f"[Voice] GUI accepted command: wake_acknowledge (raw='{raw}')")
            # Pass T-A: open the post-ack follow-up window BEFORE we
            # speak the ready line, so that if the user starts talking
            # while TTS is still ramping the listener already has the
            # grace flag set by the time the next utterance is heard.
            self._open_voice_grace()
            self.after(0, lambda: self._chat_reply("I'm listening."))
        else:
            self.log(f"[Voice] GUI rejected (unknown command '{cmd}', raw='{raw}')")

    def _open_voice_grace(self) -> None:
        """Open the 5s post-wake-ack follow-up window on the listener.

        Thin wrapper that tolerates a missing or already-stopped listener
        without raising; voice commands keep working in test/headless
        mode where the listener may be stubbed.
        """
        listener = getattr(self, "mic_listener", None)
        if listener is None:
            return
        opener = getattr(listener, "open_wake_grace", None)
        if opener is None:
            return
        try:
            opener()
        except Exception as exc:
            self.log(f"[Voice] Wake grace open failed: {exc}")

    def _extend_voice_grace(self) -> None:
        """Extend the grace window after a follow-up command landed.

        No-ops if the listener has no grace currently open, so a non-voice
        command path (typing) doesn't accidentally start a grace window.
        """
        listener = getattr(self, "mic_listener", None)
        if listener is None:
            return
        extender = getattr(listener, "extend_wake_grace", None)
        if extender is None:
            return
        try:
            extender()
        except Exception as exc:
            self.log(f"[Voice] Wake grace extend failed: {exc}")
    # ----------------------- CLOSE -----------------------

    def _on_close(self) -> None:
        self._closing = True
        # Cancel any pending deferred replay-buffer autostart so we do
        # not spawn FFmpeg *after* the user has chosen to quit. after_cancel
        # on a fired/unknown id is harmless; wrap just in case Tk is
        # already tearing down.
        pending_after_id = self._replay_autostart_after_id
        self._replay_autostart_after_id = None
        if pending_after_id is not None:
            try:
                self.after_cancel(pending_after_id)
            except Exception:
                pass
        try:
            self._stop_mic_listener()
        except Exception:
            pass
        try:
            self._stop_replay_buffer()
        except Exception:
            pass
        try:
            self._stop_api()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    s = load_settings()
    Path(getattr(s, "clips_dir", "data/clips")).mkdir(parents=True, exist_ok=True)
    Path(getattr(s, "buffer_dir", "data/buffer")).mkdir(parents=True, exist_ok=True)

    app = SimianApp()
    app.mainloop()


if __name__ == "__main__":
    main()
