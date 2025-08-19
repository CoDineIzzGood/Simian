
import os
import time
import threading
from pathlib import Path
from typing import Optional, List

import numpy as np

try:
    import dxcam  # type: ignore
    HAS_DXCAM = True
except Exception:
    HAS_DXCAM = False

try:
    import mss  # type: ignore
    import mss.tools  # noqa
    HAS_MSS = True
except Exception:
    HAS_MSS = False

import cv2  # type: ignore

class ScreenRecorder:
    def __init__(self, clips_dir: str, fps: int = 15, monitor_indexes: Optional[List[int]] = None):
        self.clips_dir = Path(clips_dir)
        self.fps = fps
        self.monitor_indexes = monitor_indexes  # None -> all
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_clip_path: Optional[Path] = None

    def _get_monitors_dxcam(self):
        cam = dxcam.create(output_idx=0)
        monitors = dxcam.device_info()["outputs"]
        idxs = self.monitor_indexes or list(range(len(monitors)))
        return idxs, monitors

    def _frame_from_dxcam(self, idxs):
        frames = []
        for i in idxs:
            cam = dxcam.create(output_idx=i)
            frame = cam.grab()
            if frame is None:
                continue
            frames.append(frame[..., ::-1])
        return frames

    def _frame_from_mss(self):
        frames = []
        with mss.mss() as sct:
            mons = sct.monitors[1:]
            idxs = self.monitor_indexes or list(range(len(mons)))
            for i in idxs:
                mon = mons[i]
                img = np.array(sct.grab(mon))
                frames.append(img[..., :3])
        return frames

    def _stack_frames(self, frames):
        if not frames:
            return None
        max_h = max(f.shape[0] for f in frames)
        padded = []
        for f in frames:
            h, w, c = f.shape
            if h < max_h:
                pad = np.zeros((max_h - h, w, 3), dtype=f.dtype)
                f = np.vstack([f, pad])
            padded.append(f)
        return np.hstack(padded)

    def _writer(self, size):
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.last_clip_path = self.clips_dir / f"simian_{ts}.mp4"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        return cv2.VideoWriter(str(self.last_clip_path), fourcc, self.fps, size)

    def _run(self):
        use_dx = HAS_DXCAM
        if not use_dx and not HAS_MSS:
            return

        if use_dx:
            idxs, _ = self._get_monitors_dxcam()
            frames = self._frame_from_dxcam(idxs)
        else:
            frames = self._frame_from_mss()
        composite = self._stack_frames(frames)
        if composite is None:
            return
        h, w = composite.shape[:2]
        writer = self._writer((w, h))

        frame_interval = 1.0 / self.fps
        while not self._stop.is_set():
            start = time.time()
            if use_dx:
                frames = self._frame_from_dxcam(idxs)
            else:
                frames = self._frame_from_mss()
            composite = self._stack_frames(frames)
            if composite is not None:
                writer.write(composite)
            dt = time.time() - start
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
        writer.release()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        return str(self.last_clip_path) if self.last_clip_path else None
