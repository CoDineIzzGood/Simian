from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from services.image_gen import txt2img_generate


def video_generate(prompt: str, seconds: int = 4) -> str:
    out_dir = Path("data/generated/video")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(txt2img_generate(prompt))
    out = out_dir / f"vid_{int(time.time()*1000)}.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")
    cmd = [
        ffmpeg, "-y",
        "-loop", "1",
        "-i", str(base),
        "-vf", "scale=1280:720,format=yuv420p",
        "-t", str(max(1, int(seconds))),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(out.resolve())
