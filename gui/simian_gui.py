"""
Simian GUI (rebuilt): unified Settings, Replay Buffer, File Summarizer, News, 4D Lab.

Key behaviors requested:
- Voice enabled by default (toggle in Settings)
- Voice settings live in Settings tab
- Theme accent color picker in Settings
- Replay buffer (5 min default) records in background; exports only on "Clip that"
- Clip settings adjustable in Settings (buffer minutes, segment seconds, fps/resolution, upscale)
- Background mic listener for commands like "clip that" (Vosk + sounddevice), if installed
- World + Tech news feed (trusted sources, refresh every 5 minutes)
- File summarization for any file type (best-effort; richer if Ollama is running)

Run:
  python -m gui.simian_gui

Optional:
  set OLLAMA_TEXT_MODEL / OLLAMA_VISION_MODEL for better summaries
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Dict, Any, List

import customtkinter as ctk

from services.settings_store import load_settings, save_settings, Settings
from services.replay_buffer import ReplayBufferRecorder, CaptureDevices
from services.file_scanner import FileScannerService
from services.news_service import fetch_news
from services.mic_listener import MicListenerService

try:
    from voice.voice import speak_text as _edge_speak_text  # type: ignore
except Exception:
    _edge_speak_text = None


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000


class UILogger:
    def __init__(self, textbox: ctk.CTkTextbox):
        self.textbox = textbox
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        with self._lock:
            self.textbox.configure(state="normal")
            self.textbox.insert("end", line)
            self.textbox.see("end")
            self.textbox.configure(state="disabled")


def port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:
        return False


class SimianApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Simian — Project C.H.I.M.P")
        self.geometry("1100x760")
        ctk.set_appearance_mode("dark")

        self.settings: Settings = load_settings()

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self.tab_chat = self.tabs.add("Chat")
        self.tab_clips = self.tabs.add("Clips")
        self.tab_files = self.tabs.add("Files")
        self.tab_news = self.tabs.add("News")
        self.tab_4d = self.tabs.add("4D Lab")
        self.tab_services = self.tabs.add("Services")
        self.tab_settings = self.tabs.add("Settings")
        self.tab_logs = self.tabs.add("Logs")

        # Logs (global)
        self.log_box = ctk.CTkTextbox(self.tab_logs, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box.configure(state="disabled")
        self.log = UILogger(self.log_box).log
        self.log("GUI ready.")

        # Services
        self.api_proc: Optional[subprocess.Popen] = None
        self.replay = ReplayBufferRecorder(log_cb=self.log)
        self.mic_listener: Optional[MicListenerService] = None

        # Build tabs
        self._build_chat()
        self._build_clips()
        self._build_files()
        self._build_news()
        self._build_4d()
        self._build_services()
        self._build_settings()

        # Auto-start behaviors
        self.after(500, self._auto_start)

        # Close handler
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _speak(self, text: str) -> None:
        """Speak via Edge TTS if enabled."""
        if not getattr(self.settings, 'voice_enabled', False):
            return
        if _edge_speak_text is None:
            return
        voice_id = getattr(self.settings, 'voice_id', None)

        def worker():
            try:
                _edge_speak_text(text, voice=voice_id)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()


    def _speak(self, text: str) -> None:
        """Speak via Edge TTS if enabled."""
        if not getattr(self.settings, "voice_enabled", False):
            return
        if _edge_speak_text is None:
            return
        voice_id = getattr(self.settings, "voice_id", None)

        def worker():
            try:
                _edge_speak_text(text, voice=voice_id)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()


    # ----------------------- AUTO START -----------------------

    def _auto_start(self):
        # Voice default on unless user disables
        if self.settings.voice_enabled:
            self._start_mic_listener()

        # Replay buffer default on (requested behavior)
        try:
            self._start_replay_buffer()
        except Exception as e:
            self.log(f"[Replay] Failed to auto-start: {e}")

        # News auto refresh
        self._schedule_news_refresh(initial=True)

    # ----------------------- CHAT -----------------------

    def _build_chat(self):
        frame = ctk.CTkFrame(self.tab_chat)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.chat_log = ctk.CTkTextbox(frame, wrap="word")
        self.chat_log.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        self.chat_log.configure(state="disabled")

        self.chat_entry = ctk.CTkEntry(frame, placeholder_text="Ask Simian...")
        self.chat_entry.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.chat_entry.bind("<Return>", lambda _e: self._send_chat())

        btn = ctk.CTkButton(frame, text="Send", command=self._send_chat)
        btn.grid(row=1, column=1, padx=8, pady=8)

        btn2 = ctk.CTkButton(frame, text="Clear", command=self._clear_chat)
        btn2.grid(row=1, column=2, padx=8, pady=8)

        self._chat_append("Simian", "Online. (Chat uses Ollama if available.)")

    def _chat_append(self, who: str, msg: str):
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", f"{who}: {msg}\n\n")
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def _clear_chat(self):
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")

    def _send_chat(self):
        text = self.chat_entry.get().strip()
        if not text:
            return
        self.chat_entry.delete(0, "end")
        self._chat_append("You", text)

        # Minimal local chat via Ollama HTTP (no API dependency)
        def worker():
            try:
                import httpx
                model = os.environ.get("OLLAMA_TEXT_MODEL", "llama3.1:8b")

                # If SRM is running, include current telemetry as context so the LLM can "see" it.
                telemetry = ""
                try:
                    if getattr(self, "srm_running", False):
                        telemetry = f"\n\n[SRM telemetry] theta={self.srm_theta:.3f}, phi={self.srm_phi:.3f}, sigma={self.srm_sigma:.3f}"
                except Exception:
                    telemetry = ""

                prompt = text + telemetry

                r = httpx.post("http://127.0.0.1:11434/api/generate",
                               json={"model": model, "prompt": prompt, "stream": False},
                               timeout=120)
                r.raise_for_status()
                out = (r.json().get("response") or "").strip()
                if not out:
                    out = "(No response)"
                self.after(0, lambda: self._chat_append("Simian", out))
            except Exception as e:
                self.after(0, lambda: self._chat_append("Simian", f"(Ollama offline) {e}"))

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------- CLIPS -----------------------

    def _build_clips(self):
        outer = ctk.CTkFrame(self.tab_clips)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # Controls
        ctrl = ctk.CTkFrame(outer)
        ctrl.pack(fill="x", padx=8, pady=8)

        self.lbl_replay = ctk.CTkLabel(ctrl, text="Replay buffer: stopped")
        self.lbl_replay.pack(side="left", padx=8)

        ctk.CTkButton(ctrl, text="Start Buffer", command=self._start_replay_buffer).pack(side="left", padx=6)
        ctk.CTkButton(ctrl, text="Stop Buffer", command=self._stop_replay_buffer).pack(side="left", padx=6)

        ctk.CTkButton(ctrl, text="Clip that", command=self._export_clip).pack(side="left", padx=6)

        self.extra_entry = ctk.CTkEntry(ctrl, width=120, placeholder_text="Extra sec")
        self.extra_entry.pack(side="left", padx=6)
        self.extra_entry.insert(0, str(self.settings.extra_seconds_default))

        # Clip list
        list_frame = ctk.CTkFrame(outer)
        list_frame.pack(fill="both", expand=True, padx=8, pady=8)
        list_frame.grid_rowconfigure(1, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(list_frame, text="Saved clips").grid(row=0, column=0, sticky="w", padx=8, pady=6)

        self.clips_box = ctk.CTkTextbox(list_frame, wrap="none")
        self.clips_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        self.clips_box.configure(state="disabled")

        ctk.CTkButton(list_frame, text="Refresh", command=self._refresh_clips).grid(row=2, column=0, sticky="e", padx=8, pady=8)

        self._refresh_clips()

    def _start_replay_buffer(self):
        # Set devices via env / settings for now
        sys_audio = os.environ.get("SIMIAN_SYSTEM_AUDIO") or ""
        mic = os.environ.get("SIMIAN_MIC") or ""
        self.replay.devices = CaptureDevices(system_audio=sys_audio.strip() or None, mic=mic.strip() or None)
        self.replay.start()
        self.lbl_replay.configure(text="Replay buffer: running")
        self.log("[Replay] Buffer running.")
        self._speak("Replay buffer running.")

    def _stop_replay_buffer(self):
        self.replay.stop()
        self.lbl_replay.configure(text="Replay buffer: stopped")
        self.log("[Replay] Buffer stopped.")

    def _export_clip(self):
        try:
            extra = int(self.extra_entry.get().strip() or "0")
        except Exception:
            extra = 0
        try:
            p = self.replay.export_last(minutes=self.settings.replay_minutes, extra_seconds=extra, upscale=self.settings.export_upscale)
            self.log(f"[Replay] Exported clip: {p}")
            self._speak("Clip saved.")
            self._refresh_clips()
        except Exception as e:
            self.log(f"[Replay] Export failed: {e}")
            self._speak("Clip export failed.")

    def _refresh_clips(self):
        clips_dir = Path(self.settings.clips_dir)
        clips_dir.mkdir(parents=True, exist_ok=True)
        clips = sorted(clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)

        self.clips_box.configure(state="normal")
        self.clips_box.delete("1.0", "end")
        for p in clips[:200]:
            self.clips_box.insert("end", f"{p.name}\t{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))}\n")
        self.clips_box.configure(state="disabled")

    # ----------------------- FILES -----------------------

    def _build_files(self):
        outer = ctk.CTkFrame(self.tab_files)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        row = ctk.CTkFrame(outer)
        row.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.file_path_entry = ctk.CTkEntry(row, placeholder_text="Pick a file or paste a path...")
        self.file_path_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)

        ctk.CTkButton(row, text="Browse", command=self._browse_file).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Summarize", command=self._summarize_file).pack(side="left", padx=6)

        row2 = ctk.CTkFrame(outer)
        row2.grid(row=1, column=0, sticky="ew", padx=8, pady=4)

        self.dir_entry = ctk.CTkEntry(row2, placeholder_text="Directory path for batch scan (optional)")
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ctk.CTkButton(row2, text="Summarize Dir", command=self._summarize_dir).pack(side="left", padx=6)

        self.file_out = ctk.CTkTextbox(outer, wrap="word")
        self.file_out.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)

    def _browse_file(self):
        import tkinter.filedialog as fd
        fp = fd.askopenfilename()
        if fp:
            self.file_path_entry.delete(0, "end")
            self.file_path_entry.insert(0, fp)

    def _summarize_file(self):
        fp = self.file_path_entry.get().strip()
        if not fp:
            return
        svc = FileScannerService(log_cb=self.log)
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", "Scanning...\n")
        def worker():
            try:
                res = svc.summarize_path(fp)
                txt = f"Path: {res.path}\nMime: {res.mime}\nSize: {res.size_bytes}\nSHA256: {res.sha256}\n\nSummary:\n{res.summary}\n\nDetails:\n{res.details}\n"
                self.after(0, lambda: self._set_file_out(txt))
            except Exception as e:
                self.after(0, lambda: self._set_file_out(f"Error: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _summarize_dir(self):
        dp = self.dir_entry.get().strip()
        if not dp:
            return
        svc = FileScannerService(log_cb=self.log)
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", "Batch scanning...\n")
        def worker():
            try:
                payload = svc.summarize_directory(dp, recursive=True, max_files=200)
                # concise output
                lines = [f"Scanned {payload['count']} files\n"]
                for item in payload["results"]:
                    if "error" in item:
                        lines.append(f"- {item['path']}  ERROR: {item['error']}")
                    else:
                        lines.append(f"- {Path(item['path']).name}: {item['mime']}  (sha={item['sha256'][:10]}...)")
                self.after(0, lambda: self._set_file_out("\n".join(lines)))
            except Exception as e:
                self.after(0, lambda: self._set_file_out(f"Error: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _set_file_out(self, text: str):
        self.file_out.delete("1.0", "end")
        self.file_out.insert("end", text)

    # ----------------------- NEWS -----------------------

    def _build_news(self):
        outer = ctk.CTkFrame(self.tab_news)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        bar = ctk.CTkFrame(outer)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.news_category = ctk.CTkOptionMenu(bar, values=["tech","world","security","science","business"])
        self.news_category.set(self.settings.news_default_category)
        self.news_category.pack(side="left", padx=8)

        ctk.CTkButton(bar, text="Refresh now", command=self._refresh_news).pack(side="left", padx=6)

        self.news_status = ctk.CTkLabel(bar, text="")
        self.news_status.pack(side="left", padx=12)

        self.news_box = ctk.CTkTextbox(outer, wrap="word")
        self.news_box.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)

        ctk.CTkLabel(outer, text="Tip: double-click a link to open in browser (copy/paste).").grid(row=3, column=0, sticky="w", padx=8, pady=4)

    def _schedule_news_refresh(self, initial: bool = False):
        if initial:
            self.after(1000, self._refresh_news)
        # reschedule
        self.after(max(30, int(self.settings.news_refresh_seconds)) * 1000, self._schedule_news_refresh)

    def _refresh_news(self):
        cat = self.news_category.get()
        self.news_status.configure(text="Loading...")
        def worker():
            try:
                items = fetch_news(cat, limit=60)
                txt_lines = []
                for it in items:
                    when = f" ({it.published})" if it.published else ""
                    txt_lines.append(f"[{it.source}]{when}\n{it.title}\n{it.url}\n")
                txt = "\n".join(txt_lines) if txt_lines else "No items returned (feed blocked or offline)."
                self.after(0, lambda: self._set_news(txt, f"{len(items)} items"))
            except Exception as e:
                self.after(0, lambda: self._set_news(f"Error: {e}", "error"))
        threading.Thread(target=worker, daemon=True).start()

    def _set_news(self, text: str, status: str):
        self.news_box.delete("1.0", "end")
        self.news_box.insert("end", text)
        self.news_status.configure(text=status)

    # ----------------------- 4D LAB -----------------------

    def _build_4d(self):
        outer = ctk.CTkFrame(self.tab_4d)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(outer)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        self.srm_running = False
        self.srm_theta = 0.0
        self.srm_phi = 0.0
        self.srm_sigma = 0.0

        self.lbl_srm = ctk.CTkLabel(bar, text="SRM: stopped")
        self.lbl_srm.pack(side="left", padx=8)

        ctk.CTkButton(bar, text="Start", command=self._srm_start).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="Stop", command=self._srm_stop).pack(side="left", padx=6)

        self.chk_push = ctk.CTkCheckBox(bar, text="Push telemetry to API (/api/telemetry)", onvalue=1, offvalue=0)
        self.chk_push.select()
        self.chk_push.pack(side="left", padx=12)

        import tkinter as tk
        self.canvas = tk.Canvas(outer, bg="#111111", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        self._srm_points: List[List[float]] = []  # x,y projection points

    def _srm_start(self):
        if self.srm_running:
            return
        self.srm_running = True
        self.lbl_srm.configure(text="SRM: running")
        self.log("[4D] SRM visualizer started.")
        self._srm_tick()

    def _srm_stop(self):
        self.srm_running = False
        self.lbl_srm.configure(text="SRM: stopped")
        self.log("[4D] SRM visualizer stopped.")

    def _srm_tick(self):
        if not self.srm_running:
            return

        # evolve params
        self.srm_theta = (self.srm_theta + 0.03) % 6.283
        self.srm_phi = (self.srm_phi + 0.021) % 6.283
        self.srm_sigma = (self.srm_sigma + 0.017) % 6.283

        # Create a small cloud in a rotating projection (fake 4D -> 2D)
        import math
        cx, cy = 520, 260
        scale = 160

        # generate 24 points on a torus-like loop
        pts = []
        for i in range(24):
            a = (i / 24.0) * 6.283
            # 4D-ish coords (x,y,z,w)
            x = math.cos(a + self.srm_theta)
            y = math.sin(a + self.srm_phi)
            z = math.cos(2*a + self.srm_sigma)
            w = math.sin(2*a + self.srm_theta)

            # project to 2D (mix z,w into depth)
            px = cx + (x + 0.35*z) * scale
            py = cy + (y + 0.35*w) * scale
            pts.append((px, py))

        self._draw_points(pts)

        if self.chk_push.get() == 1:
            self._push_telemetry({
                "theta": self.srm_theta,
                "phi": self.srm_phi,
                "sigma": self.srm_sigma,
                "points": pts[:8],  # keep small
                "ts": time.time(),
            })

        self.after(33, self._srm_tick)  # ~30fps

    def _draw_points(self, pts):
        self.canvas.delete("all")
        for (x, y) in pts:
            r = 3
            self.canvas.create_oval(x-r, y-r, x+r, y+r, fill="#4da3ff", outline="")
        # draw info
        self.canvas.create_text(10, 10, anchor="nw",
                                fill="#d0d0d0",
                                text=f"θ={self.srm_theta:.3f}  φ={self.srm_phi:.3f}  σ={self.srm_sigma:.3f}")

    def _push_telemetry(self, payload: Dict[str, Any]):
        # fire and forget
        def worker():
            try:
                import httpx
                httpx.post(f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/api/telemetry",
                           json={"source":"gui","kind":"srm","payload":payload},
                           timeout=1.2)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    # ----------------------- SERVICES -----------------------

    def _build_services(self):
        outer = ctk.CTkFrame(self.tab_services)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # API control
        api = ctk.CTkFrame(outer)
        api.pack(fill="x", padx=8, pady=8)

        self.lbl_api = ctk.CTkLabel(api, text="API: unknown")
        self.lbl_api.pack(side="left", padx=8)

        ctk.CTkButton(api, text="Start API", command=self._start_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Stop API", command=self._stop_api).pack(side="left", padx=6)
        ctk.CTkButton(api, text="Open Swagger", command=self._open_swagger).pack(side="left", padx=6)

        # Mic listener
        mic = ctk.CTkFrame(outer)
        mic.pack(fill="x", padx=8, pady=8)

        self.lbl_mic = ctk.CTkLabel(mic, text="Mic listener: stopped")
        self.lbl_mic.pack(side="left", padx=8)

        ctk.CTkButton(mic, text="Start listener", command=self._start_mic_listener).pack(side="left", padx=6)
        ctk.CTkButton(mic, text="Stop listener", command=self._stop_mic_listener).pack(side="left", padx=6)

        # Status poller
        self.after(800, self._poll_status)

    def _poll_status(self):
        self.lbl_api.configure(text=f"API: {'running' if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT) else 'stopped'}")
        self.after(1200, self._poll_status)

    def _start_api(self):
        if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT):
            self.log("[API] Already running.")
            return

        py = sys_exe()
        cmd = [py, "-m", "uvicorn", "main:app", "--host", DEFAULT_API_HOST, "--port", str(DEFAULT_API_PORT)]
        self.log(f"[API] Starting: {' '.join(cmd)}")
        self.api_proc = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[1]))
        time.sleep(0.5)
        if port_in_use(DEFAULT_API_HOST, DEFAULT_API_PORT):
            self.log("[API] Started.")
        else:
            self.log("[API] Failed to bind (check logs / port conflicts).")

    def _stop_api(self):
        if self.api_proc and self.api_proc.poll() is None:
            self.log("[API] Stopping...")
            try:
                self.api_proc.terminate()
            except Exception:
                pass
        self.api_proc = None

    def _open_swagger(self):
        url = f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}/docs"
        webbrowser.open(url)

    # ----------------------- SETTINGS -----------------------

    def _build_settings(self):
        outer = ctk.CTkFrame(self.tab_settings)
        outer.pack(fill="both", expand=True, padx=10, pady=10)
        outer.grid_columnconfigure(0, weight=1)

        # Voice settings
        voice = ctk.CTkFrame(outer)
        voice.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        voice.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(voice, text="Voice / TTS").grid(row=0, column=0, sticky="w", padx=8, pady=8)

        self.chk_voice = ctk.CTkCheckBox(voice, text="Voice enabled (default ON)", onvalue=1, offvalue=0)
        if self.settings.voice_enabled:
            self.chk_voice.select()
        else:
            self.chk_voice.deselect()
        self.chk_voice.grid(row=1, column=0, sticky="w", padx=8, pady=6)

        ctk.CTkLabel(voice, text="Voice ID (Edge TTS)").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.voice_id_entry = ctk.CTkEntry(voice)
        self.voice_id_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        self.voice_id_entry.insert(0, self.settings.voice_id)

        # Theme settings
        theme = ctk.CTkFrame(outer)
        theme.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        theme.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(theme, text="UI Theme").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.accent_entry = ctk.CTkEntry(theme)
        self.accent_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        self.accent_entry.insert(0, self.settings.accent_hex)

        ctk.CTkButton(theme, text="Pick color", command=self._pick_color).grid(row=0, column=2, padx=8, pady=8)

        # Clip settings
        clips = ctk.CTkFrame(outer)
        clips.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        clips.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(clips, text="Replay buffer / Clips").grid(row=0, column=0, sticky="w", padx=8, pady=8)

        self.entry_clips_dir = ctk.CTkEntry(clips)
        self.entry_clips_dir.grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        self.entry_clips_dir.insert(0, self.settings.clips_dir)
        ctk.CTkLabel(clips, text="Clips dir").grid(row=1, column=0, sticky="w", padx=8, pady=6)

        self.entry_buffer_min = ctk.CTkEntry(clips, width=120)
        self.entry_buffer_min.grid(row=2, column=1, sticky="w", padx=8, pady=6)
        self.entry_buffer_min.insert(0, str(self.settings.replay_minutes))
        ctk.CTkLabel(clips, text="Replay minutes (default 5)").grid(row=2, column=0, sticky="w", padx=8, pady=6)

        self.entry_seg_sec = ctk.CTkEntry(clips, width=120)
        self.entry_seg_sec.grid(row=3, column=1, sticky="w", padx=8, pady=6)
        self.entry_seg_sec.insert(0, str(self.settings.segment_seconds))
        ctk.CTkLabel(clips, text="Segment seconds").grid(row=3, column=0, sticky="w", padx=8, pady=6)

        self.entry_fps = ctk.CTkEntry(clips, width=120)
        self.entry_fps.grid(row=4, column=1, sticky="w", padx=8, pady=6)
        self.entry_fps.insert(0, str(self.settings.fps))
        ctk.CTkLabel(clips, text="FPS").grid(row=4, column=0, sticky="w", padx=8, pady=6)

        self.entry_res = ctk.CTkEntry(clips, width=200)
        self.entry_res.grid(row=5, column=1, sticky="w", padx=8, pady=6)
        self.entry_res.insert(0, f"{self.settings.width}x{self.settings.height}")
        ctk.CTkLabel(clips, text="Resolution (WxH)").grid(row=5, column=0, sticky="w", padx=8, pady=6)

        self.upscale_opt = ctk.CTkOptionMenu(clips, values=["none","1080p","4k"])
        self.upscale_opt.set(self.settings.export_upscale)
        self.upscale_opt.grid(row=6, column=1, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(clips, text="Export upscale").grid(row=6, column=0, sticky="w", padx=8, pady=6)

        # News settings
        news = ctk.CTkFrame(outer)
        news.grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        news.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(news, text="News refresh seconds").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.entry_news_refresh = ctk.CTkEntry(news, width=140)
        self.entry_news_refresh.grid(row=0, column=1, sticky="w", padx=8, pady=8)
        self.entry_news_refresh.insert(0, str(self.settings.news_refresh_seconds))

        # Apply
        btns = ctk.CTkFrame(outer)
        btns.grid(row=4, column=0, sticky="ew", padx=8, pady=12)
        ctk.CTkButton(btns, text="Apply Settings", command=self._apply_settings).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Open audio device picker help", command=self._open_audio_help).pack(side="left", padx=8)

    def _pick_color(self):
        import tkinter.colorchooser as cc
        col = cc.askcolor(title="Pick accent color")
        if col and col[1]:
            self.accent_entry.delete(0, "end")
            self.accent_entry.insert(0, col[1])

    def _open_audio_help(self):
        self._chat_append("Simian", "To capture system audio + mic, set env vars:\n"
                                    "  SIMIAN_SYSTEM_AUDIO=\"<Exact dshow name>\"\n"
                                    "  SIMIAN_MIC=\"<Exact dshow name>\"\n"
                                    "List devices:\n  python -m services.audio_devices\n")

    def _apply_settings(self):
        # update from UI
        self.settings.voice_enabled = (self.chk_voice.get() == 1)
        self.settings.voice_id = self.voice_id_entry.get().strip() or self.settings.voice_id
        self.settings.accent_hex = self.accent_entry.get().strip() or self.settings.accent_hex

        self.settings.clips_dir = self.entry_clips_dir.get().strip() or self.settings.clips_dir

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
            self.settings.width = int(w); self.settings.height = int(h)
        except Exception:
            pass

        self.settings.export_upscale = self.upscale_opt.get()
        try:
            self.settings.news_refresh_seconds = int(self.entry_news_refresh.get().strip())
        except Exception:
            pass

        save_settings(self.settings)
        self.log("Settings applied.")

        # Apply voice on/off immediately
        if self.settings.voice_enabled:
            self._start_mic_listener()
        else:
            self._stop_mic_listener()

    # ----------------------- MIC LISTENER -----------------------

    def _start_mic_listener(self):
        if self.mic_listener is None:
            self.mic_listener = MicListenerService(log_cb=self.log, command_cb=self._on_voice_command)
        self.mic_listener.start()
        self.lbl_mic.configure(text="Mic listener: running")

    def _stop_mic_listener(self):
        if self.mic_listener:
            self.mic_listener.stop()
        self.lbl_mic.configure(text="Mic listener: stopped")

    def _on_voice_command(self, cmd: str, meta: Dict[str, Any]):
        # run on listener thread, bounce to UI thread
        if cmd == "clip":
            extra = int(meta.get("extra_seconds", self.settings.extra_seconds_default) or 0)
            self.after(0, lambda: self.extra_entry.delete(0, "end"))
            self.after(0, lambda: self.extra_entry.insert(0, str(extra)))
            self.after(0, self._export_clip)
        elif cmd == "buffer_start":
            self.after(0, self._start_replay_buffer)
        elif cmd == "buffer_stop":
            self.after(0, self._stop_replay_buffer)

    # ----------------------- CLOSE -----------------------

    def _on_close(self):
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


def sys_exe() -> str:
    # Works inside venv + PyInstaller (usually)
    import sys
    return sys.executable


def main():
    # Ensure clips directory exists (requested path)
    s = load_settings()
    Path(s.clips_dir).mkdir(parents=True, exist_ok=True)
    Path(s.buffer_dir).mkdir(parents=True, exist_ok=True)

    app = SimianApp()
    app.mainloop()


if __name__ == "__main__":
    main()
