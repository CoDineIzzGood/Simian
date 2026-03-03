"""
Standalone screen recorder used for one-off captures.

For "replay buffer" (always-on rolling recording) use services.replay_buffer instead.

Usage:
  python screen_recorder.py --duration 30 --out data/clips/recording.mp4
  python screen_recorder.py --list-audio-devices
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

from services.replay_buffer import _find_ffmpeg
from services.settings_store import load_settings


def list_audio_devices() -> None:
    ffmpeg = _find_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    subprocess.call(cmd)


def record(duration: int, out: Path, system_audio: str | None, mic: str | None, fps: int, width: int, height: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _find_ffmpeg()

    args = [ffmpeg, "-y",
            "-hide_banner", "-loglevel", "warning",
            "-f", "gdigrab",
            "-framerate", str(fps),
            "-video_size", f"{width}x{height}",
            "-i", "desktop"]

    audio_inputs = 0
    if system_audio:
        args += ["-f", "dshow", "-i", f"audio={system_audio}"]
        audio_inputs += 1
    if mic:
        args += ["-f", "dshow", "-i", f"audio={mic}"]
        audio_inputs += 1

    map_args = ["-map", "0:v"]
    if audio_inputs == 1:
        map_args += ["-map", "1:a"]
    elif audio_inputs == 2:
        args += ["-filter_complex", "[1:a][2:a]amix=inputs=2:duration=longest[aout]"]
        map_args += ["-map", "[aout]"]
    args += map_args

    args += ["-t", str(max(1, duration)),
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k",
             str(out)]

    subprocess.check_call(args)


def main() -> int:
    s = load_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=30, help="seconds")
    ap.add_argument("--out", type=str, default="", help="output mp4 path")
    ap.add_argument("--system-audio", type=str, default=os.environ.get("SIMIAN_SYSTEM_AUDIO", ""))
    ap.add_argument("--mic", type=str, default=os.environ.get("SIMIAN_MIC", ""))
    ap.add_argument("--fps", type=int, default=s.fps)
    ap.add_argument("--width", type=int, default=s.width)
    ap.add_argument("--height", type=int, default=s.height)
    ap.add_argument("--list-audio-devices", action="store_true")
    args = ap.parse_args()

    if args.list_audio_devices:
        list_audio_devices()
        return 0

    out = Path(args.out) if args.out else Path(s.clips_dir) / f"recording_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    sysa = args.system_audio.strip() or None
    mic = args.mic.strip() or None

    record(duration=args.duration, out=out, system_audio=sysa, mic=mic, fps=args.fps, width=args.width, height=args.height)
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
