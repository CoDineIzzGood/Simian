# gui/simian_gui.py (delta version with Settings: voice + model + api base)
from __future__ import annotations
import os, threading, requests, json, tkinter as tk
import customtkinter as ctk
from pathlib import Path

APP_TITLE = "Simian"
API_BASE_DEFAULT = "http://127.0.0.1:8000"

# --- small helpers ---
def api_get(path:str):
    try:
        r = requests.get(f"{state['api_base']}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def api_post(path:str, payload:dict):
    try:
        r = requests.post(f"{state['api_base']}{path}", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

state = {
    "model": os.environ.get("SIMIAN_MODEL","llama3.1"),
    "api_base": os.environ.get("SIMIAN_API_BASE", API_BASE_DEFAULT),
    "voice": os.environ.get("SIMIAN_VOICE","en-US-GuyNeural"),
    "monitor_running": False,
}

class SimianGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x680")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        # header with bigger monkey logo
        hdr = ctk.CTkFrame(self)
        hdr.pack(side="top", fill="x", padx=10, pady=(8,4))
        self.logo = ctk.CTkLabel(hdr, text="🦧", font=ctk.CTkFont(size=28))   # bigger
        self.logo.pack(side="left")
        self.status = ctk.CTkLabel(hdr, text="Starting...", anchor="w")
        self.status.pack(side="left", padx=8)

        # Tabs
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_chat = self.tabs.add("Chat")
        self.tab_clips = self.tabs.add("Clips")
        self.tab_monitor = self.tabs.add("Screen Monitor")
        self.tab_settings = self.tabs.add("Settings")

        # Chat area
        self.text = tk.Text(self.tab_chat, bg="#222", fg="#ddd", insertbackground="#ddd", wrap="word")
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        entry_row = ctk.CTkFrame(self.tab_chat)
        entry_row.pack(fill="x", padx=8, pady=(0,8))
        self.entry = ctk.CTkEntry(entry_row)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0,8))
        ctk.CTkButton(entry_row, text="Send", command=self.send).pack(side="left")

        # Monitor controls
        self.monitor_lbl = ctk.CTkLabel(self.tab_monitor, text="Monitor: stopped")
        self.monitor_lbl.pack(pady=8)
        btns = ctk.CTkFrame(self.tab_monitor); btns.pack(pady=6)
        ctk.CTkButton(btns, text="Start Monitor", command=self.start_monitor).pack(side="left", padx=5)
        ctk.CTkButton(btns, text="Stop Monitor", command=self.stop_monitor).pack(side="left", padx=5)

        # Settings
        grid = ctk.CTkFrame(self.tab_settings); grid.pack(fill="x", padx=10, pady=10)
        # model
        ctk.CTkLabel(grid, text="Model (Ollama):").grid(row=0, column=0, sticky="w", padx=4, pady=6)
        self.model_box = ctk.CTkComboBox(grid, values=["llama3.1","llama3","phi3","mistral"], command=self.on_model)
        self.model_box.set(state["model"]); self.model_box.grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        # api base
        ctk.CTkLabel(grid, text="API Base:").grid(row=1, column=0, sticky="w", padx=4, pady=6)
        self.api_entry = ctk.CTkEntry(grid); self.api_entry.insert(0, state["api_base"])
        self.api_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=6)
        # voice
        ctk.CTkLabel(grid, text="Voice:").grid(row=2, column=0, sticky="w", padx=4, pady=6)
        voices = ["en-US-GuyNeural","en-US-JennyNeural","en-GB-RyanNeural","en-AU-NatashaNeural"]
        self.voice_box = ctk.CTkComboBox(grid, values=voices, command=self.on_voice)
        self.voice_box.set(state["voice"]); self.voice_box.grid(row=2, column=1, sticky="ew", padx=4, pady=6)
        grid.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(self.tab_settings, text="Save", command=self.save_settings).pack(pady=(4,10))

        self.after(100, self._post_init)

    # --- lifecycle ---
    def _post_init(self):
        # load greeting + set to status; autostart monitor for "privacy with a button"
        hello = api_get("/hello")
        if "greeting" in hello:
            self.append(f"[{APP_TITLE}] {hello['greeting']}")
        self.status.configure(text="Ready")
        self.start_monitor()

    # --- chat ---
    def append(self, msg:str):
        self.text.insert("end", msg + "\n")
        self.text.see("end")

    def send(self):
        prompt = self.entry.get().strip()
        if not prompt: return
        self.append(f"[You] {prompt}")
        self.entry.delete(0, "end")
        payload = {"prompt": prompt, "model": state["model"], "options": {}}
        res = api_post("/api/chat", payload)
        if isinstance(res, dict) and "error" in res:
            self.append(f"[Error] {res['error']}")
        else:
            self.append(f"[{APP_TITLE}] {res}")

    # --- monitor ---
    def start_monitor(self):
        if state["monitor_running"]: return
        api_get("/api/monitor/start")
        state["monitor_running"] = True
        self.monitor_lbl.configure(text="Monitor: running")

    def stop_monitor(self):
        if not state["monitor_running"]: return
        api_get("/api/monitor/stop")
        state["monitor_running"] = False
        self.monitor_lbl.configure(text="Monitor: stopped")

    # --- settings ---
    def on_model(self, value): state["model"] = value
    def on_voice(self, value): state["voice"] = value

    def save_settings(self):
        state["api_base"] = self.api_entry.get().strip() or API_BASE_DEFAULT
        os.environ["SIMIAN_MODEL"] = state["model"]
        os.environ["SIMIAN_VOICE"] = state["voice"]
        os.environ["SIMIAN_API_BASE"] = state["api_base"]
        self.append("[Simian] Settings saved.")

def launch_gui():
    app = SimianGUI()
    app.mainloop()

if __name__ == "__main__":
    launch_gui()
