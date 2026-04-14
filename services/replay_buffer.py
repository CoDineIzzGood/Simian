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
from typing import List, Optional

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
    def __init__(self, devices: Optional[CaptureDevices] = None, log_cb=None):
        self.devices = devices or CaptureDevices()
        self.log = log_cb or (lambda msg: None)
        self.proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stop_drain = threading.Event()

    def is_running(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def _drain_output(self, proc: subprocess.Popen) -> None:
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
                low = line.lower()
                if any(k in low for k in ("error", "failed", "warning", "invalid")):
                    self.log(f"[Replay][ffmpeg] {line[:500]}")
        except Exception:
            return

    def _audio_input_args(self, system_audio: Optional[str], mic: Optional[str]) -> tuple[list[str], int, Optional[str]]:
        args: list[str] = []
        audio_inputs = 0
        filter_complex = None

        sys_choice = (system_audio or "").strip()
        mic_choice = (mic or "").strip()

        if not sys_choice or sys_choice == DEFAULT_WASAPI_SYSTEM:
            sys_choice = _pick_auto_system_audio(self.log) or ""

        if sys_choice:
            sys_spec = sys_choice if sys_choice.startswith("audio=") else f"audio={sys_choice}"
            args += ["-thread_queue_size", "512", "-f", "dshow", "-i", sys_spec]
            audio_inputs += 1

        if mic_choice:
            mic_spec = mic_choice if mic_choice.startswith("audio=") else f"audio={mic_choice}"
            args += ["-thread_queue_size", "512", "-f", "dshow", "-i", mic_spec]
            audio_inputs += 1

        if audio_inputs >= 2:
            filter_complex = "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
        return args, audio_inputs, filter_complex

    def start(self) -> None:
        if self.is_running():
            self.log("[Replay] Buffer already running.")
            return

        s = load_settings()
        buffer_dir = Path(s.buffer_dir).resolve()
        buffer_dir.mkdir(parents=True, exist_ok=True)

        for p in buffer_dir.glob("seg*.mp4"):
            try:
                p.unlink()
            except Exception:
                pass

        seg_count = max(1, int((s.replay_minutes * 60) / max(1, s.segment_seconds)))
        ffmpeg = _find_ffmpeg()
        out_pattern = str((buffer_dir / "seg%03d.mp4").resolve())

        args = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtbufsize",
            "256M",
            "-f",
            "gdigrab",
            "-framerate",
            str(s.fps),
            "-video_size",
            f"{s.width}x{s.height}",
            "-i",
            "desktop",
        ]

        audio_args, audio_inputs, filter_complex = self._audio_input_args(self.devices.system_audio, self.devices.mic)
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

        self.log(
            f"[Replay] Starting buffer: {s.replay_minutes}m @ {s.fps}fps {s.width}x{s.height} seg={s.segment_seconds}s wrap={seg_count}"
        )
        self.log(f"[Replay] ffmpeg cmd: {' '.join(args)}")

        self._stop_drain.clear()
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        self._stdout_thread = threading.Thread(target=self._drain_output, args=(self.proc,), daemon=True)
        self._stdout_thread.start()
        time.sleep(0.8)
        if not self.is_running():
            out = ""
            proc = self.proc
            try:
                out = proc.stdout.read() if proc and proc.stdout else ""
            except Exception:
                pass
            self.proc = None
            raise RuntimeError(f"Replay buffer failed to start. ffmpeg output\n{out}")

    def stop(self) -> None:
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
        self.log(f"[Replay] Saved: {out_path}")
        return out_path
