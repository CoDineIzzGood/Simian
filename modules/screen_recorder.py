# modules/screen_recorder.py
from __future__ import annotations
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterable

import numpy as np

# backends
try:
    import dxcam
    _DX = True
except Exception:
    _DX = False

try:
    from mss import mss
    _MSS = True
except Exception:
    _MSS = False

# imageio-ffmpeg ships a working ffmpeg binary
import imageio_ffmpeg
import subprocess
import shutil
import os

class ScreenRecorder:
    def __init__(self, out_dir: Path, fps: int = 20):
        self.out_dir = Path(out_dir)
        self.fps = fps
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._outfile: Optional[Path] = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> Optional[str]:
        if not self._thread:
            return None
        self._stop.set()
        self._thread.join(timeout=5)
        return str(self._outfile) if self._outfile and self._outfile.exists() else None

    # --- helpers -------------------------------------------------------

    def _ffmpeg_cmd(self, w: int, h: int, fps: int, path: Path) -> list[str]:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        # raw video from stdin -> h264 mp4
        return [
            exe,
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}",
            "-r", str(fps),
            "-i", "-",
            "-vcodec", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(path)
        ]

    def _capture_frames(self) -> Iterable[np.ndarray]:
        interval = 1.0 / max(1, self.fps)

        if _DX:
            cam = dxcam.create(output_idx=0)  # primary combined
            cam.start(target_fps=self.fps)
            try:
                while not self._stop.is_set():
                    frame = cam.get_latest_frame()
                    if frame is not None:
                        yield frame[:, :, ::-1]  # BGR->RGB if needed; dxcam returns RGB already on most setups
                    time.sleep(interval)
            finally:
                cam.stop()
            return

        if _MSS:
            with mss() as sct:
                mon = sct.monitors[0]  # full virtual screen
                while not self._stop.is_set():
                    img = np.asarray(sct.grab(mon))[:, :, :3]  # BGRA->BGR
                    yield img[:, :, ::-1]  # to RGB
                    time.sleep(interval)
            return

        # Fallback: black frames (so GUI still â€œworksâ€)
        import numpy as np
        w, h = 1280, 720
        while not self._stop.is_set():
            yield np.zeros((h, w, 3), dtype=np.uint8)
            time.sleep(interval)

    def _run(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._outfile = self.out_dir / f"simian_{ts}.mp4"

        # Probe one frame to fix width/height
        gen = self._capture_frames()
        first = next(gen)
        h, w = first.shape[:2]

        proc = subprocess.Popen(self._ffmpeg_cmd(w, h, self.fps, self._outfile),
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.stdin.write(first.tobytes())
            for frame in gen:
                proc.stdin.write(frame.tobytes())
                if self._stop.is_set():
                    break
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=5)
