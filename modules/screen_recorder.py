
# modules/screen_recorder.py
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import mss
import numpy as np

try:
    import cv2
except Exception as e:
    raise RuntimeError("OpenCV (cv2) is required for screen recording. pip install opencv-python") from e

CLIPS_DIR = Path("data/clips")
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

class ScreenRecorder:
    def __init__(self, fps: int = 20, region: dict | None = None, filename_prefix: str = "simian"):
        self.fps = fps
        self.region = region  # e.g., {"top":0,"left":0,"width":1920,"height":1080}
        self.filename_prefix = filename_prefix
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._out = None
        self._path: Path | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Path:
        if self.is_running():
            return self._path  # already running

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = CLIPS_DIR / f"{self.filename_prefix}_{ts}.mp4"

        # Prepare capture
        sct = mss.mss()
        mon = self.region or sct.monitors[1]  # primary monitor
        width, height = mon["width"], mon["height"]

        # FourCC for .mp4
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(out_path), fourcc, self.fps, (width, height))

        if not out.isOpened():
            raise RuntimeError("Failed to open VideoWriter. Ensure opencv is installed and you have write permission.")

        self._out = out
        self._path = out_path
        self._stop.clear()

        def _run():
            frame_interval = 1.0 / max(self.fps, 1)
            with sct:
                last = 0.0
                while not self._stop.is_set():
                    start = time.perf_counter()
                    img = np.array(sct.grab(mon))  # RGBA
                    frame = img[..., :3]  # BGR expected after conversion below
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    self._out.write(frame)
                    # sleep to keep fps
                    elapsed = time.perf_counter() - start
                    rem = frame_interval - elapsed
                    if rem > 0:
                        time.sleep(rem)

            # Finalize properly
            self._out.release()
            self._out = None

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return out_path

    def stop(self) -> Path | None:
        if not self.is_running():
            return self._path
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None
        p = self._path
        self._path = None
        return p
