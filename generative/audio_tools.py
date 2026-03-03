# audio_tools.py
import os, time, wave, struct, math, tempfile
from pathlib import Path

def _unique_name(prefix: str, ext: str) -> str:
    return f"{prefix}_{int(time.time()*1000):d}.{'wav' if ext.lower()!='wav' else ext}"

def _ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p

def _sine_beep_wav(out_path: Path, seconds: float = 0.6, freq: int = 600, vol: float = 0.25):
    sr = 22050
    n = int(seconds * sr)
    with wave.open(str(out_path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sr)
        for i in range(n):
            s = int(32767 * vol * math.sin(2 * math.pi * freq * (i / sr)))
            wf.writeframes(struct.pack("<h", s))

def _try_pyttsx3(text: str, out_path: Path, voice: str | None):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        if voice:
            # best effort â€“ only set if the voice name/id exists
            try:
                engine.setProperty("voice", voice)
            except Exception:
                pass
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        return True
    except Exception:
        return False

def synth_to_wav(text: str, out_dir: str | os.PathLike, voice: str | None = None) -> str:
    """
    Create a WAV file for 'text' and return its absolute path.
    Prefers pyttsx3 (offline). Falls back to a short confirmation beep.
    """
    out_dir = _ensure_dir(out_dir)
    out_path = out_dir / _unique_name("tts", "wav")

    # Try real TTS first (offline)
    if _try_pyttsx3(text, out_path, voice):
        return str(out_path.resolve())

    # Fallback: write a short beep so GUI still plays "something"
    _sine_beep_wav(out_path, seconds=0.35, freq=700)
    return str(out_path.resolve())
