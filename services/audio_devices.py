"""
Utility to list Windows DirectShow audio device names for FFmpeg.

Run:
  python -m services.audio_devices
"""
from __future__ import annotations

import subprocess
import sys

from services.replay_buffer import _find_ffmpeg


def main() -> int:
    ffmpeg = _find_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate()
    print(out)
    print("\nTip: Copy the exact device name into Settings -> Replay Buffer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
