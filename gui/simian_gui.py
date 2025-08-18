
# gui/simian_gui.py
import os
from pathlib import Path
import threading
import queue
import json
from datetime import datetime

import customtkinter as ctk
from tkinter import ttk, filedialog, messagebox

from modules.screen_recorder import ScreenRecorder
from services.screen_monitor import ScreenMonitor

import httpx
import asyncio

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
API_BASE = f"http://{APP_HOST}:{APP_PORT}"

ASSETS = Path("gui/assets")
CLIPS_DIR = Path("data/clips")
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

class SimianGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Simian")
        self.geometry("900x640")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")  # gives a purple/blue accent

        # Style ttk for tabs (dark background)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background="#111111", borderwidth=0)
        style.configure("TNotebook.Tab", background="#1b1b1b", foreground="#cfcfff", padding=(12, 6))
        style.map("TNotebook.Tab", background=[("selected", "#3a1a5e")])  # purple

        self.recorder = ScreenRecorder()
        self.monitor = ScreenMonitor(on_tick=self._on_monitor_tick)
        self.is_recording = False

        # Top status with monkey
        top = ctk.CTkFrame(self, corner_radius=8)
        top.pack(fill="x", padx=10, pady=10)
        self.status_lbl = ctk.CTkLabel(top, text="🐒 Ready", font=ctk.CTkFont(size=16, weight="bold"))
        self.status_lbl.pack(side="left", padx=8, pady=8)

        # Tabs
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_chat = ctk.CTkFrame(nb)
        self.tab_clips = ctk.CTkFrame(nb)
        self.tab_monitor = ctk.CTkFrame(nb)
        self.tab_settings = ctk.CTkFrame(nb)

        nb.add(self.tab_chat, text="Chat")
        nb.add(self.tab_clips, text="Clips")
        nb.add(self.tab_monitor, text="Screen Monitor")
        nb.add(self.tab_settings, text="Settings")

        self._build_chat_tab(self.tab_chat)
        self._build_clips_tab(self.tab_clips)
        self._build_monitor_tab(self.tab_monitor)
        self._build_settings_tab(self.tab_settings)

        self.after(500, self.refresh_clips)

    # ---------- Tabs ----------
    def _build_chat_tab(self, tab):
        frm = ctk.CTkFrame(tab)
        frm.pack(fill="both", expand=True, padx=10, pady=10)

        self.chat_txt = ctk.CTkTextbox(frm, width=1, height=1, wrap="word")
        self.chat_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self.chat_txt.insert("end", "[Simian] Hello! Type below and press Send.\n")

        entry_row = ctk.CTkFrame(frm)
        entry_row.pack(fill="x", padx=6, pady=6)
        self.entry = ctk.CTkEntry(entry_row, placeholder_text="Ask Simian...")
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        send_btn = ctk.CTkButton(entry_row, text="Send", command=self.send_chat, fg_color="#7c3aed", hover_color="#6d28d9")
        send_btn.pack(side="left", padx=6)

        rec_btn = ctk.CTkButton(entry_row, text="Start Rec", command=self.toggle_recording, fg_color="#7c3aed", hover_color="#6d28d9")
        rec_btn.pack(side="left", padx=6)
        self.rec_btn = rec_btn

    def _build_clips_tab(self, tab):
        top = ctk.CTkFrame(tab)
        top.pack(fill="both", expand=True, padx=10, pady=10)
        self.clips_list = ctk.CTkScrollableFrame(top, width=1, height=1)
        self.clips_list.pack(fill="both", expand=True)
        self.no_clips = ctk.CTkLabel(top, text="No clips yet.")
        self.no_clips.pack(pady=(6, 0))

    def _build_monitor_tab(self, tab):
        frm = ctk.CTkFrame(tab)
        frm.pack(fill="x", padx=10, pady=10)
        self.monitor_lbl = ctk.CTkLabel(frm, text="Monitor idle.")
        self.monitor_lbl.pack(side="left", padx=6, pady=6)
        start = ctk.CTkButton(frm, text="Start Monitor", command=lambda: self.monitor.start(), fg_color="#7c3aed", hover_color="#6d28d9")
        stop = ctk.CTkButton(frm, text="Stop Monitor", command=lambda: self.monitor.stop(), fg_color="#7c3aed", hover_color="#6d28d9")
        start.pack(side="left", padx=6)
        stop.pack(side="left", padx=6)

    def _build_settings_tab(self, tab):
        frm = ctk.CTkFrame(tab)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(frm, text="Model (Ollama):").pack(anchor="w")
        self.model_entry = ctk.CTkEntry(frm, placeholder_text="llama3.1")
        self.model_entry.pack(fill="x", padx=6, pady=(0,10))

        ctk.CTkLabel(frm, text="API Base:").pack(anchor="w")
        self.api_entry = ctk.CTkEntry(frm, placeholder_text=API_BASE)
        self.api_entry.insert(0, API_BASE)
        self.api_entry.pack(fill="x", padx=6, pady=(0,10))

    # ---------- Actions ----------
    def set_status(self, txt: str):
        self.status_lbl.configure(text=txt)

    def send_chat(self):
        user_text = self.entry.get().strip()
        if not user_text:
            return
        self.entry.delete(0, "end")
        self.chat_txt.insert("end", f"[You] {user_text}\n")
        self.chat_txt.see("end")
        self.set_status("🐒 Thinking...")

        async def _work():
            async with httpx.AsyncClient(timeout=90) as client:
                api = (self.api_entry.get() or API_BASE).strip()
                payload = {"prompt": user_text, "model": (self.model_entry.get() or "llama3.1").strip()}
                r = await client.post(f"{api}/api/chat", json=payload)
                r.raise_for_status()
                return r.text

        def _thread():
            try:
                ans = asyncio.run(_work())
            except Exception as e:
                ans = f"[error] {e}"
            self.chat_txt.insert("end", f"[Simian] {ans}\n")
            self.chat_txt.see("end")
            self.set_status("🐒 Ready")

        threading.Thread(target=_thread, daemon=True).start()

    def toggle_recording(self):
        if not self.is_recording:
            path = self.recorder.start()
            self.is_recording = True
            self.rec_btn.configure(text="Stop Rec")
            self.set_status(f"🐒 Recording → {path.name}")
        else:
            p = self.recorder.stop()
            self.is_recording = False
            self.rec_btn.configure(text="Start Rec")
            self.set_status("🐒 Ready")
            self.refresh_clips()

    def refresh_clips(self):
        for child in list(self.clips_list.children.values()):
            child.destroy()
        files = sorted(CLIPS_DIR.glob("*.mp4"))
        if not files:
            self.no_clips.configure(text="No clips yet.")
            return
        self.no_clips.configure(text=f"{len(files)} clip(s).")
        for f in files[-200:]:
            row = ctk.CTkFrame(self.clips_list)
            row.pack(fill="x", padx=4, pady=4)
            ctk.CTkLabel(row, text=f.name).pack(side="left", padx=4)
            ctk.CTkButton(row, text="Open", width=70, command=lambda p=f: os.system(f'xdg-open "{p}" 2>/dev/null || open "{p}" 2>/dev/null || start "" "{p}"')).pack(side="right", padx=4)

    def _on_monitor_tick(self, secs: float):
        self.monitor_lbl.configure(text=f"Monitor running: {int(secs)}s")

def run(api_base: str | None = None):
    app = SimianGUI()
    app.mainloop()

if __name__ == "__main__":
    run()
