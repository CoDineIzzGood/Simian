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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple


def _ts_with_micros() -> str:
    """Microsecond-precision timestamp suffix (millisecond granularity).

    Pass U-B follow-up: ``time.strftime`` does NOT interpolate ``%f``
    -- the C ``strftime`` leaves it as a literal ``%f`` token, so two
    back-to-back rotations within the same wall-clock second produced
    identical paths and the second WAV silently truncated the first.
    ``datetime.now().strftime`` honors ``%f`` (microseconds), giving us
    millisecond uniqueness which is enough for snapshot rotation.
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


@dataclass
class FallbackPaths:
    mic_wav: Optional[Path] = None
    desktop_wav: Optional[Path] = None


# Pass V-A: mic preprocessing thresholds. Tuned conservatively so
# that quiet-but-real speech survives intact and loud speech isn't
# clipped or "telephoned" by the gate. All values are full-scale
# fractions in [0, 1].
#   - GATE_FLOOR: samples whose envelope is below this fraction of
#     full-scale are treated as room noise and attenuated.
#   - GATE_ATTEN: how much the gate squashes those samples (multiplier
#     in linear amplitude). 0.15 ~= -16 dB, audible enough that the
#     gate doesn't sound like a hard mute.
#   - NORM_TARGET: peak we aim for after normalization. Leaving 0.05
#     of headroom prevents inter-sample clipping after the int16
#     round-trip.
#   - NORM_MIN_PEAK: skip normalization on near-silent input -- we
#     don't want to pump the noise floor of an empty room up to -3 dB.
#   - NORM_MAX_PEAK: skip normalization on already-loud input. The
#     typical user shouting into a desktop mic produces peaks at or
#     above this; touching them would just risk clipping.
MIC_PREPROCESS_GATE_FLOOR = 0.012        # ~ -38 dBFS envelope floor
MIC_PREPROCESS_GATE_ATTEN = 0.15         # ~ -16 dB squash, not a hard mute
MIC_PREPROCESS_NORM_TARGET = 0.95        # ~ -0.45 dBFS post-norm peak
MIC_PREPROCESS_NORM_MIN_PEAK = 0.04      # ~ -28 dBFS; below this we don't amplify
MIC_PREPROCESS_NORM_MAX_PEAK = 0.85      # ~ -1.4 dBFS; above this normalization is a no-op


def preprocess_mic_wav(
    src: Path,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, bool, bool, float, float]:
    """Apply optional noise gate + normalization to a mic WAV.

    Returns ``(out_path, gate_applied, norm_applied, peak_before, peak_after)``.

    ``out_path`` is a sibling of ``src`` named ``<stem>_processed.wav``
    when any pass actually ran, otherwise ``src`` itself. The original
    WAV is never overwritten -- snapshot rotation depends on stable
    paths and we don't want preprocessing to race with the next-window
    capture.

    Both passes are deliberately gentle:
      * Gate uses an envelope follower (~30 ms attack/release) so the
        attenuation transitions smoothly instead of clicking on every
        threshold crossing -- a hard sample-level gate would chop the
        leading consonant off short words.
      * Normalization is a single linear gain that aims for
        ``MIC_PREPROCESS_NORM_TARGET`` and skips when the input is too
        quiet (would amplify noise) or already loud enough.

    Failure is non-fatal: any exception returns the source path
    unchanged with both flags False.
    """
    log = log_cb or (lambda _msg: None)
    if not src or not src.exists():
        return src, False, False, 0.0, 0.0
    try:
        import numpy as np
    except Exception as exc:
        log(f"[ReplayAudio] mic preprocess: numpy unavailable ({exc}); skipping.")
        return src, False, False, 0.0, 0.0

    try:
        with wave.open(str(src), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except Exception as exc:
        log(f"[ReplayAudio] mic preprocess: WAV read failed ({exc}); using raw file.")
        return src, False, False, 0.0, 0.0

    if sampwidth != 2 or n_frames == 0 or not raw:
        # Non-int16 or empty WAV -- our writer always emits int16 mono,
        # so this only fires if something else replaced the file.
        log(
            f"[ReplayAudio] mic preprocess: skipping (sampwidth={sampwidth}, "
            f"frames={n_frames}); using raw file."
        )
        return src, False, False, 0.0, 0.0

    try:
        ints = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            ints = ints.reshape(-1, channels).mean(axis=1).astype(np.int16)
        floats = ints.astype(np.float32) / 32767.0
        peak_before = float(np.abs(floats).max()) if floats.size else 0.0
    except Exception as exc:
        log(f"[ReplayAudio] mic preprocess: numpy convert failed ({exc}); using raw file.")
        return src, False, False, 0.0, 0.0

    # ---------- envelope-followed noise gate ---------------------------
    gate_applied = False
    if peak_before > MIC_PREPROCESS_GATE_FLOOR * 1.5:
        try:
            abs_sig = np.abs(floats)
            # Smooth envelope: 30 ms attack + 30 ms release. At 44.1kHz
            # that's ~1300 samples; we approximate with a one-pole IIR
            # so the cost is O(N). alpha picked from
            #   alpha = exp(-1 / (tau * fs)) with tau=0.03s, fs=44100
            #   -> alpha ~= 0.9992. That's too slow; use 0.99 for
            # ~10 ms which sounds tight but not clicky.
            alpha = 0.99
            env = np.empty_like(abs_sig)
            running = 0.0
            for i, v in enumerate(abs_sig):
                running = alpha * running + (1.0 - alpha) * v
                env[i] = running
            # Build a per-sample gain mask: full-scale (1.0) where the
            # envelope is above the floor, attenuated where it is below.
            # Smooth the transition with a soft knee so a 0.012->0.013
            # boundary doesn't oscillate.
            knee_low = MIC_PREPROCESS_GATE_FLOOR
            knee_high = MIC_PREPROCESS_GATE_FLOOR * 2.0
            atten = MIC_PREPROCESS_GATE_ATTEN
            gain = np.ones_like(env, dtype=np.float32)
            below = env < knee_low
            soft = (env >= knee_low) & (env < knee_high)
            gain[below] = atten
            # Linear interpolation across the knee.
            t = (env[soft] - knee_low) / max(1e-9, (knee_high - knee_low))
            gain[soft] = atten + (1.0 - atten) * t.astype(np.float32)
            floats = floats * gain
            gate_applied = True
        except Exception as exc:
            log(f"[ReplayAudio] mic preprocess: gate failed ({exc}); skipping gate.")

    # ---------- normalization ------------------------------------------
    norm_applied = False
    try:
        post_peak = float(np.abs(floats).max()) if floats.size else 0.0
    except Exception:
        post_peak = peak_before
    if MIC_PREPROCESS_NORM_MIN_PEAK <= post_peak < MIC_PREPROCESS_NORM_MAX_PEAK:
        try:
            gain = MIC_PREPROCESS_NORM_TARGET / max(post_peak, 1e-9)
            floats = floats * gain
            norm_applied = True
        except Exception as exc:
            log(f"[ReplayAudio] mic preprocess: normalize failed ({exc}); skipping norm.")

    if not (gate_applied or norm_applied):
        log(
            f"[ReplayAudio] mic preprocess: nothing to do "
            f"(peak={peak_before:.3f}); using raw WAV."
        )
        return src, False, False, peak_before, peak_before

    try:
        clipped = np.clip(floats, -1.0, 1.0)
        peak_after = float(np.abs(clipped).max()) if clipped.size else 0.0
        out_path = src.with_name(f"{src.stem}_processed.wav")
        with wave.open(str(out_path), "wb") as wf_out:
            wf_out.setnchannels(1)
            wf_out.setsampwidth(2)
            wf_out.setframerate(framerate)
            wf_out.writeframes((clipped * 32767.0).astype(np.int16).tobytes())
        log(
            f"[ReplayAudio] mic preprocess: gate={'on' if gate_applied else 'off'}, "
            f"normalize={'on' if norm_applied else 'off'}; peak_before={peak_before:.3f}, "
            f"peak_after={peak_after:.3f} -> {out_path.name}"
        )
        return out_path, gate_applied, norm_applied, peak_before, peak_after
    except Exception as exc:
        log(f"[ReplayAudio] mic preprocess: write failed ({exc}); using raw WAV.")
        return src, False, False, peak_before, peak_before


# Pass U-D: device names that should NEVER be auto-picked even if
# sounddevice surfaces them as inputs. These are either software
# routers ("Microsoft Sound Mapper"), system-level loopback that
# normally captures only what's playing ("Stereo Mix" sometimes
# misbehaves and reports zero level), explicitly bad ("PC Speaker"),
# or low-quality phone-call profiles ("Bluetooth Hands-Free"). The
# user can still pick any of these explicitly via settings -- the
# auto-pick path is the only one that filters them out.
MIC_AUTOPICK_BLOCKLIST = (
    "microsoft sound mapper",
    "primary sound capture driver",
    "stereo mix",
    "pc speaker",
    "bluetooth hands-free",
    "bluetooth hands free",
    "bluetooth hf",
    "what u hear",  # SoundBlaster equivalent of Stereo Mix
    "wave out mix",
)


def pick_best_mic_device(log_cb: Optional[Callable[[str], None]] = None) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(sd_device_index, name)`` for the best mic candidate.

    Pass U-D selection rule:
      1. Skip ``MIC_AUTOPICK_BLOCKLIST`` matches.
      2. Skip devices with no input channels.
      3. Score: WASAPI host API > DirectSound > MME; "microphone" in
         name nudges the score up; default-input device gets a small
         bonus so unsurprising picks win ties.
      4. Return the highest-scoring candidate (or ``(None, None)`` when
         nothing remains, in which case the caller should let
         sounddevice pick the OS default).
    """
    log = log_cb or (lambda _msg: None)
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        log(f"[ReplayAudio] mic auto-pick: sounddevice unavailable ({exc}); using OS default.")
        return None, None
    try:
        devs = sd.query_devices()
        host_apis = sd.query_hostapis()
        try:
            default_in_idx = sd.default.device[0]  # type: ignore[index]
        except Exception:
            default_in_idx = -1
    except Exception as exc:
        log(f"[ReplayAudio] mic auto-pick: query_devices failed ({exc}); using OS default.")
        return None, None

    candidates: list[Tuple[int, int, str]] = []
    for idx, d in enumerate(devs):
        try:
            ch = int(d.get("max_input_channels", 0) or 0)
        except Exception:
            ch = 0
        if ch <= 0:
            continue
        name = str(d.get("name") or "").strip()
        name_low = name.lower()
        if any(bad in name_low for bad in MIC_AUTOPICK_BLOCKLIST):
            continue
        host_idx = d.get("hostapi")
        host_name = ""
        try:
            if isinstance(host_idx, int) and 0 <= host_idx < len(host_apis):
                host_name = str(host_apis[host_idx].get("name") or "").lower()
        except Exception:
            host_name = ""
        score = 0
        if "wasapi" in host_name:
            score += 10
        elif "directsound" in host_name:
            score += 5
        elif "mme" in host_name:
            score += 3
        if "microphone" in name_low:
            score += 2
        if idx == default_in_idx:
            score += 1
        candidates.append((score, idx, name))

    if not candidates:
        log("[ReplayAudio] mic auto-pick: no acceptable input device; using OS default.")
        return None, None
    # Stable sort, highest score first.
    candidates.sort(key=lambda t: (-t[0], t[1]))
    score, idx, name = candidates[0]
    log(f"[ReplayAudio] mic auto-pick: chose {name!r} (sd index {idx}, score={score}).")
    return idx, name


