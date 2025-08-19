
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Optional

from modules.llm_client import LLMClient
from modules.screen_recorder import ScreenRecorder
from utils.greetings import get_wakeup_message

MONKEY_IMG_SIZE = 30  # px

def _load_img(path: Path) -> Optional[tk.PhotoImage]:
    try:
        from PIL import Image, ImageTk  # type: ignore
    except Exception:
        return None
    try:
        img = Image.open(path).convert("RGBA").resize((MONKEY_IMG_SIZE, MONKEY_IMG_SIZE))
        return ImageTk.PhotoImage(img)
    except Exception:
        return None

def run(api_base: str):
    root = tk.Tk()
    root.title("Simian")
    root.geometry("1200x700")
    root.configure(bg="#111")

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TNotebook", background="#111", borderwidth=0)
    style.configure("TNotebook.Tab", background="#222", foreground="#ccc")
    style.map("TNotebook.Tab", background=[("selected","#333")])
    style.configure("TButton", background="#6a31ff", foreground="#fff", padding=8)
    style.map("TButton", background=[("active","#7a4fff")])

    header = tk.Frame(root, bg="#111")
    header.pack(fill="x", padx=10, pady=6)
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    monkey = _load_img(assets_dir / "monkey_idle.png")
    if monkey:
        tk.Label(header, image=monkey, bg="#111").pack(side="left", padx=(4,8))
    tk.Label(header, text="Ready", fg="#ddd", bg="#111", font=("Segoe UI", 14, "bold")).pack(side="left")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=6)

    chat_tab = tk.Frame(nb, bg="#111")
    nb.add(chat_tab, text="Chat")

    txt = tk.Text(chat_tab, bg="#1a1a1a", fg="#ddd", insertbackground="#ddd", wrap="word")
    txt.pack(fill="both", expand=True, padx=8, pady=8)
    input_frame = tk.Frame(chat_tab, bg="#111")
    input_frame.pack(fill="x", padx=8, pady=(0,8))
    entry = tk.Entry(input_frame, bg="#1a1a1a", fg="#ddd", insertbackground="#ddd")
    entry.pack(side="left", fill="x", expand=True, padx=(0,8))
    send_btn = ttk.Button(input_frame, text="Send")
    stop_rec_btn = ttk.Button(input_frame, text="Stop Rec")
    send_btn.pack(side="left", padx=(0,8))
    stop_rec_btn.pack(side="left")

    clips_tab = tk.Frame(nb, bg="#111")
    nb.add(clips_tab, text="Clips")
    clips_list = tk.Listbox(clips_tab, bg="#1a1a1a", fg="#ddd")
    clips_list.pack(fill="both", expand=True, padx=8, pady=8)

    mon_tab = tk.Frame(nb, bg="#111")
    nb.add(mon_tab, text="Screen Monitor")
    mon_status = tk.StringVar(value="Monitor running: 0s")
    tk.Label(mon_tab, textvariable=mon_status, bg="#111", fg="#ddd").pack(pady=8)
    start_mon_btn = ttk.Button(mon_tab, text="Start Monitor")
    stop_mon_btn = ttk.Button(mon_tab, text="Stop Monitor")
    start_mon_btn.pack(side="left", padx=8)
    stop_mon_btn.pack(side="left", padx=8)

    settings_tab = tk.Frame(nb, bg="#111")
    nb.add(settings_tab, text="Settings")
    tk.Label(settings_tab, text="Model (Ollama):", fg="#ddd", bg="#111").pack(anchor="w", padx=8, pady=(10,2))
    model_var = tk.StringVar(value=os.getenv("SIMIAN_MODEL","simian"))
    model_entry = tk.Entry(settings_tab, textvariable=model_var, bg="#1a1a1a", fg="#ddd", insertbackground="#ddd")
    model_entry.pack(fill="x", padx=8)
    tk.Label(settings_tab, text="API Base:", fg="#ddd", bg="#111").pack(anchor="w", padx=8, pady=(10,2))
    api_var = tk.StringVar(value=api_base)
    api_entry = tk.Entry(settings_tab, textvariable=api_var, bg="#1a1a1a", fg="#ddd", insertbackground="#ddd")
    api_entry.pack(fill="x", padx=8)
    tk.Label(settings_tab, text="Voice (edge-tts):", fg="#ddd", bg="#111").pack(anchor="w", padx=8, pady=(10,2))
    voice_var = tk.StringVar(value="en-US-AriaNeural")
    voice_entry = tk.Entry(settings_tab, textvariable=voice_var, bg="#1a1a1a", fg="#ddd", insertbackground="#ddd")
    voice_entry.pack(fill="x", padx=8)

    clips_dir = Path(__file__).resolve().parent.parent / "data" / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    client = LLMClient(api_var.get())
    recorder = ScreenRecorder(str(clips_dir), fps=15, monitor_indexes=None)

    txt.insert("end", f"[Simian] {get_wakeup_message()}
")

    def refresh_clips():
        clips_list.delete(0, "end")
        for p in sorted(clips_dir.glob("*.mp4")):
            clips_list.insert("end", p.name)

    def on_send():
        user = entry.get().strip()
        if not user:
            return
        txt.insert("end", f"[You] {user}\n")
        entry.delete(0, "end")
        try:
            reply = client.chat(messages=[{"role":"user","content":user}], model=model_var.get())
        except Exception as e:
            messagebox.showerror("Chat error", str(e))
            return
        if isinstance(reply, (dict, list)):
            reply = str(reply)
        txt.insert("end", f"[Simian] {reply}\n")
        txt.see("end")

    def on_stop_rec():
        out = recorder.stop()
        refresh_clips()
        if out:
            messagebox.showinfo("Recorder", f"Saved: {out}")

    def on_start_mon():
        recorder.start()
        start = [int(__import__("time").time())]
        def tick():
            if recorder._thread and recorder._thread.is_alive():
                sec = int(__import__("time").time()) - start[0]
                mon_status.set(f"Monitor running: {sec}s")
                root.after(1000, tick)
        tick()

    def on_stop_mon():
        recorder.stop()
        refresh_clips()

    send_btn.configure(command=on_send)
    stop_rec_btn.configure(command=on_stop_rec)
    start_mon_btn.configure(command=on_start_mon)
    stop_mon_btn.configure(command=on_stop_mon)

    recorder.start()
    root.after(500, refresh_clips)

    root.mainloop()
