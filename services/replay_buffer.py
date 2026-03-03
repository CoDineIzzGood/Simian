"""
Replay buffer recorder (GeForce Experience style) using FFmpeg rolling segments.

Design goals:
- Runs as a background process
- Always recording, but only *exports* when asked ("clip that")
- Keeps last N minutes using a fixed-size segment ring (segment_wrap)
- Captures screen + system audio + mic (best-effort; device names are configurable)

Windows capture notes:
- Video: gdigrab (desktop)
- Audio: dshow devices (system audio typically requires "Stereo Mix" or a virtual capture device)
- If system/mic device names are missing, it will fall back to whichever is available.

You can list audio devices with:
  python -m services.audio_devices
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from services.settings_store import load_settings


def _find_ffmpeg() -> str:
    # Prefer bundled ffmpeg.exe if present
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
    system_audio: Optional[str] = None  # dshow device name
    mic: Optional[str] = None           # dshow device name


class ReplayBufferRecorder:
    def __init__(self, devices: Optional[CaptureDevices] = None, log_cb=None):
        self.devices = devices or CaptureDevices()
        self.log = log_cb or (lambda msg: None)
        self.proc: Optional[subprocess.Popen] = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.is_running():
            self.log("[Replay] Buffer already running.")
            return

        s = load_settings()
        buffer_dir = Path(s.buffer_dir)
        buffer_dir.mkdir(parents=True, exist_ok=True)

        # Clean old segments (optional)
        for p in buffer_dir.glob("seg*.mp4"):
            try:
                p.unlink()
            except Exception:
                pass

        # segment count = minutes * 60 / segment_seconds
        seg_count = max(1, int((s.replay_minutes * 60) / max(1, s.segment_seconds)))

        ffmpeg = _find_ffmpeg()
        out_pattern = str(buffer_dir / "seg%03d.mp4")

        # Build inputs
        args = [ffmpeg, "-y",
                "-hide_banner", "-loglevel", "warning",
                "-f", "gdigrab",
                "-framerate", str(s.fps),
                "-video_size", f"{s.width}x{s.height}",
                "-i", "desktop"]

        # Audio inputs (optional)
        audio_inputs = []
        if self.devices.system_audio:
            args += ["-f", "dshow", "-i", f"audio={self.devices.system_audio}"]
            audio_inputs.append(len(audio_inputs) + 1)  # input index after video
        if self.devices.mic:
            args += ["-f", "dshow", "-i", f"audio={self.devices.mic}"]
            audio_inputs.append(len(audio_inputs) + (1 if self.devices.system_audio else 1))

        filter_complex = None
        map_args = ["-map", "0:v"]

        if len(audio_inputs) == 0:
            # no audio captured
            pass
        elif len(audio_inputs) == 1:
            map_args += ["-map", "1:a"]
        else:
            # mix 2 audio sources
            # audio inputs are 1:a and 2:a
            filter_complex = "[1:a][2:a]amix=inputs=2:duration=longest[aout]"
            map_args += ["-map", "[aout]"]

        args += map_args

        if filter_complex:
            args += ["-filter_complex", filter_complex]

        # Encode
        args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "192k"]

        # Segment muxer (ring buffer)
        args += ["-f", "segment",
                 "-segment_time", str(max(1, s.segment_seconds)),
                 "-segment_wrap", str(seg_count),
                 "-reset_timestamps", "1",
                 out_pattern]

        self.log(f"[Replay] Starting buffer: {s.replay_minutes}m @ {s.fps}fps {s.width}x{s.height} seg={s.segment_seconds}s wrap={seg_count}")
        self.log(f"[Replay] ffmpeg cmd: {' '.join(args)}")

        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.4)

        if not self.is_running():
            out = ""
            try:
                out = (self.proc.stdout.read() if self.proc and self.proc.stdout else "")
            except Exception:
                pass
            self.proc = None
            raise RuntimeError(f"Replay buffer failed to start. ffmpeg output:\n{out}")

    def stop(self) -> None:
        if not self.is_running():
            self.proc = None
            return
        self.log("[Replay] Stopping buffer...")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _list_segments(self, buffer_dir: Path) -> List[Path]:
        segs = [p for p in buffer_dir.glob("seg*.mp4") if p.is_file()]
        # sort by mtime
        segs.sort(key=lambda p: p.stat().st_mtime)
        return segs

    def export_last(self, minutes: Optional[int] = None, extra_seconds: int = 0, upscale: str = "none") -> Path:
        s = load_settings()
        minutes = minutes or s.replay_minutes
        clips_dir = Path(s.clips_dir)
        clips_dir.mkdir(parents=True, exist_ok=True)
        buffer_dir = Path(s.buffer_dir)

        if extra_seconds > 0:
            self.log(f"[Replay] Waiting extra {extra_seconds}s to capture after trigger...")
            time.sleep(extra_seconds)

        segs = self._list_segments(buffer_dir)
        if not segs:
            raise RuntimeError("No replay segments found. Is the buffer running?")

        # Determine how many seconds to include
        target_seconds = int(minutes * 60 + extra_seconds)
        # Read segments newest->oldest until we cover target_seconds
        selected = []
        total = 0
        for p in reversed(segs):
            selected.append(p)
            total += s.segment_seconds
            if total >= target_seconds:
                break
        selected = list(reversed(selected))

        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = clips_dir / f"clip_{ts}.mp4"

        # Create concat list
        concat_txt = clips_dir / f"concat_{ts}.txt"
        with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
            for p in selected:
                f.write(f"file '{p.as_posix()}'\n")

        ffmpeg = _find_ffmpeg()

        # Base concat
        tmp_out = clips_dir / f"clip_{ts}_tmp.mp4"
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
               "-f", "concat", "-safe", "0",
               "-i", str(concat_txt),
               "-c", "copy",
               str(tmp_out)]
        self.log(f"[Replay] Exporting clip ({len(selected)} segments) -> {tmp_out.name}")
        subprocess.check_call(cmd)

        # Optional upscale export
        upscale = (upscale or "none").lower()
        if upscale in ("none", ""):
            shutil.move(str(tmp_out), str(out_path))
        else:
            if upscale == "1080p":
                scale = "1920:1080"
            elif upscale == "4k":
                scale = "3840:2160"
            else:
                scale = None
            if scale:
                cmd2 = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                        "-i", str(tmp_out),
                        "-vf", f"scale={scale}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "192k",
                        str(out_path)]
                self.log(f"[Replay] Upscaling -> {upscale} ({scale})")
                subprocess.check_call(cmd2)
                tmp_out.unlink(missing_ok=True)
            else:
                shutil.move(str(tmp_out), str(out_path))

        concat_txt.unlink(missing_ok=True)
        self.log(f"[Replay] Saved: {out_path}")
        return out_path
