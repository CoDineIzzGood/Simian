from __future__ import annotations
import time
from pathlib import Path
from typing import Optional

from edge_tts_speak import synthesize_to_wav

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "data" / "generated" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

def speak_text(text: str, voice_id: Optional[str] = None) -> Path:
    """
    Synthesize text to a .wav file and return the path.
    """
    ts = int(time.time() * 1000)
    out_path = AUDIO_DIR / f"tts_{ts}.wav"
    synthesize_to_wav(text, out_path, voice_id=voice_id)
    return out_path
