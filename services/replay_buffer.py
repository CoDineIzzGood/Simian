"""
Replay buffer recorder (GeForce Experience style) using FFmpeg rolling segments.
Optimized to avoid stdout pipe stalls and to fail fast with useful diagnostics.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional

from services.settings_store import load_settings


def _find_ffmpeg() -> str:
    candidates = [
        os.environ.get("FFMPEG_PATH"),
        str(Path("ffmpeg") / "bin" / "ffmpeg.exe"),
        "ffmpeg",
    ]
    for c in candidates:
        if not c:
            continue
        if c.lower().endswith("ffmpeg.exe") and Path(c).exists():
            return c
        if c == "ffmpeg":
            return c
        if Path(c).exists():
            return c
    return "ffmpeg"


@dataclass
class CaptureDevices:
    system_audio: Optional[str] = None
    mic: Optional[str] = None


class ReplayBufferRecorder:
    def __init__(self, devices: Optional[CaptureDevices] = None, log_cb=None):
        self.devices = devices or CaptureDevices()
        self.log = log_cb or (lambda msg: None)
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._drain_thread: Optional[threading.Thread] = None
        self._recent_output: Deque[str] = deque(maxlen=120)

    def is_running(self) -> bool:
        with self._lock:
            proc = self.proc
            return proc is not None and proc.poll() is None

    def _append_output(self, line: str) -> None:
        if line:
            self._recent_output.append(line.rstrip())

    def _drain_proc_output(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, ""):
                if not raw:
                    break
                self._append_output(raw)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _start_output_drain(self, proc: subprocess.Popen) -> None:
        self._recent_output.clear()
        if proc.stdout is None:
            self._drain_thread = None
            return
        self._drain_thread = threading.Thread(
            target=self._drain_proc_output,
            args=(proc,),
            name="ReplayBufferFFmpegDrain",
            daemon=True,
        )
        self._drain_thread.start()

    def _stop_output_drain(self) -> None:
        thread = self._drain_thread
        self._drain_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=1.0)

    def _startup_detail(self) -> str:
        detail = "\n".join(x for x in self._recent_output if x).strip()
        return detail[:4000] if detail else "(no ffmpeg output captured)"

    def start(self) -> None:
        with self._lock:
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

            seg_seconds = max(1, int(getattr(s, "segment_seconds", 5) or 5))
            replay_minutes = max(1, int(getattr(s, "replay_minutes", 5) or 5))
            fps = max(1, int(getattr(s, "fps", 30) or 30))
            width = max(320, int(getattr(s, "width", 1920) or 1920))
            height = max(240, int(getattr(s, "height", 1080) or 1080))
            seg_count = max(1, int((replay_minutes * 60) / seg_seconds))
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
                str(fps),
                "-video_size",
                f"{width}x{height}",
                "-i",
                "desktop",
            ]

            audio_inputs = 0
            if self.devices.system_audio:
                args += ["-f", "dshow", "-i", f"audio={self.devices.system_audio}"]
                audio_inputs += 1
            if self.devices.mic:
                args += ["-f", "dshow", "-i", f"audio={self.devices.mic}"]
                audio_inputs += 1

            filter_complex = None
            map_args = ["-map", "0:v"]
            if audio_inputs == 1:
                map_args += ["-map", "1:a"]
            elif audio_inputs >= 2:
                filter_complex = "[1:a][2:a]amix=inputs=2:duration=longest[aout]"
                map_args += ["-map", "[aout]"]

            args += map_args
            if filter_complex:
                args += ["-filter_complex", filter_complex]

            args += [
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
                "-f",
                "segment",
                "-segment_time",
                str(seg_seconds),
                "-segment_wrap",
                str(seg_count),
                "-reset_timestamps",
                "1",
                out_pattern,
            ]

            self.log(
                f"[Replay] Starting buffer: {replay_minutes}m @ {fps}fps {width}x{height} seg={seg_seconds}s wrap={seg_count}"
            )
            self.log(f"[Replay] ffmpeg cmd: {' '.join(args)}")

            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
            self._start_output_drain(self.proc)

        time.sleep(0.6)
        if not self.is_running():
            with self._lock:
                proc = self.proc
                detail = self._startup_detail()
                self.proc = None
                self._stop_output_drain()
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            raise RuntimeError(f"Replay buffer failed to start. ffmpeg output:\n{detail}")

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.proc = None
                self._stop_output_drain()
                return
            self.log("[Replay] Stopping buffer...")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            self.proc = None
        self._stop_output_drain()

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
        filter_complex = "".join(f"[{i}:v:0]" for i in range(n)) + "".join(
            f"[{i}:a:0]" for i in range(n)
        ) + f"concat=n={n}:v=1:a=1[v][a]"
        cmd += [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
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

    def export_last(self, minutes: Optional[int] = None, extra_seconds: int = 0, upscale: str = "none") -> Path:
        s = load_settings()
        minutes = max(1, int(minutes or s.replay_minutes or 5))
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
            if newest_age < max(2, int(getattr(s, "segment_seconds", 5) or 5)):
                self.log(f"[Replay] Skipping active segment: {segs[-1].name}")
                segs = segs[:-1]

        target_seconds = int(minutes * 60 + extra_seconds)
        selected: List[Path] = []
        total = 0
        seg_seconds = max(1, int(getattr(s, "segment_seconds", 5) or 5))
        for p in reversed(segs):
            selected.append(p)
            total += seg_seconds
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
