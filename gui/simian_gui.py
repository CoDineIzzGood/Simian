import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
import requests
import queue
import os
from modules.screen_recorder import ScreenRecorder

class SimianGUI:
    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")
        self.root = tk.Tk()
        self.root.title("Simian")
        self.root.geometry("780x560")

        self.txt = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, height=22)
        self.txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        frm = tk.Frame(self.root)
        frm.pack(fill=tk.X, padx=8, pady=(0,8))

        self.entry = tk.Entry(frm)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda e: self.send())

        tk.Button(frm, text="Send", command=self.send).pack(side=tk.LEFT, padx=6)
        self.rec_btn = tk.Button(frm, text="Start Rec", command=self.toggle_record)
        self.rec_btn.pack(side=tk.LEFT)

        self.recorder = ScreenRecorder()
        self.queue = queue.Queue()
        self.root.after(100, self._pump)

    def append(self, who: str, text: str):
        self.txt.insert(tk.END, f"[{who}] {text}\n")
        self.txt.see(tk.END)

    def send(self):
        prompt = self.entry.get().strip()
        if not prompt:
            return
        self.entry.delete(0, tk.END)
        self.append("You", prompt)
        threading.Thread(target=self._do_chat, args=(prompt,), daemon=True).start()

    def _do_chat(self, prompt: str):
        try:
            r = requests.post(f"{self.api_base}/api/chat", json={"prompt": prompt}, timeout=120)
            r.raise_for_status()
            resp = r.json().get("response", "")
            self.queue.put(("Simian", resp))
        except Exception as e:
            self.queue.put(("Error", str(e)))

    def toggle_record(self):
        try:
            if not self.recorder.is_running():
                out = self.recorder.start()
                self.append("Recorder", f"Recording to {out}")
                self.rec_btn.configure(text="Stop Rec")
            else:
                path = self.recorder.stop()
                self.append("Recorder", f"Saved {path}")
                self.rec_btn.configure(text="Start Rec")
        except Exception as e:
            messagebox.showerror("Recorder", str(e))

    def _pump(self):
        try:
            while True:
                who, msg = self.queue.get_nowait()
                self.append(who, msg)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

def run(api_base: str):
    SimianGUI(api_base).root.mainloop()

if __name__ == "__main__":
    run(os.environ.get("SIMIAN_API", "http://127.0.0.1:8000"))
