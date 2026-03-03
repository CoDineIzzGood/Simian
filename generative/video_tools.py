from pathlib import Path
from typing import Optional
import subprocess, shlex
from .config import VIDEO_DIR

FFMPEG = "ffmpeg"

def _run(cmd: str):
    subprocess.run(cmd, shell=True, check=True)

async def generate_video(prompt: str, duration_s: int = 4, fps: int = 8, backend: Optional[str] = None) -> Optional[Path]:
    """
    Placeholder text-to-video: draws the prompt on a black clip so the pipeline is testable end-to-end.
    Later, swap to SVD/Deforum.
    """
    out = VIDEO_DIR / "txt2vid_placeholder.mp4"
    safe_text = str(prompt).replace("'", r"\'")
    cmd = (
        FFMPEG + " -y -f lavfi -i color=c=black:s=768x432:d=" + str(duration_s) +
        " -vf drawtext=text='" + safe_text +
        "':x=(w-tw)/2:y=(h-th)/2:fontsize=32:fontcolor=white -r " + str(fps) +
        " " + shlex.quote(str(out))
    )
    _run(cmd)
    return out

async def caption_video(video_path: str, caption: str) -> Optional[Path]:
    src = Path(video_path)
    if not src.exists():
        return None
    out = src.with_name(src.stem + "_captioned.mp4")
    safe_text = str(caption).replace("'", r"\'")
    cmd = (
        FFMPEG + " -y -i " + shlex.quote(str(src)) +
        " -vf drawtext=text='" + safe_text +
        "':x=(w-tw)/2:y=h-80:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5 " +
        shlex.quote(str(out))
    )
    _run(cmd)
    return out
