from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path


def synthesize_to_file(text: str, voice: str = "en-US-GuyNeural") -> str:
    out_dir = Path("data/generated/audio")
    out_dir.mkdir(parents=True, exist_ok=True)

    if text is None or not str(text).strip():
        raise RuntimeError("No text provided for TTS synthesis")

    # edge-tts saves compressed audio even if the extension says .wav, so use .mp3 here.
    mp3_path = out_dir / f"tts_{int(time.time()*1000)}.mp3"
    wav_path = out_dir / f"tts_{int(time.time()*1000)}.wav"

    try:
        import edge_tts  # type: ignore

        async def _run() -> None:
            communicate = edge_tts.Communicate(text=text, voice=voice)
            await communicate.save(str(mp3_path))

        asyncio.run(_run())
        if mp3_path.exists() and mp3_path.stat().st_size > 512:
            return str(mp3_path.resolve())
    except Exception:
        pass

    try:
        import pyttsx3
        engine = pyttsx3.init()
        try:
            engine.setProperty("voice", voice)
        except Exception:
            pass
        engine.save_to_file(text, str(wav_path))
        engine.runAndWait()
        if wav_path.exists() and wav_path.stat().st_size > 512:
            return str(wav_path.resolve())
    except Exception:
        pass

    if os.name == "nt":
        try:
            safe_path = str(wav_path).replace("'", "''")
            safe_text = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{safe_path}'); "
                f"$s.Speak('{safe_text}'); "
                "$s.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if wav_path.exists() and wav_path.stat().st_size > 512:
                return str(wav_path.resolve())
        except Exception:
            pass

    raise RuntimeError("No working TTS backend could synthesize audio to a file")
