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

from services.settings_store import Settings, load_settings, save_settings
from services.replay_buffer import CaptureDevices, ReplayBufferRecorder
from services.file_scanner import FileScannerService
from services.news_service import NewsItem, fetch_news
from services.simian import SYSTEM_PERSONA, time_of_day_greeting

try:
    from services.audio_devices import list_dshow_audio_devices, list_sounddevice_devices
except Exception:
    list_dshow_audio_devices = None  # type: ignore[assignment]
    list_sounddevice_devices = None  # type: ignore[assignment]

try:
    from services.mic_listener import MicListenerConfig, MicListenerService
except Exception:
    MicListenerConfig = None  # type: ignore[assignment]
    MicListenerService = None  # type: ignore[assignment]

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


class UILogger:
    def __init__(self, root: ctk.CTk, textbox: ctk.CTkTextbox, max_lines: int = 800, poll_ms: int = 120, batch_size: int = 80):
        self.root = root
        self.textbox = textbox
        self.max_lines = max_lines
        self.poll_ms = max(50, int(poll_ms))
        self.batch_size = max(10, int(batch_size))
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._scheduled = False
        self._line_count = 0
        self._schedule_drain()

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._queue.put(f"[{ts}] {msg}\n")
        self._schedule_drain()

    def _schedule_drain(self) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        try:
            self.root.after(self.poll_ms, self._drain)
        except Exception:
            self._scheduled = False

    def _drain(self) -> None:
        self._scheduled = False
        if not self.textbox.winfo_exists():
            return

        items: list[str] = []
        for _ in range(self.batch_size):
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if items:
            joined = "".join(items)
            new_lines = joined.count("\n")
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
            self.textbox.see("end")
            self.textbox.configure(state="disabled")

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

        # Telemetry throttling
        self._telemetry_last_ts = 0.0
        self._telemetry_min_interval = float(os.getenv("SIMIAN_TELEMETRY_INTERVAL", "10.0"))
        self._all_news_items: List[Any] = []
        self._news_status_text = ""
        self._news_filter_after_id: Any = None
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

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

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
        self._ui_logger = UILogger(self, self.log_box, max_lines=700, poll_ms=140, batch_size=100)
        self.log = self._ui_logger.log
        self.log("GUI ready.")

        # Services / state
        self.api_proc: Optional[subprocess.Popen] = None
        self.replay = ReplayBufferRecorder(log_cb=self.log)
        self.mic_listener: Any = None

        self.srm_running = False
        self.srm_theta = 0.0
        self.srm_phi = 0.0
        self.srm_sigma = 0.0
        self._srm_points: List[List[float]] = []

        # Build tabs
        self._build_chat()
        self._build_clips()
        self._build_files()
        self._build_news()
        self._build_4d()
        self._build_services()
        self._build_settings()
        self._apply_accent_color()

        # Auto-start behaviors
        startup_delay = max(1200, int(getattr(self.settings, "safe_startup_delay_ms", 2500) or 2500))
        self.after(startup_delay, self._auto_start)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
    def _chat_reply(self, text: str) -> None:
        self._remember_chat("assistant", text)
        self._chat_append("Simian", text)
        self._speak(text)

    def _tts_preview_text(self, text: str) -> str:
        cleaned = " ".join((text or "").strip().split())
        if len(cleaned) > 360:
            cleaned = cleaned[:357].rsplit(" ", 1)[0] + "..."
        return cleaned

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

    def _get_selected_output_device(self) -> Optional[int]:
        raw = getattr(self.settings, "tts_output_device", None)
        if raw in (None, "", "default"):
            return None
        try:
            return int(str(raw).split("|", 1)[0].strip())
        except Exception:
            return None

    def _get_selected_input_device(self) -> Optional[int]:
        raw = getattr(self.settings, "stt_input_device", None)
        if raw in (None, "", "default"):
            return None
        try:
            return int(str(raw).split("|", 1)[0].strip())
        except Exception:
            return None

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

    def _play_audio_file(self, path: str) -> None:
        if not path:
            return
        ext = Path(path).suffix.lower()
        output_device = self._get_selected_output_device()

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
        if os.name == "nt":
            try:
                ps_path = path.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName System; "
                    f"$p = New-Object System.Media.SoundPlayer '{ps_path}'; "
                    "$p.PlaySync()"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception as e:
                self.log(f"[TTS] PowerShell playback failed: {e}")
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as e:
            self.log(f"[TTS] open audio file failed: {e}")

    def _speak(self, text: str) -> None:

        if not getattr(self.settings, "voice_enabled", False):
            return
        text = self._tts_preview_text(text)
        if not text:
            return

        voice_id = getattr(self.settings, "voice_id", None) or "en-US-GuyNeural"

        def worker() -> None:
            with self._tts_lock:
                if callable(_service_tts_to_file):
                    try:
                        path = str(_service_tts_to_file(text, voice=voice_id))
                        self.log(f"[TTS] Audio ready: {path}")
                        self._play_audio_file(path)
                        return
                    except Exception as e:
                        self.log(f"[TTS] service synthesis failed: {e}")

                speak_fn = _edge_speak_text
                if callable(speak_fn):
                    try:
                        speak_fn(text, voice=voice_id)
                        return
                    except TypeError:
                        try:
                            speak_fn(text)
                            return
                        except Exception as e:
                            self.log(f"[TTS] voice backend failed: {e}")
                    except Exception as e:
                        self.log(f"[TTS] voice backend failed: {e}")

                if os.name == "nt":
                    try:
                        safe_text = text.replace("'", "''")
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
                    engine.say(text)
                    engine.runAndWait()
                except Exception as e:
                    self.log(f"[TTS] No working TTS backend: {e}")

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
        if HEALTH_QUERY_RE.search((text or "").strip()):
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
            self.after(4200, self._request_start_replay_buffer)
        else:
            self.log("[Startup] Replay buffer auto-start is disabled.")

        self.after(5200, lambda: self._schedule_news_refresh(initial=True))
        self._startup_inflight = False
    # ----------------------- CHAT -----------------------


    def _build_chat(self) -> None:
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

    def _toggle_chat_mic(self) -> None:
        listener = self.mic_listener
        running = listener is not None and getattr(listener, "is_running", lambda: False)()
        if running and self._chat_mic_hot_mode:
            self._chat_mic_hot_mode = False
            self._stop_mic_listener()
        else:
            self._chat_mic_hot_mode = True
            self._start_mic_listener(hot_mode=True)
        self._sync_chat_mic_controls()

    def _sync_chat_mic_controls(self) -> None:
        running = False
        listener = getattr(self, "mic_listener", None)
        if listener is not None:
            running = bool(getattr(listener, "is_running", lambda: False)())
        if hasattr(self, "chat_mic_btn"):
            self.chat_mic_btn.configure(text=("Mic On" if running and self._chat_mic_hot_mode else "Mic Off"))
        if hasattr(self, "chat_voice_hint"):
            if running and self._chat_mic_hot_mode:
                self.chat_voice_hint.configure(text="Voice: hot mic is on. Speak naturally, or say clip that.")
            elif running:
                self.chat_voice_hint.configure(text="Voice: background listener is on. Say 'Simian ...' to talk.")
            else:
                self.chat_voice_hint.configure(text="Voice: mic is off. Click the mic button to listen.")

    def _on_voice_transcript(self, text: str, meta: Dict[str, Any]) -> None:
        spoken = (text or "").strip()
        if not spoken:
            return
        low = spoken.lower()
        if low in {"huh", "uh", "um", "hmm", "mm", "hm"}:
            return
        last = getattr(self, "_last_voice_text", "")
        last_ts = float(getattr(self, "_last_voice_ts", 0.0) or 0.0)
        now = time.time()
        if low == str(last).lower() and (now - last_ts) < 2.0:
            return
        self._last_voice_text = spoken
        self._last_voice_ts = now

        def apply_transcript() -> None:
            if self._chat_inflight:
                self.log(f"[Voice] Busy, ignored transcript: {spoken}")
                return
            self.chat_entry.delete(0, "end")
            self.chat_entry.insert(0, spoken)
            self._send_chat()

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

    def _send_chat(self) -> None:
        text = self.chat_entry.get().strip()
        if not text or self._chat_inflight:
            return
        self.chat_entry.delete(0, "end")
        self._chat_append("You", text)
        self._remember_chat("user", text)

        if IDENTITY_QUERY_RE.search(text):
            self.after(0, lambda: self._chat_reply("I'm Simian, your local assistant for Project C.H.I.M.P."))
            return

        local = self._handle_local_query(text)
        if local:
            self.after(0, lambda local=local: self._chat_reply(local))
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
                prompt = (
                    f"{SIMIAN_SYSTEM_PROMPT}\n"
                    f"Current greeting context: {time_of_day_greeting()}\n"
                    "Be honest about what this desktop app can currently do.\n"
                    "Do not invent integrations, secret control paths, or fake live data.\n"
                    "If the user asks for live system data, provide local data only when actually available.\n"
                    f"{telemetry_block}\n\n"
                    "Recent conversation:\n"
                    f"{history_block}\n"
                    "Simian:"
                )

                timeout_s = float(getattr(self.settings, "chat_request_timeout", 180) or 180)
                r = httpx.post(
                    "http://127.0.0.1:11434/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False},
                    timeout=timeout_s,
                )
                r.raise_for_status()
                out = (r.json().get("response") or "").strip() or "(No response)"
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
        self.replay.start()
        self.lbl_replay.configure(text="Replay buffer: running")
        self.log("[Replay] Buffer running.")
        self._speak("Replay buffer running.")

    def _stop_replay_buffer(self) -> None:
        self.replay.stop()
        self.lbl_replay.configure(text="Replay buffer: stopped")
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
        if initial:
            self.after(1000, self._refresh_news)
        self.after(max(30, int(getattr(self.settings, "news_refresh_seconds", 300))) * 1000, self._schedule_news_refresh)

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
            self.news_box.tag_config(tag, foreground="#6aaeff", underline=True)
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

        self.canvas = tk.Canvas(outer, bg="#111111", highlightthickness=0)
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

        if self.chk_push.get() == 1:
            self._push_telemetry(
                {
                    "theta": self.srm_theta,
                    "phi": self.srm_phi,
                    "sigma": self.srm_sigma,
                    "points": pts[:8],
                    "ts": time.time(),
                }
            )

        self.after(33, self._srm_tick)

    def _draw_points(self, pts: List[tuple[float, float]]) -> None:
        self.canvas.delete("all")
        for (x, y) in pts:
            r = 3
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill="#4da3ff", outline="")
        self.canvas.create_text(
            10,
            10,
            anchor="nw",
            fill="#d0d0d0",
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

        threading.Thread(target=worker, daemon=True).start()

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
        ctk.CTkButton(api, text="Start API", command=self._start_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Stop API", command=self._stop_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Open Swagger", command=self._open_swagger).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Open OpenAPI JSON", command=lambda: webbrowser.open(f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/openapi.json")).pack(side="left", padx=6)

        mic = ctk.CTkFrame(outer)
        mic.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.lbl_mic = ctk.CTkLabel(mic, text="Mic listener: stopped")
        self.lbl_mic.pack(side="left", padx=8)
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
                            threading.Thread(target=self._play_audio_file, args=(out_path,), daemon=True).start()
            except Exception as e:
                self.after(0, lambda e=e: self._set_services_output(f"Error calling {path}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_status(self) -> None:
        self.lbl_api.configure(text=f"API: {'running' if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT) else 'stopped'}")
        listener = getattr(self, "mic_listener", None)
        if listener is not None and getattr(listener, "is_running", lambda: False)():
            self.lbl_mic.configure(text="Mic listener: running")
        elif MicListenerService is None or self._resolve_vosk_model_dir() is None:
            self.lbl_mic.configure(text="Mic listener: unavailable")
        else:
            self.lbl_mic.configure(text="Mic listener: stopped")
        self._sync_chat_mic_controls()
        self.after(1800, self._poll_status)
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
        ctk.CTkLabel(theme, text="UI Theme").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.accent_entry = ctk.CTkEntry(theme)
        self.accent_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        self.accent_entry.insert(0, getattr(self.settings, "accent_hex", "#4da3ff"))
        ctk.CTkButton(theme, text="Pick color", command=self._pick_color).grid(row=0, column=2, padx=8, pady=8)

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

        ctk.CTkButton(scroll, text="Open audio device picker", command=self._open_audio_device_picker).grid(row=4, column=0, sticky="w", padx=8, pady=12)

    def _darken_hex(self, color: str, factor: float = 0.85) -> str:
        color = (color or "#4da3ff").lstrip("#")
        if len(color) != 6:
            return "#3b82f6"
        r = max(0, min(255, int(int(color[0:2], 16) * factor)))
        g = max(0, min(255, int(int(color[2:4], 16) * factor)))
        b = max(0, min(255, int(int(color[4:6], 16) * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _apply_accent_color(self) -> None:
        accent = getattr(self.settings, "accent_hex", "") or "#4da3ff"
        hover = self._darken_hex(accent, 0.82)
        try:
            self.tabs._segmented_button.configure(selected_color=accent, selected_hover_color=hover)
        except Exception:
            pass

        def apply_widget(widget: Any) -> None:
            try:
                if isinstance(widget, ctk.CTkButton):
                    widget.configure(fg_color=accent, hover_color=hover)
                elif isinstance(widget, ctk.CTkOptionMenu):
                    widget.configure(fg_color=accent, button_color=accent, button_hover_color=hover)
                elif isinstance(widget, ctk.CTkCheckBox):
                    widget.configure(fg_color=accent, hover_color=hover, border_color=accent)
            except Exception:
                pass
            try:
                for child in widget.winfo_children():
                    apply_widget(child)
            except Exception:
                pass

        apply_widget(self)
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

    def _open_audio_device_picker(self) -> None:
        if list_sounddevice_devices is None or list_dshow_audio_devices is None:
            self.log("[Audio] Device picker unavailable (services.audio_devices import failed).")
            return
        try:
            sd_info = list_sounddevice_devices()
            dshow_names = list_dshow_audio_devices()
        except Exception as e:
            self.log(f"[Audio] Could not query devices: {e}")
            return

        win = ctk.CTkToplevel(self)
        win.title("Simian audio device picker")
        win.geometry("860x520")
        win.transient(self)
        win.grab_set()
        win.grid_columnconfigure(1, weight=1)
        win.grid_rowconfigure(4, weight=1)

        inputs = ["default"] + [f"{d['index']} | {d['name']}" for d in sd_info.get('inputs', [])]
        outputs = ["default"] + [f"{d['index']} | {d['name']}" for d in sd_info.get('outputs', [])]
        dshow_values = [""] + dshow_names

        stt_current = str(getattr(self.settings, "stt_input_device", "default") or "default")
        tts_current = str(getattr(self.settings, "tts_output_device", "default") or "default")
        replay_sys_current = str(getattr(self.settings, "replay_system_audio_device", "") or "")
        replay_mic_current = str(getattr(self.settings, "replay_mic_device", "") or "")

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

        ctk.CTkLabel(win, text="Replay system audio (FFmpeg / dshow)").grid(row=2, column=0, sticky="w", padx=12, pady=10)
        sys_opt = ctk.CTkOptionMenu(win, values=dshow_values or [""])
        sys_opt.grid(row=2, column=1, sticky="ew", padx=12, pady=10)
        sys_opt.set(normalize_choice(dshow_values or [""], replay_sys_current))

        ctk.CTkLabel(win, text="Replay microphone (FFmpeg / dshow)").grid(row=3, column=0, sticky="w", padx=12, pady=10)
        mic_opt = ctk.CTkOptionMenu(win, values=dshow_values or [""])
        mic_opt.grid(row=3, column=1, sticky="ew", padx=12, pady=10)
        mic_opt.set(normalize_choice(dshow_values or [""], replay_mic_current))

        textbox = ctk.CTkTextbox(win, wrap="word")
        textbox.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=12, pady=12)
        textbox.insert("end", "Available sounddevice inputs:\n")
        for d in sd_info.get("inputs", []):
            textbox.insert("end", f"- {d['index']} | {d['name']}\n")
        textbox.insert("end", "\nAvailable sounddevice outputs:\n")
        for d in sd_info.get("outputs", []):
            textbox.insert("end", f"- {d['index']} | {d['name']}\n")
        textbox.insert("end", "\nAvailable FFmpeg DirectShow audio names:\n")
        for name in dshow_names:
            textbox.insert("end", f"- {name}\n")

        def apply_and_close() -> None:
            self.settings.stt_input_device = stt_opt.get()
            self.settings.tts_output_device = tts_opt.get()
            self.settings.replay_system_audio_device = sys_opt.get()
            self.settings.replay_mic_device = mic_opt.get()
            save_settings(self.settings)
            self.log("[Audio] Device preferences saved.")
            win.destroy()

        btn = ctk.CTkFrame(win)
        btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        ctk.CTkButton(btn, text="Save device choices", command=apply_and_close).pack(side="left", padx=8)
        ctk.CTkButton(btn, text="Close", command=win.destroy).pack(side="left", padx=8)

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

        self.settings.voice_enabled = self.chk_voice.get() == 1
        self.settings.stt_enabled = self.chk_stt.get() == 1
        self.settings.voice_id = self.voice_id_entry.get().strip() or getattr(self.settings, "voice_id", "")
        self.settings.vosk_model_dir = self.vosk_model_entry.get().strip()
        self.settings.accent_hex = self.accent_entry.get().strip() or getattr(self.settings, "accent_hex", "#4da3ff")
        self.settings.clips_dir = self.entry_clips_dir.get().strip() or getattr(self.settings, "clips_dir", "data/clips")
        self.settings.news_default_category = self.news_category.get()

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

        replay_changed = any(old[k] != getattr(self.settings, k) for k in old)
        if replay_changed and self.replay.is_running():
            try:
                self._stop_replay_buffer()
                self._start_replay_buffer()
            except Exception as e:
                self.log(f"[Replay] Restart after settings change failed: {e}")

        if restart_listener:
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

        if hasattr(listener, "set_hot_mode"):
            listener.set_hot_mode(bool(hot_mode))
        self._chat_mic_hot_mode = bool(hot_mode)

        if not listener.available():
            reason = listener.unavailable_reason()
            self.lbl_mic.configure(text="Mic listener: unavailable")
            if model_dir is None:
                self.log("[Voice] Mic listener unavailable: set VOSK_MODEL_DIR or choose a model in Settings.")
            self.log(f"[Voice] {reason}")
            self._sync_chat_mic_controls()
            return

        if not getattr(listener, "is_running", lambda: False)():
            listener.start()
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

    def _on_voice_command(self, cmd: str, meta: Dict[str, Any]) -> None:
        if cmd == "clip":
            extra = int(meta.get("extra_seconds", getattr(self.settings, "extra_seconds_default", 0)) or 0)
            self.after(0, lambda: self.extra_entry.delete(0, "end"))
            self.after(0, lambda: self.extra_entry.insert(0, str(extra)))
            self.after(0, self._export_clip)
        elif cmd == "buffer_start":
            self.after(0, self._start_replay_buffer)
        elif cmd == "buffer_stop":
            self.after(0, self._stop_replay_buffer)
    # ----------------------- CLOSE -----------------------

    def _on_close(self) -> None:
        self._closing = True
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
