"""
Replay buffer recorder (GeForce Experience style) using FFmpeg rolling segments.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from services.settings_store import load_settings

DEFAULT_WASAPI_SYSTEM = "__DEFAULT_WASAPI__"


def _find_ffmpeg() -> str:
    candidates = [
        os.environ.get("FFMPEG_PATH"),
        str(Path("ffmpeg-7.1.1") / "bin" / "ffmpeg.exe"),
        str(Path("ffmpeg") / "bin" / "ffmpeg.exe"),
        str(Path("tools") / "ffmpeg.exe"),
        str(Path("bin") / "ffmpeg.exe"),
        "ffmpeg",
    ]
    for c in candidates:
        if not c:
            continue
        if c == "ffmpeg":
            return c
        if Path(c).exists():
            return c
    return "ffmpeg"


def _pick_auto_system_audio(log_cb=None) -> Optional[str]:
    log = log_cb or (lambda _msg: None)
    try:
        from services.audio_devices import pick_best_system_audio_choice, DEFAULT_WASAPI_SYSTEM as AUDIO_DEFAULT
        choice = pick_best_system_audio_choice()
        if choice == AUDIO_DEFAULT:
            log("[Replay] No concrete loopback capture device detected; starting without desktop audio.")
            return None
        return choice
    except Exception as e:
        log(f"[Replay] Auto-detect system audio failed: {e}")
        return None


@dataclass
class CaptureDevices:
    system_audio: Optional[str] = None
    mic: Optional[str] = None


class ReplayBufferRecorder:
    def __init__(
        self,
        devices: Optional[CaptureDevices] = None,
        log_cb=None,
        stt_pause_cb: Optional[Callable[[], None]] = None,
        stt_resume_cb: Optional[Callable[[], None]] = None,
    ):
        self.devices = devices or CaptureDevices()
        self.log = log_cb or (lambda msg: None)
        self.proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stop_drain = threading.Event()
        # Pass Q: Python-side audio fallback. Used when ffmpeg/dshow
        # cannot open Stereo Mix or the configured mic. Lives next to
        # the segment buffer; export merges any captured WAV files in.
        # None until a screen-only rung fires; cleaned up on stop().
        self._audio_fallback: Optional[Any] = None
        # Last fallback paths captured (set when stop() finalizes the
        # WAVs). export_last() reads this to know whether to mux audio.
        self._last_fallback_paths: Optional[Any] = None
        # Pass R-C: STT/replay mic coordination. When the AudioFallback-
        # Recorder claims the same default mic device the STT listener
        # is already using, sounddevice on Windows raises a
        # PaErrorCode -9999 (device busy / unanticipated host error) on
        # whichever opens second. To avoid that, the recorder calls
        # stt_pause_cb() right before mic-fallback start and
        # stt_resume_cb() once the fallback is finalized in stop().
        # Both callbacks are optional and never raise -- failures are
        # swallowed so a missing or buggy callback can't kill replay.
        self._stt_pause_cb = stt_pause_cb
        self._stt_resume_cb = stt_resume_cb
        self._stt_was_paused_for_fallback = False

    def is_running(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def _drain_output(self, proc: subprocess.Popen, captured: Optional[list] = None) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, ""):
                if self._stop_drain.is_set():
                    break
                line = (raw_line or "").strip()
                if not line:
                    continue
                # Mirror every line into the shared capture list so _launch
                # can inspect it for audio-failure markers even though this
                # drain thread is the one actually consuming the pipe.
                # list.append is atomic under CPython's GIL, so no lock
                # needed here.
                if captured is not None:
                    captured.append(line)
                low = line.lower()
                if any(k in low for k in ("error", "failed", "warning", "invalid")):
                    self.log(f"[Replay][ffmpeg] {line[:500]}")
        except Exception:
            return

    @staticmethod
    def _normalize_for_match(name: str) -> str:
        """Strip trademark glyphs + accents and collapse whitespace.

        Observed in the wild on the user's box: the picker surfaced
        ``Microphone Array (Intel(R) Smart Sound Technology for Digital
        Microphones)`` but FFmpeg's dshow enumeration rendered the
        same device without the ``(R)`` glyph and with different
        whitespace, so the exact/containment matches all missed and
        mic input was silently skipped. Normalize both sides through
        this helper before the containment rung matches.
        """
        try:
            import unicodedata
            s = unicodedata.normalize("NFKD", name)
            s = "".join(ch for ch in s if not unicodedata.combining(ch))
        except Exception:
            s = name
        # Known marketing glyphs that sometimes survive NFKD.
        for glyph in ("\u00ae", "\u2122", "\u00a9"):  # (R), (TM), (C)
            s = s.replace(glyph, "")
        # Parenthesised ASCII approximations that ffmpeg emits in place
        # of the Unicode glyphs above.
        for token in ("(R)", "(r)", "(TM)", "(tm)", "(C)", "(c)"):
            s = s.replace(token, "")
        return " ".join(s.split()).lower()

    def _resolve_dshow_audio_name(self, requested: str, available: List[str]) -> Optional[str]:
        """Map the user's picker value to an actual dshow device name.

        FFmpeg's dshow demuxer requires the device name to match the
        enumeration from ``ffmpeg -list_devices`` EXACTLY (including
        trailing qualifiers like ``(Realtek(R) Audio)``). Windows Sound
        panel shows truncated or friendlier names, so the name the user
        picked often doesn't match. We try (1) exact match, (2) exact
        match stripped of ``audio=`` prefix, (3) case-insensitive
        prefix / contains match, (4) glyph-normalized match to catch
        ``Intel(R)`` vs ``Intel\u00ae`` drift. Returns None when nothing
        plausible lines up -- callers should log and skip that input
        rather than hand a bad name to ffmpeg.
        """
        if not requested or not available:
            return None
        needle = requested.strip()
        if needle.startswith("audio="):
            needle = needle[len("audio="):]
        for name in available:
            if name == needle:
                return name
        low = needle.lower()
        for name in available:
            if name.lower() == low:
                return name
        for name in available:
            nlow = name.lower()
            if nlow.startswith(low) or low.startswith(nlow):
                return name
        for name in available:
            if low in name.lower() or name.lower() in low:
                return name
        # Final rung: trademark-glyph-insensitive match. This catches
        # the "Intel(R)" vs "Intel\u00ae" drift we observed in real
        # Windows runtime logs where every previous rung missed.
        needle_norm = self._normalize_for_match(needle)
        if needle_norm:
            for name in available:
                if self._normalize_for_match(name) == needle_norm:
                    return name
            for name in available:
                name_norm = self._normalize_for_match(name)
                if needle_norm and name_norm and (needle_norm in name_norm or name_norm in needle_norm):
                    return name
        return None

    def _audio_input_args(self, system_audio: Optional[str], mic: Optional[str]) -> tuple[list[str], int, Optional[str]]:
        args: list[str] = []
        audio_inputs = 0
        filter_complex = None

        sys_choice = (system_audio or "").strip()
        mic_choice = (mic or "").strip()

        if not sys_choice or sys_choice == DEFAULT_WASAPI_SYSTEM:
            resolved = _pick_auto_system_audio(self.log)
            if resolved:
                self.log(f"[Replay] Auto-picked desktop audio device: {resolved!r}")
            sys_choice = resolved or ""

        # Enumerate what dshow actually sees right now so we can refuse
        # to hand ffmpeg a name that won't resolve. This catches the
        # "Could not find audio only device with name [Stereo Mix ...]"
        # regression that happens when the Windows Sound panel hides or
        # disables loopback devices between launches.
        #
        # enum_ok is True only when the enumeration call returned
        # cleanly (even if empty). When it's True but `available` is
        # empty, we know with confidence that FFmpeg cannot open ANY
        # dshow audio input on this machine right now -- so we refuse
        # to configure either sys or mic. Previously the code fell
        # through and handed ffmpeg the unvalidated name anyway, which
        # is the exact regression the latest Windows runtime logs show
        # ("FFmpeg enumerated zero dshow audio devices" followed by a
        # "Could not find audio only device with name [...]" on the
        # picked Stereo Mix name).
        available: List[str] = []
        enum_ok = False
        try:
            from services.audio_devices import list_dshow_audio_devices
            available = list_dshow_audio_devices()
            enum_ok = True
            if available:
                self.log(f"[Replay] Available dshow audio devices: {available!r}")
            else:
                self.log(
                    "[Replay] FFmpeg enumerated zero dshow audio devices. "
                    "Desktop-audio and mic capture require a dshow-visible source. "
                    "Fixes: (a) Windows Sound panel -> Recording tab -> right-click "
                    "empty space -> Show Disabled Devices -> enable 'Stereo Mix'; "
                    "(b) install a Virtual Audio Capturer DirectShow filter "
                    "(https://github.com/rdp/screen-capture-recorder-to-video-windows-free); "
                    "(c) Settings -> Privacy & security -> Microphone -> allow apps "
                    "to access your microphone."
                )
        except Exception as exc:
            self.log(f"[Replay] dshow enumeration failed ({exc}); proceeding without pre-validation.")

        # Pass P: log sounddevice inputs alongside dshow enumeration so
        # the operator can see at a glance which mics/loopback Windows
        # itself is happy to expose vs. what dshow sees. When dshow is
        # empty but sounddevice sees 4 inputs, the fix is almost always
        # "enable Stereo Mix" or "install VAC", not "plug in a mic".
        try:
            from services.audio_devices import list_sounddevice_devices
            sd_info = list_sounddevice_devices()
            sd_inputs = [d.get("name") for d in sd_info.get("inputs", [])]
            if sd_inputs:
                self.log(f"[Replay] sounddevice inputs (for cross-debug): {sd_inputs!r}")
        except Exception:
            pass

        # Pass P: stop hard-refusing when dshow enum returned clean AND
        # empty. The previous "return [], 0, None" short-circuit made
        # the rung silently fall to screen-only with a misleading
        # "(no dshow audio devices)" label even when the user had
        # explicitly picked a device that ffmpeg could in fact open by
        # name. Instead, let the picked names flow through; if ffmpeg
        # cannot open them the four-rung ladder below will cascade
        # normally via the audio_failure_markers path, and the user
        # ends up at screen-only (all audio rungs failed) with ffmpeg's
        # actual error in the log, which is actionable.

        if sys_choice:
            resolved_sys = self._resolve_dshow_audio_name(sys_choice, available) if available else sys_choice
            if available and resolved_sys is None:
                self.log(
                    f"[Replay] Desktop audio device {sys_choice!r} not found among dshow devices; "
                    f"skipping (likely Stereo Mix is disabled in Windows Sound panel)."
                )
            else:
                use_name = resolved_sys or sys_choice
                if resolved_sys and resolved_sys != sys_choice:
                    self.log(f"[Replay] Mapped desktop audio {sys_choice!r} -> {resolved_sys!r}.")
                sys_spec = f"audio={use_name}"
                self.log(f"[Replay] Desktop audio input spec: {sys_spec!r}")
                args += ["-thread_queue_size", "512", "-f", "dshow", "-i", sys_spec]
                audio_inputs += 1
        else:
            self.log("[Replay] No desktop audio device selected; clip will omit system audio.")

        if mic_choice:
            resolved_mic = self._resolve_dshow_audio_name(mic_choice, available) if available else mic_choice
            if available and resolved_mic is None:
                self.log(
                    f"[Replay] Microphone {mic_choice!r} not found among dshow devices; skipping."
                )
            else:
                use_mic = resolved_mic or mic_choice
                if resolved_mic and resolved_mic != mic_choice:
                    self.log(f"[Replay] Mapped mic {mic_choice!r} -> {resolved_mic!r}.")
                mic_spec = f"audio={use_mic}"
                self.log(f"[Replay] Microphone input spec: {mic_spec!r}")
                args += ["-thread_queue_size", "512", "-f", "dshow", "-i", mic_spec]
                audio_inputs += 1
        else:
            self.log("[Replay] No microphone device selected; clip will omit mic audio.")

        if audio_inputs >= 2:
            filter_complex = "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
        self.log(f"[Replay] Audio inputs configured: {audio_inputs}.")
        return args, audio_inputs, filter_complex

    # Substrings used to recognize an audio-capture failure in captured
    # ffmpeg output. When any of these appears and an audio input was
    # configured, start() retries once with the video-only pipeline --
    # the "prefer screen-only fallback where supported" directive from
    # the project brief (priority #4).
    _AUDIO_FAILURE_MARKERS = (
        "error opening input file audio=",
        "could not find audio",
        "no such audio device",
        "audio=stereo mix",
        "i/o error",
    )

    # Mean luminance threshold below which a captured frame is considered
    # "black" (desktop composition not ready yet). Empirically a truly
    # dark desktop scene still runs ~8-15 in 8-bit luma terms; anything
    # below 3 is a blank framebuffer.
    _BLACK_FRAME_LUMA_THRESHOLD = 3.0

    def _probe_desktop_frame_luma(self) -> Optional[float]:
        """Grab a single cheap screenshot via mss and return mean luma.

        Returns None if mss/PIL isn't available or the grab failed. We
        do NOT raise -- callers treat None as "unknown, proceed." The
        goal is to catch the specific regression where gdigrab starts
        against a not-yet-composed desktop and produces a black-cursor
        clip, not to be the arbiter of desktop health.
        """
        try:
            import mss  # type: ignore
            import mss.tools  # type: ignore
        except Exception:
            return None
        try:
            with mss.mss() as sct:
                if not sct.monitors or len(sct.monitors) < 2:
                    return None
                frame = sct.grab(sct.monitors[1])
        except Exception:
            return None
        try:
            raw = bytes(frame.rgb)
            if not raw:
                return None
            # Sample every 256th byte to keep the probe cheap -- a 4K
            # grab is ~24MB and we only need a coarse brightness check.
            stride = 256
            total = 0
            count = 0
            for i in range(0, len(raw), stride):
                total += raw[i]
                count += 1
            if count == 0:
                return None
            return total / count
        except Exception:
            return None

    def _detect_desktop_size(self) -> Optional[tuple[int, int]]:
        """Return (w, h) of the primary monitor via mss, or None on failure.

        Pass P regression fix: the user reported black video with the
        Pass J/K pipeline still in place. Root cause on Win11 is that
        settings.width/height (default 1920x1080) were being passed to
        gdigrab verbatim. When the live desktop is a different size
        (scaled DPI, 2560x1440, 4K, rotated secondary, etc.) gdigrab
        silently captures a region that partly or entirely falls off
        the compositor surface -- the resulting clip is black or black
        with only the cursor.

        We probe mss (already a dependency for the black-frame luma
        guard) and, if the detected desktop is smaller than the
        configured capture size, we shrink the capture size to match.
        We never enlarge: a user who deliberately picked 1920x1080 on a
        4K monitor presumably wants the downscaled view.
        """
        try:
            import mss  # type: ignore
        except Exception:
            return None
        try:
            with mss.mss() as sct:
                if not sct.monitors or len(sct.monitors) < 2:
                    return None
                mon = sct.monitors[1]
                w = int(mon.get("width") or 0)
                h = int(mon.get("height") or 0)
                if w <= 0 or h <= 0:
                    return None
                return w, h
        except Exception:
            return None

    def _maybe_arm_audio_fallback(
        self,
        rung: str,
        has_sys: bool,
        has_mic: bool,
        buffer_dir: Path,
    ) -> None:
        """When the ffmpeg ladder lands on screen-only, start the Python
        sounddevice fallback recorder for mic and (best-effort) desktop
        loopback. Idempotent. Never raises.

        Pass Q's whole point: ffmpeg/dshow on Win11 without VAC can't
        see Stereo Mix even when sounddevice can, so we capture audio
        through Python directly and mux it in at export time. The
        sounddevice + WASAPI loopback combination works on Win11
        out-of-the-box for the default playback device.
        """
        if not rung.lower().startswith("screen-only"):
            return
        if self._audio_fallback is not None:
            return  # Already armed (e.g. retry).
        # Arm only when the user actually wanted audio. A silent clip
        # by user choice should stay silent.
        if not (has_sys or has_mic):
            self.log(
                "[Replay] Screen-only rung; user didn't request audio so the "
                "fallback recorder stays disarmed."
            )
            return
        try:
            from services.audio_fallback_recorder import AudioFallbackRecorder, cleanup_old_fallback_wavs
        except Exception as exc:
            self.log(f"[Replay] Fallback recorder import failed ({exc}); silent clip.")
            return
        try:
            cleanup_old_fallback_wavs(buffer_dir)
        except Exception:
            pass
        self.log(
            "[Replay] Arming Python audio fallback (sounddevice). DirectShow "
            "couldn't open audio, but sounddevice / WASAPI loopback may still "
            "be available."
        )
        # Pass R-C: when the fallback is about to grab the mic device,
        # pause STT first so PortAudio doesn't return -9999 (device busy)
        # on whichever opens the device second. The pause callback is a
        # cheap flag flip on the listener side -- it releases the input
        # stream but keeps the Vosk model loaded for fast resume.
        if has_mic and self._stt_pause_cb is not None and not self._stt_was_paused_for_fallback:
            try:
                self._stt_pause_cb()
                self._stt_was_paused_for_fallback = True
            except Exception as exc:
                self.log(f"[Replay] STT pause callback failed: {exc}; continuing.")
        rec = AudioFallbackRecorder(buffer_dir, log_cb=self.log)
        rec.start(mic_wanted=has_mic, desktop_wanted=has_sys)
        self._audio_fallback = rec

    def _spawn_health_check(self, buffer_dir: Path, segment_seconds: int) -> None:
        """Background one-shot verify that seg000.mp4 is actually being written.

        The four-rung ladder succeeds as soon as ffmpeg stays alive past
        the 2.0s grace, but ffmpeg can stay alive while still producing
        zero frames (e.g. gdigrab driver lock, clip off-screen, display
        driver returning empty surfaces). Without this probe the user
        only learns the recording was black by trying to play it back.

        Runs in a daemon thread. Fires exactly once. Never blocks
        start(). Never raises.
        """
        def _probe() -> None:
            try:
                wait_s = max(2.0, float(segment_seconds) * 1.5)
                time.sleep(wait_s)
                # seg000.mp4 is the first segment the segment muxer
                # writes after the first segment_seconds window closes.
                # We don't care which specific file appeared, just that
                # SOMETHING reasonably-sized did.
                biggest = 0
                found_any = False
                for p in buffer_dir.glob("seg*.mp4"):
                    try:
                        sz = p.stat().st_size
                    except Exception:
                        continue
                    found_any = True
                    if sz > biggest:
                        biggest = sz
                if not found_any:
                    self.log(
                        "[Replay] HEALTH: No segment files exist after "
                        f"{wait_s:.1f}s. FFmpeg appears alive but the segment "
                        "muxer has not produced output. Likely causes: "
                        "gdigrab cannot read the desktop surface (display "
                        "driver / UAC), or the output directory is not "
                        "writable."
                    )
                    return
                if biggest < 16 * 1024:
                    self.log(
                        "[Replay] HEALTH: First segment is tiny "
                        f"({biggest} bytes). FFmpeg is producing near-empty "
                        "frames -- very likely a black/off-screen capture. "
                        "Check display resolution vs. settings.width/height "
                        "and that the primary monitor is at (0,0)."
                    )
                    return
                self.log(
                    f"[Replay] HEALTH: First segment OK ({biggest} bytes)."
                )
            except Exception:
                # Best-effort probe; never take down the recorder.
                return

        t = threading.Thread(target=_probe, daemon=True, name="replay-health")
        t.start()

    def _wait_for_desktop_ready(self, attempts: int = 3, delay_sec: float = 1.2) -> bool:
        """Return True when a live desktop frame is probable, else False.

        Does not raise. Logs only on a transition so normal fast-path
        starts stay quiet. Non-blocking after at most ``attempts``
        cycles, so this can never deadlock replay startup.
        """
        for attempt in range(1, attempts + 1):
            luma = self._probe_desktop_frame_luma()
            if luma is None:
                # Probe not available -- skip silently, do not block.
                return True
            if luma >= self._BLACK_FRAME_LUMA_THRESHOLD:
                if attempt > 1:
                    self.log(
                        f"[Replay] Desktop looked black; recovered on attempt {attempt} "
                        f"(mean luma={luma:.1f})."
                    )
                return True
            self.log(
                f"[Replay] Desktop readiness probe {attempt}/{attempts}: black frame "
                f"(mean luma={luma:.1f})."
            )
            if attempt < attempts:
                time.sleep(delay_sec)
        self.log(
            "[Replay] Desktop still looked black after probes; proceeding anyway to avoid "
            "blocking replay start."
        )
        return False

    def _emit_device_diagnostics(self) -> None:
        """Log a single contiguous block describing every audio source.

        Pass V-D: bracketed by ``Device diagnostics start`` /
        ``Device diagnostics end`` so the operator can grep one block
        and see (a) what dshow sees, (b) what sounddevice sees, (c)
        which mic auto-pick chose, (d) which desktop strategy is
        available, and (e) which devices were rejected by the
        blocklist. Best-effort: every sub-call is wrapped because we
        do NOT want a missing dependency to derail replay startup.
        """
        self.log("[ReplayAudio] Device diagnostics start")

        # dshow audio devices
        try:
            from services.audio_devices import list_dshow_audio_devices
            ds = list_dshow_audio_devices()
            if ds:
                for n in ds:
                    self.log(f"[ReplayAudio]   dshow audio: {n}")
            else:
                self.log("[ReplayAudio]   dshow audio: (none enumerated)")
        except Exception as exc:
            self.log(f"[ReplayAudio]   dshow audio: enumeration failed ({exc})")

        # sounddevice inputs / outputs
        try:
            from services.audio_devices import list_sounddevice_devices
            sd_info = list_sounddevice_devices()
            for d in sd_info.get("inputs", []):
                tag = " *default" if d.get("default") else ""
                self.log(
                    f"[ReplayAudio]   sounddevice in [{d.get('index')}]: "
                    f"{d.get('name')}{tag}"
                )
            for d in sd_info.get("outputs", []):
                tag = " *default" if d.get("default") else ""
                self.log(
                    f"[ReplayAudio]   sounddevice out[{d.get('index')}]: "
                    f"{d.get('name')}{tag}"
                )
        except Exception as exc:
            self.log(f"[ReplayAudio]   sounddevice: enumeration failed ({exc})")

        # selected mic (auto-pick) + which devices the auto-pick rejected
        try:
            from services.audio_fallback_recorder import (
                pick_best_mic_device,
                MIC_AUTOPICK_BLOCKLIST,
            )
            picked = pick_best_mic_device(log_cb=lambda _m: None)
            if picked and picked[0] is not None:
                self.log(
                    f"[ReplayAudio]   selected mic (auto-pick): {picked[1]!r} "
                    f"(sd index {picked[0]})"
                )
            else:
                self.log(
                    "[ReplayAudio]   selected mic (auto-pick): OS default "
                    "(no scored candidates)"
                )
            # Walk sounddevice inputs again and flag anyone the
            # blocklist would have eliminated. Doesn't repeat the
            # WASAPI / score work -- just shows the operator which
            # surfaced names get skipped.
            rejected: list[str] = []
            try:
                from services.audio_devices import list_sounddevice_devices as _ls
                for d in _ls().get("inputs", []):
                    nlow = (d.get("name") or "").lower()
                    if any(b in nlow for b in MIC_AUTOPICK_BLOCKLIST):
                        rejected.append(str(d.get("name")))
            except Exception:
                rejected = []
            if rejected:
                self.log(
                    f"[ReplayAudio]   blocked from auto-pick: "
                    f"{', '.join(rejected)}"
                )
            else:
                self.log("[ReplayAudio]   blocked from auto-pick: (none)")
        except Exception as exc:
            self.log(f"[ReplayAudio]   mic auto-pick probe failed ({exc})")

        # desktop audio strategy
        try:
            from services.audio_devices import detect_desktop_audio_strategy
            strat = detect_desktop_audio_strategy()
            avail = strat.get("available") or []
            if avail:
                self.log(
                    f"[ReplayAudio]   desktop strategy: preferred="
                    f"{strat.get('preferred')!r}; available={avail}"
                )
            else:
                self.log(
                    f"[ReplayAudio]   desktop strategy: NONE available -- "
                    f"{strat.get('diagnostic_message')}"
                )
        except Exception as exc:
            self.log(f"[ReplayAudio]   desktop strategy probe failed ({exc})")

        self.log("[ReplayAudio] Device diagnostics end")

    def start(self) -> None:
        if self.is_running():
            self.log("[Replay] Buffer already running.")
            return

        # Pass Q: clear any stale fallback paths from a prior session so
        # export_last doesn't pick up an old WAV that doesn't line up
        # with the current segment buffer.
        self._last_fallback_paths = None

        # Pass V-D: emit one diagnostics block before any audio choices
        # are committed to ffmpeg. Lets the operator confirm the
        # selected mic + desktop path before reading any
        # rung-success/failure lines.
        try:
            self._emit_device_diagnostics()
        except Exception as exc:
            self.log(f"[ReplayAudio] device diagnostics raised ({exc}); continuing.")

        s = load_settings()
        buffer_dir = Path(s.buffer_dir).resolve()
        buffer_dir.mkdir(parents=True, exist_ok=True)

        for p in buffer_dir.glob("seg*.mp4"):
            try:
                p.unlink()
            except Exception:
                pass

        # Black-screen guard: before FFmpeg gets a chance to lock onto a
        # half-composed desktop (the regression behind clip_20260423_084641
        # -- black screen + cursor only), take a cheap mss screenshot and
        # check mean luma. If it's black-on-black, wait a bit and retry a
        # couple of times. This adds at most ~3.6s on a cold start and
        # zero when the desktop is already live. When the probe reports
        # the desktop is STILL black after every attempt, give the
        # compositor one final 2s grace before launching FFmpeg -- we'd
        # rather pay 2s than produce another black-cursor clip.
        if not self._wait_for_desktop_ready():
            self.log(
                "[Replay] Adding 2.0s grace before FFmpeg launch to give the "
                "desktop compositor one more chance after black-frame probe."
            )
            time.sleep(2.0)

        # Pass P black-video fix: detect the live desktop resolution and
        # shrink the gdigrab capture region to match when the configured
        # size is larger than the physical desktop. gdigrab silently
        # returns black frames when -video_size exceeds the compositor
        # surface, which is the exact regression the user reported.
        eff_w, eff_h = int(s.width), int(s.height)
        detected = self._detect_desktop_size()
        if detected is not None:
            det_w, det_h = detected
            self.log(
                f"[Replay] Desktop detected: {det_w}x{det_h}; "
                f"settings: {eff_w}x{eff_h}."
            )
            if eff_w > det_w or eff_h > det_h:
                old_w, old_h = eff_w, eff_h
                eff_w, eff_h = det_w, det_h
                self.log(
                    f"[Replay] Capture size {old_w}x{old_h} exceeds desktop "
                    f"{det_w}x{det_h}; shrinking to {eff_w}x{eff_h} to avoid "
                    "off-screen (black) capture."
                )
        else:
            self.log(
                "[Replay] Desktop size probe unavailable (mss missing or "
                f"failed); using settings size {eff_w}x{eff_h} as-is."
            )

        seg_count = max(1, int((s.replay_minutes * 60) / max(1, s.segment_seconds)))
        ffmpeg = _find_ffmpeg()
        out_pattern = str((buffer_dir / "seg%03d.mp4").resolve())

        def _build_args(with_audio: bool) -> tuple[list[str], int]:
            # -draw_mouse 0: on Windows with an elevated/UAC-protected
            # foreground window, gdigrab's cursor query hits
            # ACCESS_DENIED (error 5) and the capture degrades to a
            # black frame. We don't need the cursor burned into the
            # replay clip anyway, so disabling it makes the recorder
            # robust against that specific Windows regression.
            args = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtbufsize",
                "256M",
                "-thread_queue_size",
                "1024",
                "-f",
                "gdigrab",
                "-draw_mouse",
                "0",
                # Pass P: explicit (0,0) offset. gdigrab defaults to
                # (0,0) but some Windows driver paths were observed to
                # honor stale offsets from a prior capture when the
                # flag was omitted. Making it explicit also documents
                # that we always record the primary monitor region.
                "-offset_x",
                "0",
                "-offset_y",
                "0",
                "-framerate",
                str(s.fps),
                "-video_size",
                f"{eff_w}x{eff_h}",
                "-i",
                "desktop",
            ]
            if with_audio:
                audio_args, audio_inputs, filter_complex = self._audio_input_args(
                    self.devices.system_audio, self.devices.mic
                )
            else:
                audio_args, audio_inputs, filter_complex = [], 0, None
            args += audio_args

            map_args = ["-map", "0:v"]
            if audio_inputs == 1:
                map_args += ["-map", "1:a"]
            elif audio_inputs >= 2:
                map_args += ["-map", "[aout]"]

            if filter_complex:
                args += ["-filter_complex", filter_complex]
            args += map_args
            args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
            if audio_inputs:
                args += ["-c:a", "aac", "-b:a", "192k"]
            args += [
                "-f",
                "segment",
                "-segment_time",
                str(max(1, s.segment_seconds)),
                "-segment_wrap",
                str(seg_count),
                "-reset_timestamps",
                "1",
                out_pattern,
            ]
            return args, audio_inputs

        def _launch(args: list[str]) -> Optional[str]:
            """Launch ffmpeg and wait briefly. Return None on success, or the
            captured ffmpeg output on failure. Never raises."""
            self.log(f"[Replay] ffmpeg cmd: {' '.join(args)}")
            self._stop_drain.clear()
            try:
                self.proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                )
            except Exception as exc:
                self.proc = None
                return f"Popen failed: {exc}"
            # Shared list the drain thread mirrors each ffmpeg line into so
            # this function can inspect output after a fast failure. Without
            # this, proc.stdout.read() below races with the drain thread and
            # always returns "" -- which is how the screen-only fallback was
            # silently skipped on Windows when Stereo Mix was unavailable.
            captured: list[str] = []
            self._stdout_thread = threading.Thread(
                target=self._drain_output, args=(self.proc, captured), daemon=True
            )
            self._stdout_thread.start()
            # 2.0s grace: long enough for dshow audio-open errors and
            # gdigrab init failures to actually hit stderr. The previous
            # 0.8s wait let fast-failing audio pipelines look healthy
            # because the process hadn't yet exited when we checked.
            time.sleep(2.0)
            if self.is_running():
                return None
            # Process died fast. Give the drain thread a short grace period
            # to pull the last few lines out of the closing pipe, then use
            # whatever it captured. This is the authoritative source of the
            # failure output; proc.stdout.read() would race the drain.
            if self._stdout_thread is not None:
                self._stdout_thread.join(timeout=1.0)
            out = "\n".join(captured).strip()
            self.proc = None
            return out or "(no ffmpeg output captured)"

        self.log(
            f"[Replay] Starting buffer: {s.replay_minutes}m @ {s.fps}fps {eff_w}x{eff_h} seg={s.segment_seconds}s wrap={seg_count}"
        )

        # Four-rung fallback ladder. Each rung logs its attempt once;
        # the rung that succeeds emits a single summary line so
        # operators can grep `[Replay] Capture rung:` and immediately
        # know what the live process is actually recording. Rung order:
        #     full     : desktop audio + mic
        #     mic_only : mic only (drop desktop audio)
        #     desk_only: desktop audio only (drop mic)
        #     screen   : no audio
        has_sys = bool((self.devices.system_audio or "").strip()) or (
            (self.devices.system_audio or "") == DEFAULT_WASAPI_SYSTEM
        )
        has_mic = bool((self.devices.mic or "").strip())

        def _emit_rung(rung: str) -> None:
            self.log(f"[Replay] Capture rung: {rung}.")
            # 4D Lab telemetry: publish the final rung so the Lab tab
            # can show a "replay: <rung>" badge without needing a
            # dedicated polling thread. Best-effort; import locally so
            # a missing telemetry module can't break replay.
            try:
                from services.four_d_telemetry import telemetry as _four_d_t
                _four_d_t.emit("replay", "rung", rung=rung)
            except Exception:
                pass
            # Pass P: spawn a one-shot health probe so a "rung succeeded
            # but ffmpeg produces no frames" silent failure becomes a
            # visible log line instead of a mystery black clip at export.
            try:
                self._spawn_health_check(buffer_dir, max(1, int(s.segment_seconds)))
            except Exception:
                pass
            # Pass Q: when ffmpeg ended up at a screen-only rung but the
            # user actually picked audio devices, spin up the Python
            # sounddevice fallback so we can mux mic + WASAPI loopback
            # in at export. This is the path where ffmpeg/dshow can't
            # see Stereo Mix or the mic but sounddevice can. We only
            # arm it when the rung label starts with "screen-only" --
            # any rung that actually got dshow audio is fine and we
            # don't want a duplicated audio track.
            try:
                self._maybe_arm_audio_fallback(rung, has_sys, has_mic, buffer_dir)
            except Exception as _exc:
                self.log(f"[Replay] Audio fallback arm failed: {_exc}")

        # Rung 0 / full audio
        args, audio_inputs = _build_args(with_audio=True)
        failure_out = _launch(args)
        if failure_out is None:
            if audio_inputs >= 2:
                _emit_rung("full (desktop + mic)")
            elif audio_inputs == 1:
                # Only one audio source was configured -- still the
                # "full as configured" rung.
                _emit_rung("single-audio (configured)")
            elif has_sys or has_mic:
                # User picked audio devices but _audio_input_args
                # refused to configure any -- typically because dshow
                # enumeration returned zero devices. Label the rung so
                # the user knows the clip is silent on purpose, not by
                # coincidence.
                _emit_rung("screen-only (no dshow audio devices)")
            else:
                _emit_rung("screen-only (no audio configured)")
            return

        low = failure_out.lower()
        audio_failed = audio_inputs > 0 and any(
            m in low for m in self._AUDIO_FAILURE_MARKERS
        )
        if not audio_failed:
            raise RuntimeError(f"Replay buffer failed to start. ffmpeg output\n{failure_out}")

        snippet = failure_out.strip().replace("\n", " | ")[:600]
        self.log(f"[Replay] audio-pipeline ffmpeg output: {snippet}")

        # Helper: run a rung with a temporary override of the capture
        # devices. Restores the saved tuple even on failure so the
        # next rung sees clean state.
        def _try_rung(label: str, drop_sys: bool, drop_mic: bool) -> Optional[bool]:
            saved_sys = self.devices.system_audio
            saved_mic = self.devices.mic
            if drop_sys:
                self.devices.system_audio = None
            if drop_mic:
                self.devices.mic = None
            try:
                args_r, inputs_r = _build_args(with_audio=True)
            finally:
                self.devices.system_audio = saved_sys
                self.devices.mic = saved_mic
            if inputs_r == 0:
                return None  # Rung contributes no audio; caller will skip.
            self.log(f"[Replay] Trying rung {label}...")
            fail = _launch(args_r)
            if fail is None:
                _emit_rung(label)
                return True
            sn = fail.strip().replace("\n", " | ")[:400]
            self.log(f"[Replay] {label} rung ffmpeg output: {sn}")
            return False

        # Rung 1: mic-only.
        if has_mic:
            r = _try_rung("mic-only", drop_sys=True, drop_mic=False)
            if r is True:
                return

        # Rung 2: desktop-only.
        if has_sys:
            r = _try_rung("desktop-only", drop_sys=False, drop_mic=True)
            if r is True:
                return

        # Rung 3: screen-only (no audio at all).
        self.log("[Replay] All audio rungs failed; retrying screen-only.")
        args_v, _ = _build_args(with_audio=False)
        failure_out2 = _launch(args_v)
        if failure_out2 is None:
            _emit_rung("screen-only (all audio rungs failed)")
            return
        raise RuntimeError(
            "Replay buffer failed to start (screen-only retry also failed). "
            f"ffmpeg output\n{failure_out2}"
        )

    def stop(self) -> None:
        # Pass Q: finalize the audio fallback FIRST so any in-flight
        # WAV writes flush before we tear the buffer dir down. Stop is
        # idempotent at the recorder level, so a second stop() call
        # later is fine.
        fb = self._audio_fallback
        if fb is not None:
            try:
                fb.stop()
                self._last_fallback_paths = fb.paths()
                paths = self._last_fallback_paths
                if paths and (paths.mic_wav or paths.desktop_wav):
                    self.log(
                        f"[Replay] Audio fallback finalized: "
                        f"mic={paths.mic_wav.name if paths.mic_wav else 'none'}, "
                        f"desktop={paths.desktop_wav.name if paths.desktop_wav else 'none'}."
                    )
                else:
                    self.log("[Replay] Audio fallback finalized: no usable WAVs.")
            except Exception as exc:
                self.log(f"[Replay] Audio fallback stop failed: {exc}")
            self._audio_fallback = None

        # Pass R-C: resume STT after the fallback releases the mic. Done
        # AFTER fb.stop() returns so we don't race PortAudio reopening
        # the same device while the fallback's stream is still tearing
        # down. Only fires if we actually paused; a pure desktop-loopback
        # fallback (mic_wanted=False) leaves STT running throughout.
        if self._stt_was_paused_for_fallback and self._stt_resume_cb is not None:
            try:
                self._stt_resume_cb()
            except Exception as exc:
                self.log(f"[Replay] STT resume callback failed: {exc}.")
            finally:
                self._stt_was_paused_for_fallback = False

        proc = self.proc
        if proc is None or proc.poll() is not None:
            self.proc = None
            return
        self.log("[Replay] Stopping buffer...")
        self._stop_drain.set()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.proc = None

    def _list_segments(self, buffer_dir: Path) -> List[Path]:
        segs = [p.resolve() for p in buffer_dir.glob("seg*.mp4") if p.is_file()]
        segs.sort(key=lambda p: p.stat().st_mtime)
        return segs

    def _run_ffmpeg_concat_demuxer(self, ffmpeg: str, concat_txt: Path, tmp_out: Path) -> tuple[bool, str]:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_txt),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(tmp_out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        detail = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode == 0 and tmp_out.exists(), detail

    def _run_ffmpeg_concat_filter(self, ffmpeg: str, selected: List[Path], tmp_out: Path) -> tuple[bool, str]:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
        for p in selected:
            cmd += ["-i", str(p)]
        n = len(selected)
        filter_complex = "".join(f"[{i}:v:0]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
        cmd += [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(tmp_out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        detail = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode == 0 and tmp_out.exists(), detail

    def _resolve_ffprobe(self, ffmpeg: str) -> List[str]:
        """Return ffprobe candidate paths to try, in order.

        Prefers the sibling ``ffprobe(.exe)`` next to the resolved
        ``ffmpeg`` (so the bundled ffmpeg-7.1.1 build wins on Windows),
        then falls back to PATH.
        """
        candidates: List[str] = []
        try:
            ff_path = Path(ffmpeg)
            if ff_path.is_absolute() or ff_path.exists():
                sib = ff_path.parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
                if sib.exists():
                    candidates.append(str(sib))
        except Exception:
            pass
        candidates.append("ffprobe")
        return candidates

    def _probe_media_duration(self, ffmpeg: str, path: Path) -> Optional[float]:
        """Return the container duration in seconds, or None on failure.

        Pass V-B uses this for both the mic WAV and the exported MP4
        so the AV-drift check has consistent numbers (rather than
        comparing wave.getnframes/getframerate against an ffprobe
        duration -- in practice they can disagree by ~50 ms because
        ffmpeg sometimes pads PTS).
        """
        if not path.exists():
            return None
        for ffprobe in self._resolve_ffprobe(ffmpeg):
            try:
                proc = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path),
                    ],
                    capture_output=True, text=True, timeout=15,
                )
            except FileNotFoundError:
                continue
            except Exception:
                return None
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out:
                try:
                    return float(out.splitlines()[0])
                except Exception:
                    return None
        return None

    def _probe_final_audio_stream(
        self, ffmpeg: str, out_path: Path,
    ) -> tuple[bool, Optional[str], Optional[int], Optional[int], Optional[float]]:
        """Verify the exported MP4 actually contains an audio stream.

        Pass U-B step 4 + Pass V-E enrichment. Returns
        ``(has_audio, codec, samplerate, channels, duration_seconds)``.
        Each field is None when the probe couldn't determine it. The
        whole point of the fallback recorder + mux path is to
        guarantee that exported clips are NOT silent, but the log
        line that previously claimed "Final exported streams:
        video + fallback-mic" was derived purely from which inputs we
        thought we had handed ffmpeg -- if mux silently dropped the
        audio (zero-byte WAV, codec mismatch, ``-shortest`` clipping it
        away, etc.) the user only learned by trying to play the clip.

        We probe the actual file. Prefer ``ffprobe`` (ships next to
        ``ffmpeg.exe`` in the standard Windows build) and fall back to
        parsing ``ffmpeg -i`` when ffprobe isn't on PATH or in the
        bundled folder. Never raises -- on any error returns
        ``(False, None, None, None, None)`` and logs.
        """
        empty: tuple[bool, Optional[str], Optional[int], Optional[int], Optional[float]] = (
            False, None, None, None, None,
        )
        if not out_path.exists():
            return empty
        for ffprobe in self._resolve_ffprobe(ffmpeg):
            try:
                proc = subprocess.run(
                    [
                        ffprobe, "-v", "error", "-select_streams", "a",
                        "-show_entries",
                        "stream=codec_name,sample_rate,channels,duration",
                        "-of", "csv=p=0", str(out_path),
                    ],
                    capture_output=True, text=True, timeout=15,
                )
            except FileNotFoundError:
                continue
            except Exception as exc:
                self.log(f"[Replay] ffprobe invocation failed ({exc}); will try ffmpeg -i.")
                break
            out = (proc.stdout or "").strip()
            if proc.returncode == 0:
                if not out:
                    return empty
                first = out.splitlines()[0].strip()
                # csv format: codec_name,sample_rate,channels,duration
                parts = [p.strip() for p in first.split(",")]
                codec = parts[0] if len(parts) > 0 and parts[0] else None
                samplerate: Optional[int] = None
                channels: Optional[int] = None
                duration: Optional[float] = None
                try:
                    if len(parts) > 1 and parts[1] and parts[1].upper() != "N/A":
                        samplerate = int(float(parts[1]))
                except Exception:
                    samplerate = None
                try:
                    if len(parts) > 2 and parts[2] and parts[2].upper() != "N/A":
                        channels = int(parts[2])
                except Exception:
                    channels = None
                try:
                    if len(parts) > 3 and parts[3] and parts[3].upper() != "N/A":
                        duration = float(parts[3])
                except Exception:
                    duration = None
                if duration is None:
                    duration = self._probe_media_duration(ffmpeg, out_path)
                return True, codec, samplerate, channels, duration
            # Non-zero return -- continue to next candidate, then fallback.
        # Fallback: ffmpeg -i and look for "Stream #...: Audio" in stderr.
        try:
            proc2 = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(out_path)],
                capture_output=True, text=True, timeout=15,
            )
            text = (proc2.stderr or "") + (proc2.stdout or "")
            for line in text.splitlines():
                low = line.lower()
                if "stream #" in low and "audio" in low:
                    codec: Optional[str] = None
                    samplerate = None
                    channels = None
                    try:
                        idx = low.index("audio:")
                        rest = line[idx + len("Audio:"):].strip()
                        # rest looks like: "aac (LC), 44100 Hz, mono, fltp, 192 kb/s"
                        tokens = [t.strip() for t in rest.split(",")]
                        if tokens:
                            codec = tokens[0].split()[0] or None
                        for tok in tokens[1:]:
                            tlow = tok.lower()
                            if tlow.endswith(" hz"):
                                try:
                                    samplerate = int(tlow[:-3].strip())
                                except Exception:
                                    pass
                            elif "mono" in tlow:
                                channels = 1
                            elif "stereo" in tlow:
                                channels = 2
                    except Exception:
                        pass
                    return True, codec, samplerate, channels, None
            return empty
        except Exception as exc:
            self.log(f"[Replay] ffmpeg -i probe failed ({exc}); cannot verify final audio stream.")
            return empty

    def _mux_fallback_audio(
        self,
        ffmpeg: str,
        video_in: Path,
        mic_wav: Optional[Path],
        desk_wav: Optional[Path],
        out_path: Path,
    ) -> tuple[bool, str]:
        """Mux any captured fallback WAVs into ``video_in`` -> ``out_path``.

        Pass Q audio export path. Inputs:
          [0] video_in (video-only, no audio)
          [1] mic_wav (optional)
          [2] desk_wav (optional)

        When both audio inputs are present they're amix'ed; when one is
        present it's mapped directly. Uses ``-shortest`` so the clip
        length matches the video (the fallback recorder is recording
        continuously and will typically have more audio than video).
        """
        if not mic_wav and not desk_wav:
            return False, "no fallback audio"
        cmd: list[str] = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(video_in),
        ]
        audio_idx: list[int] = []
        if mic_wav and mic_wav.exists():
            cmd += ["-i", str(mic_wav)]
            audio_idx.append(len(audio_idx) + 1)  # ffmpeg input index 1
        if desk_wav and desk_wav.exists():
            cmd += ["-i", str(desk_wav)]
            audio_idx.append(len(audio_idx) + 1)  # ffmpeg input index 2 (or 1 if mic absent)

        cmd += ["-map", "0:v"]
        if len(audio_idx) == 1:
            cmd += ["-map", f"{audio_idx[0]}:a"]
        else:
            # Two audio sources -> amix.
            fc = "".join(f"[{i}:a]" for i in audio_idx) + "amix=inputs=2:duration=longest:dropout_transition=0[aout]"
            cmd += ["-filter_complex", fc, "-map", "[aout]"]

        cmd += [
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_path),
        ]
        self.log(f"[Replay] Muxing fallback audio: video + {len(audio_idx)} WAV input(s).")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        detail = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode == 0 and out_path.exists(), detail

    def export_last(self, minutes: Optional[int] = None, extra_seconds: int = 0, upscale: str = "none") -> Path:
        s = load_settings()
        minutes = minutes or s.replay_minutes
        clips_dir = Path(s.clips_dir).resolve()
        clips_dir.mkdir(parents=True, exist_ok=True)
        buffer_dir = Path(s.buffer_dir).resolve()

        if extra_seconds > 0:
            self.log(f"[Replay] Waiting extra {extra_seconds}s to capture after trigger...")
            time.sleep(extra_seconds)

        segs = self._list_segments(buffer_dir)
        if not segs:
            raise RuntimeError("No replay segments found. Is the buffer running?")

        if self.is_running() and len(segs) > 1:
            newest_age = time.time() - segs[-1].stat().st_mtime
            if newest_age < max(2, s.segment_seconds):
                self.log(f"[Replay] Skipping active segment: {segs[-1].name}")
                segs = segs[:-1]

        target_seconds = int(minutes * 60 + extra_seconds)
        selected: List[Path] = []
        total = 0
        for p in reversed(segs):
            selected.append(p)
            total += s.segment_seconds
            if total >= target_seconds:
                break
        selected = list(reversed(selected))
        if not selected:
            raise RuntimeError("No finalized replay segments available yet.")

        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = (clips_dir / f"clip_{ts}.mp4").resolve()
        tmp_out = (clips_dir / f"clip_{ts}_tmp.mp4").resolve()
        concat_txt = (clips_dir / f"concat_{ts}.txt").resolve()

        with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
            for p in selected:
                safe = p.resolve().as_posix().replace("'", r"'\''")
                f.write(f"file '{safe}'\n")

        ffmpeg = _find_ffmpeg()
        self.log(f"[Replay] Exporting clip ({len(selected)} segments) -> {tmp_out.name}")

        ok, detail = self._run_ffmpeg_concat_demuxer(ffmpeg, concat_txt, tmp_out)
        if not ok:
            self.log(f"[Replay] Demuxer concat failed, trying filter concat... {detail[:300]}")
            tmp_out.unlink(missing_ok=True)
            ok, detail = self._run_ffmpeg_concat_filter(ffmpeg, selected, tmp_out)

        if not ok:
            concat_txt.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg concat failed: {detail[:1200]}")
        if not tmp_out.exists():
            concat_txt.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg concat completed but no temporary clip was produced.")

        # Pass Q / Pass U: if the screen-only rung armed the Python
        # fallback, mux those WAVs into the video before the (optional)
        # upscale step. Replaces tmp_out in place so downstream paths
        # don't change. Failure is non-fatal: we keep the silent video.
        #
        # Pass U-B root cause fix: the previous version read
        # ``_last_fallback_paths``, which is only populated by
        # ``stop()`` -- so a clip taken while the buffer was still
        # running always saw ``None`` and silently produced a
        # video-only MP4 even though the fallback recorder was actively
        # capturing mic. We now call ``fb.snapshot()`` to rotate the
        # WAV files (finalize current ones, open fresh ones for ongoing
        # capture) and use the returned snapshot paths for muxing.
        fb_running = self._audio_fallback
        if fb_running is not None:
            try:
                fb_paths = fb_running.snapshot()
            except Exception as exc:
                self.log(
                    f"[Replay] Audio fallback snapshot failed ({exc}); "
                    "falling back to last-finalized paths."
                )
                fb_paths = self._last_fallback_paths
        else:
            fb_paths = self._last_fallback_paths
        mic_w = getattr(fb_paths, "mic_wav", None) if fb_paths is not None else None
        desk_w = getattr(fb_paths, "desktop_wav", None) if fb_paths is not None else None
        muxed_streams: list[str] = ["video"]
        if mic_w or desk_w:
            # Pass V-A: optionally normalize / gate the mic WAV before
            # mux. Failure is non-fatal -- the original WAV stays
            # available and we mux that instead.
            if mic_w is not None:
                try:
                    from services.audio_fallback_recorder import preprocess_mic_wav
                    processed_mic, gate_on, norm_on, peak_b, peak_a = preprocess_mic_wav(
                        mic_w, log_cb=self.log,
                    )
                    if (gate_on or norm_on) and processed_mic and processed_mic.exists():
                        mic_w = processed_mic
                    self.log(
                        f"[Replay] Mic preprocess summary: gate={'on' if gate_on else 'off'}, "
                        f"normalize={'on' if norm_on else 'off'}, peak_before={peak_b:.3f}, "
                        f"peak_after={peak_a:.3f}."
                    )
                except Exception as exc:
                    self.log(f"[Replay] Mic preprocess raised ({exc}); using raw WAV.")

            # Pass V-B: AV duration drift check. Probe the post-concat
            # video duration and compare against the mic WAV duration.
            # Drift > 0.75s (visible to a human listener) gets a
            # warning; we keep ``-shortest`` in the mux step so the
            # final clip never has a long trailing audio tail.
            try:
                vid_dur = self._probe_media_duration(ffmpeg, tmp_out)
            except Exception:
                vid_dur = None
            mic_dur: Optional[float] = None
            if mic_w is not None:
                try:
                    mic_dur = self._probe_media_duration(ffmpeg, mic_w)
                except Exception:
                    mic_dur = None
            if vid_dur is not None and mic_dur is not None:
                drift = abs(vid_dur - mic_dur)
                if drift > 0.75:
                    self.log(
                        f"[ReplayAudio] AV duration drift: video={vid_dur:.2f}s "
                        f"mic={mic_dur:.2f}s drift={drift:.2f}s. ffmpeg -shortest "
                        "will trim trailing audio so the clip stays in sync; "
                        "video is never cut."
                    )
                else:
                    self.log(
                        f"[ReplayAudio] AV duration check: video={vid_dur:.2f}s "
                        f"mic={mic_dur:.2f}s drift={drift:.2f}s (within 0.75s)."
                    )
            self.log(
                f"[Replay] Mux inputs ready: "
                f"mic={mic_w.name if mic_w else 'none'}, "
                f"desktop={desk_w.name if desk_w else 'none'}."
            )
            tmp_muxed = (clips_dir / f"clip_{ts}_muxed.mp4").resolve()
            ok_mux, mux_detail = self._mux_fallback_audio(
                ffmpeg, tmp_out, mic_w, desk_w, tmp_muxed
            )
            if ok_mux:
                try:
                    tmp_out.unlink(missing_ok=True)
                except Exception:
                    pass
                tmp_out = tmp_muxed
                if mic_w:
                    muxed_streams.append("fallback-mic")
                if desk_w:
                    muxed_streams.append("fallback-desktop")
            else:
                self.log(
                    f"[Replay] Fallback audio mux failed; keeping silent video. "
                    f"detail: {mux_detail[:300]}"
                )
                try:
                    tmp_muxed.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            # Pass U-B: explicit log line when the export skips audio so
            # the user can grep for "audio skipped" and find the reason.
            if fb_running is None and self._last_fallback_paths is None:
                reason = "no audio fallback was armed (ffmpeg/dshow rung succeeded with audio, or user did not request audio)"
            else:
                reason = "fallback recorder produced no usable WAV (silent or empty after all retries)"
            self.log(f"[Replay] Audio skipped at export: {reason}.")

        upscale = (upscale or "none").lower()
        if upscale in ("none", ""):
            shutil.move(str(tmp_out), str(out_path))
        else:
            scale = "1920:1080" if upscale == "1080p" else "3840:2160" if upscale == "4k" else None
            if scale:
                cmd2 = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-i",
                    str(tmp_out),
                    "-vf",
                    f"scale={scale}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(out_path),
                ]
                self.log(f"[Replay] Upscaling -> {upscale} ({scale})")
                proc2 = subprocess.run(cmd2, capture_output=True, text=True)
                tmp_out.unlink(missing_ok=True)
                if proc2.returncode != 0:
                    concat_txt.unlink(missing_ok=True)
                    detail = (proc2.stderr or proc2.stdout or "").strip()
                    raise RuntimeError(f"ffmpeg upscale failed ({proc2.returncode}): {detail[:1200]}")
            else:
                shutil.move(str(tmp_out), str(out_path))

        concat_txt.unlink(missing_ok=True)
        # Pass Q: clear summary of what's in the final clip so the user
        # doesn't have to grep the log to know which streams made it.
        self.log(
            f"[Replay] Final exported streams: {', '.join(muxed_streams)} "
            f"({len(muxed_streams)} input{'s' if len(muxed_streams) != 1 else ''})."
        )

        # Pass U-B step 4 + Pass V-E: probe the final MP4 to verify it
        # actually has the audio stream we think we muxed and surface
        # the audio metadata (codec/samplerate/channels/duration) in
        # the log so the operator can spot wrong sample rates or
        # channel layouts at a glance.
        try:
            has_audio, audio_codec, audio_sr, audio_ch, audio_dur = (
                self._probe_final_audio_stream(ffmpeg, out_path)
            )
        except Exception as exc:
            has_audio, audio_codec, audio_sr, audio_ch, audio_dur = (
                False, None, None, None, None,
            )
            self.log(f"[Replay] Final audio-stream probe raised ({exc}); cannot verify.")
        expected_audio = len(muxed_streams) > 1

        def _audio_meta_str() -> str:
            bits = []
            if audio_codec:
                bits.append(f"codec={audio_codec}")
            if audio_sr:
                bits.append(f"sr={audio_sr}Hz")
            if audio_ch:
                bits.append(f"ch={audio_ch}")
            if audio_dur is not None:
                bits.append(f"dur={audio_dur:.2f}s")
            return ", ".join(bits) if bits else "metadata unknown"

        if has_audio and expected_audio:
            # Pass V-E: spell out which streams the mux step actually
            # contributed. The legacy log said "video + audio" but
            # the operator wants to know whether the audio came from
            # mic, desktop, or both.
            stream_label = " + ".join(
                s for s in muxed_streams if s != "video"
            ).replace("fallback-mic", "mic").replace("fallback-desktop", "desktop")
            self.log(
                f"[Replay] Final exported streams verified: video + {stream_label} "
                f"({_audio_meta_str()})."
            )
        elif has_audio and not expected_audio:
            self.log(
                f"[Replay] Final exported streams: ffprobe found an audio stream "
                f"({_audio_meta_str()}) we did not intend to add."
            )
        elif not has_audio and expected_audio:
            self.log(
                "[Replay] Audio skipped at export: ffprobe confirms final MP4 has NO "
                "audio stream despite the mux step succeeding. Likely causes: "
                "(a) WAV inputs were zero-length / silent so amix produced no output; "
                "(b) -shortest clipped audio to 0 because video had no PTS yet; "
                "(c) the muxed temp file was overwritten by the upscale step without "
                "carrying audio through."
            )
        else:
            self.log("[Replay] Final exported streams verified: video only.")

        self.log(f"[Replay] Saved: {out_path}")
        return out_path
