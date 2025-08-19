# modules/screen_recorder.py
"""
Screen recorder using ffmpeg (gdigrab on Windows). No mss, no thread-local errors.
Writes MP4 clips into data/clips/. Auto-rotates files by timestamp.
"""
from __future__ import annotations
import os, subprocess, datetime, threading, shlex, sys, signal
from pathlib import Path

IS_WIN = os.name == "nt"
FFMPEG_PATH = os.path.join(os.path.dirname(__file__), "..", "ffmpeg-7.1.1", "bin", "ffmpeg.exe") if IS_WIN else "ffmpeg"

CLIPS_DIR = Path(__file__).resolve().parents[1] / "data" / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

class Recorder:
    def __init__(self, fps:int=30, q:int=25, monitor:str="desktop"):
        self.fps = fps
        self.q = q
        self.monitor = monitor
        self.proc: subprocess.Popen | None = None
        self.filepath: Path | None = None
        self._lock = threading.Lock()

    def _next_filepath(self)->Path:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return CLIPS_DIR / f"simian_{ts}.mp4"

    def start(self):
        if not IS_WIN:
            raise RuntimeError("This ffmpeg recorder is currently Windows-only (gdigrab).")
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return self.filepath
            self.filepath = self._next_filepath()
            # Build ffmpeg command
            # gdigrab captures whole desktop; -framerate first for gdigrab
            # Use h264_nvenc if available, else libx264
            video_codec = "libx264"
            args = [
                FFMPEG_PATH, "-y",
                "-f", "gdigrab",
                "-framerate", str(self.fps),
                "-i", "desktop",
                "-pix_fmt", "yuv420p",
                "-vcodec", video_codec,
                "-preset", "veryfast",
                "-crf", str(23),
                "-movflags", "+faststart",
                str(self.filepath)
            ]
            creationflags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
            return self.filepath

    def stop(self)->Path | None:
        with self._lock:
            if not self.proc:
                return None
            try:
                if IS_WIN:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT if hasattr(signal, "CTRL_BREAK_EVENT") else signal.SIGTERM)
                self.proc.terminate()
            except Exception:
                pass
            finally:
                self.proc.wait(timeout=5)
                fp = self.filepath
                self.proc = None
                self.filepath = None
                return fp

RECORDER_SINGLETON: Recorder | None = None

def start_recording()->str:
    global RECORDER_SINGLETON
    if not RECORDER_SINGLETON:
        RECORDER_SINGLETON = Recorder()
    path = RECORDER_SINGLETON.start()
    return str(path)

def stop_recording()->str | None:
    global RECORDER_SINGLETON
    if not RECORDER_SINGLETON:
        return None
    path = RECORDER_SINGLETON.stop()
    return str(path) if path else None