def list_alt_mic_devices(skip_index: Optional[int], log_cb: Optional[Callable[[str], None]] = None) -> List[Tuple[int, str]]:
    """Return ``(idx, name)`` pairs for fallback retry candidates.

    Pass U-A: when the first chosen mic produces a silent WAV, the
    capture loop walks this list looking for one that produces real
    samples. ``skip_index`` is the device that just failed; everything
    else acceptable is included in score order.
    """
    log = log_cb or (lambda _msg: None)
    try:
        import sounddevice as sd  # type: ignore
    except Exception:
        return []
    try:
        devs = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception:
        return []

    out: list[Tuple[int, int, str]] = []
    for idx, d in enumerate(devs):
        if idx == skip_index:
            continue
        try:
            ch = int(d.get("max_input_channels", 0) or 0)
        except Exception:
            ch = 0
        if ch <= 0:
            continue
        name = str(d.get("name") or "").strip()
        name_low = name.lower()
        if any(bad in name_low for bad in MIC_AUTOPICK_BLOCKLIST):
            continue
        host_idx = d.get("hostapi")
        host_name = ""
        try:
            if isinstance(host_idx, int) and 0 <= host_idx < len(host_apis):
                host_name = str(host_apis[host_idx].get("name") or "").lower()
        except Exception:
            host_name = ""
        score = 0
        if "wasapi" in host_name:
            score += 10
        elif "directsound" in host_name:
            score += 5
        elif "mme" in host_name:
            score += 3
        if "microphone" in name_low:
            score += 2
        out.append((score, idx, name))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [(idx, name) for _score, idx, name in out]


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
    # Pass U-A: a WAV whose peak sample amplitude is below this fraction
    # of full scale is treated as silence and triggers a retry on the
    # next-best input device. 0.005 ~ -46 dBFS, well below normal speech
    # levels but above the noise floor of most digital mics on idle.
    SILENCE_PEAK_THRESHOLD = 0.005

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
        # Pass U-A: track the device sd index actually used by the
        # capture loop so snapshot/retry can pick a different one.
        self._mic_device_used: Optional[int] = None
        self._mic_device_name: Optional[str] = None
        # Pass U-A: peak amplitude observed during the last capture run,
        # in [0,1]. Used by the silent-WAV detection in stop().
        self._mic_peak: float = 0.0
        self._desk_peak: float = 0.0
        # Pass U-B: remember what start() asked for so snapshot() can
        # restart the same shape without the GUI re-passing the flags.
        self._mic_wanted: bool = False
        self._desk_wanted: bool = False
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
    ) -> Tuple[int, float]:
        """Capture stream loop. Returns ``(frame_count, peak)``.

        ``peak`` is the maximum |sample| observed in [0, 1.0]. Used by
        the post-stop health check so we can flag a silent WAV.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:
            self.log(f"[ReplayAudio] {kind}: sounddevice/numpy unavailable ({exc}); skipping.")
            return 0, 0.0

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
                # Pass U-C: explicit desktop-unavailable summary so users
                # don't have to grep for the WASAPI line. Keeps mic mux
                # entirely independent of desktop capture state.
                self.log(
                    "[ReplayAudio] desktop audio: not available "
                    "(WASAPI loopback unsupported in current sounddevice; "
                    "VB-Cable/Stereo Mix not detected). Mic-only mux will "
                    "still produce audio in the clip."
                )
                return 0, 0.0
            try:
                extra = settings_cls(loopback=True)  # type: ignore[call-arg]
            except Exception as exc:
                self.log(
                    f"[ReplayAudio] {kind}: WASAPI loopback init failed ({exc}); "
                    "skipping desktop capture."
                )
                return 0, 0.0

        wf = self._open_wav(path)
        if wf is None:
            return 0, 0.0

        frames_written = 0
        peak = 0.0
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
            return 0, 0.0

        self.log(f"[ReplayAudio] {kind}: capture started -> {path.name} (sd device={device!r}).")
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
                        # Pass U-A: track running peak so the post-stop
                        # health log can tell silence from real signal.
                        try:
                            block_peak = float(np.abs(clipped).max())
                            if block_peak > peak:
                                peak = block_peak
                        except Exception:
                            pass
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
            try:
                size_bytes = path.stat().st_size if path.exists() else 0
            except Exception:
                size_bytes = 0
            # Pass U-A: WAV health log. One line, all the requested
            # fields: path, duration, sample rate, peak, file size.
            self.log(
                f"[ReplayAudio] {kind}: WAV health -> path={path.name}, "
                f"duration={secs:.2f}s, samplerate={self.SAMPLE_RATE}, "
                f"peak={peak:.3f}, size={size_bytes} bytes."
            )
            if peak < self.SILENCE_PEAK_THRESHOLD:
                self.log(
                    f"[ReplayAudio] {kind}: WAV looks SILENT (peak={peak:.3f} "
                    f"< {self.SILENCE_PEAK_THRESHOLD}). Selected device "
                    f"{device!r} produced no signal. Caller may retry on a "
                    "different input device."
                )
        return frames_written, peak

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
        # Reset per-run health counters before threads kick in.
        self._mic_frames_written = 0
        self._desk_frames_written = 0
        self._mic_peak = 0.0
        self._desk_peak = 0.0
        # Pass U-B: remember the start() shape for snapshot() to mirror.
        self._mic_wanted = bool(mic_wanted)
        self._desk_wanted = bool(desktop_wanted)
        ts = _ts_with_micros()

        # Pass U-D: pick the best mic device up front when one is
        # wanted. The capture loop receives an explicit sd index so
        # PortAudio cannot fall back to a noise-floor router like
        # "Microsoft Sound Mapper" or a flat "Stereo Mix" input. If the
        # auto-pick returns None we let sounddevice resolve the OS
        # default (matches pre-Pass U behavior).
        mic_device_idx: Optional[int] = None
        mic_device_name: Optional[str] = None
        if mic_wanted:
            mic_device_idx, mic_device_name = pick_best_mic_device(self.log)
            if mic_device_idx is not None:
                self.log(
                    f"[ReplayAudio] mic selected: {mic_device_name!r} "
                    f"(sd index {mic_device_idx})."
                )
            else:
                self.log("[ReplayAudio] mic selected: OS default (no scored candidates).")
        self._mic_device_used = mic_device_idx
        self._mic_device_name = mic_device_name

        if mic_wanted:
            self._mic_path = self.buffer_dir / f"audio_mic_{ts}.wav"
            self._mic_thread = threading.Thread(
                target=self._capture_loop_runner,
                args=("mic", self._mic_path, False, mic_device_idx),
                daemon=True,
                name="replay-fallback-mic",
            )
            self._mic_thread.start()

        if desktop_wanted:
            self._desk_path = self.buffer_dir / f"audio_desk_{ts}.wav"
            self._desk_thread = threading.Thread(
                target=self._capture_loop_runner,
                args=("desktop", self._desk_path, True, None),
                daemon=True,
                name="replay-fallback-desktop",
            )
            self._desk_thread.start()

        self.log(
            f"[ReplayAudio] Fallback recorder armed (mic={'yes' if mic_wanted else 'no'}, "
            f"desktop={'yes' if desktop_wanted else 'no'})."
        )

    def _capture_loop_runner(
        self,
        kind: str,
        path: Path,
        wasapi_loopback: bool,
        device: Optional[int] = None,
    ) -> None:
        try:
            n, peak = self._capture_loop(kind, path, wasapi_loopback, device=device)
        except Exception as exc:
            self.log(f"[ReplayAudio] {kind}: unexpected error ({exc}); stream dropped.")
            n, peak = 0, 0.0
        # Pass U-A: silent-WAV retry. If the chosen device produced
        # nothing (or produced full-length silence), walk the alt list
        # and try the next device. We loop once: a noisy room with
        # several muted mics shouldn't lock the loop here forever.
        if (
            kind == "mic"
            and not self._stop_evt.is_set()
            and (n == 0 or peak < self.SILENCE_PEAK_THRESHOLD)
        ):
            alt_list = list_alt_mic_devices(skip_index=device, log_cb=self.log)
            for alt_idx, alt_name in alt_list:
                if self._stop_evt.is_set():
                    break
                self.log(
                    f"[ReplayAudio] mic: retrying on alt device {alt_name!r} "
                    f"(sd index {alt_idx})."
                )
                self._mic_device_used = alt_idx
                self._mic_device_name = alt_name
                # Each retry writes to a fresh WAV path so the prior
                # silent file (if any) is cleanly replaced. The capture
                # loop already removes empty WAVs, but a silent-but-
                # non-empty WAV would still occupy that path otherwise.
                ts = _ts_with_micros()
                retry_path = self.buffer_dir / f"audio_mic_{ts}.wav"
                self._mic_path = retry_path
                try:
                    n, peak = self._capture_loop("mic", retry_path, False, device=alt_idx)
                except Exception as exc:
                    self.log(f"[ReplayAudio] mic retry on {alt_name!r} crashed: {exc}")
                    n, peak = 0, 0.0
                if n > 0 and peak >= self.SILENCE_PEAK_THRESHOLD:
                    break
        if kind == "mic":
            self._mic_frames_written = n
            self._mic_peak = peak
        else:
            self._desk_frames_written = n
            self._desk_peak = peak

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

    def snapshot(self) -> FallbackPaths:
        """Pass U-B: rotate the open WAV files and return what was just
        finalized.

        ``ReplayBufferRecorder.export_last`` calls this WHILE the buffer
        is still recording. Internally it's stop() (which finalizes the
        WAV headers so ffmpeg can demux them) followed by a same-shape
        start(). There's a small audio gap (<300 ms typically) while
        the input streams reopen -- acceptable for an on-demand "clip
        that" trigger and far better than the previous behavior of
        producing a video-only clip.

        Idempotent: calling snapshot() when the recorder is not started
        returns whatever the previous run left behind (which may be
        empty).
        """
        if not self._started:
            return self.paths()
        mic_was_wanted = self._mic_wanted
        desk_was_wanted = self._desk_wanted
        self.log(
            f"[ReplayAudio] snapshot: rotating WAV files for export "
            f"(mic_wanted={'yes' if mic_was_wanted else 'no'}, "
            f"desktop_wanted={'yes' if desk_was_wanted else 'no'})."
        )
        self.stop()
        snapped = self.paths()
        # Reset path state so start() opens fresh files for the next
        # capture window. We deliberately do NOT touch the device cache
        # (_mic_device_used / _mic_device_name) so the resumed capture
        # picks the same proven-good device.
        self._mic_thread = None
        self._desk_thread = None
        self._mic_path = None
        self._desk_path = None
        self._mic_frames_written = 0
        self._desk_frames_written = 0
        self._mic_peak = 0.0
        self._desk_peak = 0.0
        # Restart for continuous capture. start() will set _started back
        # to True and arm new threads.
        if mic_was_wanted or desk_was_wanted:
            self.start(mic_wanted=mic_was_wanted, desktop_wanted=desk_was_wanted)
        if snapped.mic_wav or snapped.desktop_wav:
            self.log(
                f"[ReplayAudio] snapshot finalized: "
                f"mic={snapped.mic_wav.name if snapped.mic_wav else 'none'}, "
                f"desktop={snapped.desktop_wav.name if snapped.desktop_wav else 'none'}."
            )
        else:
            self.log("[ReplayAudio] snapshot finalized: no usable WAVs to mux.")
        return snapped

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
