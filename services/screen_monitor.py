# services/screen_monitor.py
from __future__ import annotations
import threading, time

_monitor_flag = False
_lock = threading.Lock()

def start():
    global _monitor_flag
    with _lock:
        if _monitor_flag: return
        _monitor_flag = True
    threading.Thread(target=_run, daemon=True).start()

def stop():
    global _monitor_flag
    with _lock:
        _monitor_flag = False

def _run():
    # placeholder monitor loop – extend with your detection
    while True:
        with _lock:
            if not _monitor_flag: break
        time.sleep(1.0)
