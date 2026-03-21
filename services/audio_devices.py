"""
Audio device helpers for Simian.
- Lists sounddevice input/output devices for STT/TTS routing
- Lists FFmpeg DirectShow device names for replay buffer capture
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

try:
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None

REPO_ROOT = Path(__file__).resolve().parents[1]
FFMPEG_CANDIDATES = [
    REPO_ROOT / "ffmpeg.exe",
    REPO_ROOT / "bin" / "ffmpeg.exe",
    REPO_ROOT / "tools" / "ffmpeg.exe",
    Path("ffmpeg"),
]


def _find_ffmpeg() -> str:
    for candidate in FFMPEG_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"


def list_sounddevice_devices() -> Dict[str, List[Dict[str, Any]]]:
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    if sd is None:
        return {"inputs": inputs, "outputs": outputs}

    default_pair: tuple[Optional[int], Optional[int]] = (None, None)
    try:
        raw_default = getattr(sd, "default", None)
        if raw_default is not None and getattr(raw_default, "device", None) is not None:
            dev_pair = raw_default.device
            if isinstance(dev_pair, (list, tuple)) and len(dev_pair) >= 2:
                default_pair = (cast(Optional[int], dev_pair[0]), cast(Optional[int], dev_pair[1]))
    except Exception:
        pass

    default_in, default_out = default_pair

    try:
        raw_devices = cast(List[Any], sd.query_devices())
    except Exception:
        raw_devices = []

    for idx, raw in enumerate(raw_devices):
        info: Dict[str, Any] = dict(cast(Dict[str, Any], raw))
        entry: Dict[str, Any] = {
            "index": idx,
            "name": str(info.get("name", f"Device {idx}")),
            "max_input_channels": int(info.get("max_input_channels", 0) or 0),
            "max_output_channels": int(info.get("max_output_channels", 0) or 0),
            "default_samplerate": int(float(info.get("default_samplerate", 0) or 0)),
        }
        if entry["max_input_channels"] > 0:
            entry["default"] = idx == default_in
            inputs.append(entry)
        if entry["max_output_channels"] > 0:
            entry["default"] = idx == default_out
            outputs.append(entry)

    return {"inputs": inputs, "outputs": outputs}


def list_dshow_audio_devices() -> List[str]:
    ffmpeg = _find_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    out, _ = proc.communicate()
    names: List[str] = []
    in_audio_section = False

    for line in out.splitlines():
        low = line.lower()
        if "directshow audio devices" in low:
            in_audio_section = True
            continue
        if "directshow video devices" in low:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue

        match = re.search(r'"([^"]+)"', line)
        if match:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)

    return names


def main() -> int:
    sd_info = list_sounddevice_devices()
    print("Sounddevice input devices:")
    for dev in sd_info["inputs"]:
        mark = " *default" if dev.get("default") else ""
        print(f"  {dev['index']}: {dev['name']}{mark}")

    print("\nSounddevice output devices:")
    for dev in sd_info["outputs"]:
        mark = " *default" if dev.get("default") else ""
        print(f"  {dev['index']}: {dev['name']}{mark}")

    print("\nFFmpeg DirectShow audio names:")
    for name in list_dshow_audio_devices():
        print(f"  {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
