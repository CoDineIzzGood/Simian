# services/screen_monitor.py
from __future__ import annotations
from pathlib import Path
from datetime import datetime
import threading, time, os

try:
    import dxcam  # pip install dxcam
except Exception:
    dxcam = None

try:
    import mss  # pip install mss
except Exception:
    mss = None

_running = False
_thread: threading.Thread | None = None
_out_path: Path | None = None

def _clips_dir() -> Path:
    d = Path(__file__).resolve().parents[1] / "data" / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _record_loop_dxcam() -> None:
    global _running, _out_path
    cam = dxcam.create(output_idx=0)
    _out_path = _clips_dir() / f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

    # Minimal â€œmonitoringâ€ loop â€“ replace with your real encoder later.
    # For now, we just keep the loop alive to simulate activity and timing.
    start = time.time()
    while _running and (time.time() - start) < 10:  # record ~10s placeholder
        frame = cam.get_latest_frame()
        time.sleep(0.03)
    # TODO: encode frames to MP4 using ffmpeg-python or imageio-ffmpeg.

def _record_loop_mss() -> None:
    global _running, _out_path
    sct = mss.mss()
    monitors = sct.monitors[1:]  # all screens
    _out_path = _clips_dir() / f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    start = time.time()
    while _running and (time.time() - start) < 10:
        for mon in monitors:
            _ = sct.grab(mon)
        time.sleep(0.03)
    # TODO: encode frames to MP4.

def start() -> bool:
    global _running, _thread
    if _running:
        return False
    _running = True
    target = _record_loop_mss if (dxcam is None and mss is not None) else _record_loop_dxcam
    _thread = threading.Thread(target=target, daemon=True)
    _thread.start()
    return True

def stop() -> str | None:
    global _running, _thread, _out_path
    if not _running:
        return None
    _running = False
    if _thread and _thread.is_alive():
        _thread.join(timeout=2.0)
    return str(_out_path) if _out_path else None
