"""
Audio fallback recorder for the replay buffer.

Used when FFmpeg's DirectShow audio path can't open Stereo Mix /
mic / WASAPI loopback (typical Win11-without-VAC situation). Captures
mic and desktop audio via Python ``sounddevice`` directly to WAV files
that the export step muxes into the final clip alongside the video-only
ffmpeg segments.

Design rules:
  * Lazy imports. ``sounddevice`` is already used elsewhere in Simian
    (see ``services/audio_devices.py``) but is optional at runtime on
    machines without PortAudio. A missing import never raises; it just
    means "this fallback is unavailable, log and continue".
  * Never block the video pipeline. Each capture stream runs in its
    own thread; failures are logged and the failing stream is dropped.
  * WAV files written incrementally so a crash mid-recording leaves a
    playable partial. Mono float32 at 44100Hz; small enough to mux at
    export, large enough to be intelligible.
  * No dependency on ``soundfile``: we use stdlib ``wave`` + a tiny
    float32->int16 conversion via numpy (numpy is already a hard dep).
"""

from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional


@dataclass
class FallbackPaths:
    mic_wav: Optional[Path] = None
    desktop_wav: Optional[Path] = None


class AudioFallbackRecorder:
    """Captures mic + (optional) WASAPI desktop loopback into WAV files.

    Use:
        rec = AudioFallbackRecorder(buffer_dir, log_cb=app.log)
        rec.start(mic_wanted=True, desktop_wanted=True)
        ...
        rec.stop()
        paths = rec.paths()  # FallbackPaths(mic_wav=..., desktop_wav=...)

    ``start`` is non-blocking. ``stop`` is idempotent. After ``stop``,
    ``paths()`` returns the WAV files that were actually written (or
    ``None`` for any stream that never produced samples).
    """

    SAMPLE_RATE = 44100
    CHANNELS = 1
    DTYPE = "float32"
    BLOCKSIZE = 1024

    def __init__(self, buffer_dir: Path, log_cb: Optional[Callable[[str], None]] = None):
        self.buffer_dir = Path(buffer_dir)
        self.log = log_cb or (lambda _msg: None)
        self._mic_thread: Optional[threading.Thread] = None
        self._desk_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._mic_path: Optional[Path] = None
        self._desk_path: Optional[Path] = None
        self._mic_frames_written = 0
        self._desk_frames_written = 0
        self._started = False

    # ---------- internal helpers --------------------------------------

    def _have_sounddevice(self) -> bool:
        try:
            import sounddevice  # noqa: F401  type: ignore
            return True
        except Exception:
            return False

    def _have_numpy(self) -> bool:
        try:
            import numpy  # noqa: F401  type: ignore
            return True
        except Exception:
            return False

    def _open_wav(self, path: Path) -> Optional["wave.Wave_write"]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            wf = wave.open(str(path), "wb")
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)  # int16
            wf.setframerate(self.SAMPLE_RATE)
            return wf
        except Exception as exc:
            self.log(f"[ReplayAudio] Failed to open WAV {path.name}: {exc}")
            return None

    def _capture_loop(
        self,
        kind: str,
        path: Path,
        wasapi_loopback: bool,
        device: Optional[int] = None,
    ) -> int:
        """Capture stream loop. Returns frame count written."""
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:
            self.log(f"[ReplayAudio] {kind}: sounddevice/numpy unavailable ({exc}); skipping.")
            return 0

        # WASAPI loopback only exists on Windows with the WASAPI host
        # API. The keyword-argument shape changed between sounddevice
        # builds: 0.4.x exposed ``WasapiSettings(loopback=True)`` on
        # PortAudio 19.7+ Windows builds, but 0.5.5 (the version Alex
        # is on) does NOT accept that kwarg and raises:
        #     WasapiSettings.__init__() got an unexpected keyword
        #     argument 'loopback'
        # Per the Pass R user correction we MUST NOT call that kwarg
        # blindly. Instead, introspect the constructor signature and
        # only pass ``loopback=True`` when it's actually supported.
        # On unsupported builds we log a clean "not supported" line
        # and skip desktop capture (mic capture path is independent
        # and stays working).
        extra: Any = None
        if wasapi_loopback:
            settings_cls = getattr(sd, "WasapiSettings", None)
            supports_loopback = False
            if settings_cls is not None:
                try:
                    import inspect
                    sig = inspect.signature(settings_cls.__init__)
                    supports_loopback = "loopback" in sig.parameters
                except Exception:
                    supports_loopback = False
            if not supports_loopback:
                self.log(
                    "[ReplayAudio] WASAPI loopback not supported in this "
                    "sounddevice build; desktop audio capture disabled. "
                    "Mic capture will continue normally."
                )
                return 0
            try:
                extra = settings_cls(loopback=True)  # type: ignore[call-arg]
            except Exception as exc:
                self.log(
                    f"[ReplayAudio] {kind}: WASAPI loopback init failed ({exc}); "
                    "skipping desktop capture."
                )
                return 0

        wf = self._open_wav(path)
        if wf is None:
            return 0

        frames_written = 0
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype=self.DTYPE,
                blocksize=self.BLOCKSIZE,
                device=device,
                extra_settings=extra,
            )
        except Exception as exc:
            wf.close()
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            self.log(f"[ReplayAudio] {kind}: open stream failed ({exc}); skipping.")
            return 0

        self.log(f"[ReplayAudio] {kind}: capture started -> {path.name}")
        try:
            with stream:
                while not self._stop_evt.is_set():
                    try:
                        block, _ = stream.read(self.BLOCKSIZE)
                    except Exception as exc:
                        self.log(f"[ReplayAudio] {kind}: read failed ({exc}); stopping.")
                        break
                    try:
                        # float32 -> int16 with clipping. block shape:
                        # (frames, channels). Mono so squeeze to 1D.
                        arr = np.asarray(block, dtype=np.float32)
                        if arr.ndim == 2 and arr.shape[1] == 1:
                            arr = arr[:, 0]
                        elif arr.ndim == 2 and arr.shape[1] > 1:
                            # Downmix multi-channel to mono.
                            arr = arr.mean(axis=1)
                        clipped = np.clip(arr, -1.0, 1.0)
                        as_i16 = (clipped * 32767.0).astype(np.int16)
                        wf.writeframes(as_i16.tobytes())
                        frames_written += as_i16.shape[0]
                    except Exception as exc:
                        self.log(f"[ReplayAudio] {kind}: write failed ({exc}); stopping.")
                        break
        finally:
            try:
                wf.close()
            except Exception:
                pass

        if frames_written == 0:
            self.log(f"[ReplayAudio] {kind}: no samples captured; removing empty WAV.")
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            secs = frames_written / float(self.SAMPLE_RATE)
            self.log(f"[ReplayAudio] {kind}: stopped after {secs:.1f}s ({frames_written} frames).")
        return frames_written

    # ---------- public API --------------------------------------------

    def start(self, mic_wanted: bool, desktop_wanted: bool) -> None:
        if self._started:
            return
        if not self._have_sounddevice() or not self._have_numpy():
            self.log(
                "[ReplayAudio] Fallback unavailable: sounddevice or numpy missing. "
                "pip install sounddevice numpy to enable desktop/mic capture when "
                "DirectShow can't see audio devices."
            )
            return

        self._started = True
        self._stop_evt.clear()
        ts = time.strftime("%Y%m%d_%H%M%S")

        if mic_wanted:
            self._mic_path = self.buffer_dir / f"audio_mic_{ts}.wav"
            self._mic_thread = threading.Thread(
                target=self._capture_loop_runner,
                args=("mic", self._mic_path, False),
                daemon=True,
                name="replay-fallback-mic",
            )
            self._mic_thread.start()

        if desktop_wanted:
            self._desk_path = self.buffer_dir / f"audio_desk_{ts}.wav"
            self._desk_thread = threading.Thread(
                target=self._capture_loop_runner,
                args=("desktop", self._desk_path, True),
                daemon=True,
                name="replay-fallback-desktop",
            )
            self._desk_thread.start()

        self.log(
            f"[ReplayAudio] Fallback recorder armed (mic={'yes' if mic_wanted else 'no'}, "
            f"desktop={'yes' if desktop_wanted else 'no'})."
        )

    def _capture_loop_runner(self, kind: str, path: Path, wasapi_loopback: bool) -> None:
        try:
            n = self._capture_loop(kind, path, wasapi_loopback)
        except Exception as exc:
            self.log(f"[ReplayAudio] {kind}: unexpected error ({exc}); stream dropped.")
            n = 0
        if kind == "mic":
            self._mic_frames_written = n
        else:
            self._desk_frames_written = n

    def stop(self, timeout_s: float = 4.0) -> None:
        if not self._started:
            return
        self._stop_evt.set()
        for t in (self._mic_thread, self._desk_thread):
            if t is None:
                continue
            try:
                t.join(timeout=timeout_s)
            except Exception:
                pass
        self._started = False

    def paths(self) -> FallbackPaths:
        mic = self._mic_path if self._mic_frames_written > 0 and self._mic_path and self._mic_path.exists() else None
        desk = self._desk_path if self._desk_frames_written > 0 and self._desk_path and self._desk_path.exists() else None
        return FallbackPaths(mic_wav=mic, desktop_wav=desk)

    def is_running(self) -> bool:
        return self._started and not self._stop_evt.is_set()


def cleanup_old_fallback_wavs(buffer_dir: Path, keep_recent: int = 8) -> None:
    """Trim ``audio_*_*.wav`` files in buffer_dir to the N most recent.

    Called by the replay buffer on start to bound disk usage from the
    fallback path. Best-effort, never raises.
    """
    try:
        wavs: List[Path] = sorted(
            [p for p in buffer_dir.glob("audio_*.wav") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in wavs[keep_recent:]:
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        return
