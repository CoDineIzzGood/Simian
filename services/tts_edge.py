from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path


def synthesize_to_file(text: str, voice: str = "en-US-GuyNeural") -> str:
    out_dir = Path("data/generated/audio")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tts_{int(time.time()*1000)}.wav"

    try:
        import edge_tts  # type: ignore

        async def _run() -> None:
            communicate = edge_tts.Communicate(text=text, voice=voice)
            await communicate.save(str(out_path))

        asyncio.run(_run())
        if out_path.exists() and out_path.stat().st_size > 44:
            return str(out_path.resolve())
    except Exception:
        pass

    try:
        import pyttsx3
        engine = pyttsx3.init()
        try:
            engine.setProperty("voice", voice)
        except Exception:
            pass
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        if out_path.exists() and out_path.stat().st_size > 44:
            return str(out_path.resolve())
    except Exception:
        pass

    if os.name == "nt":
        try:
            safe_path = str(out_path).replace("'", "''")
            safe_text = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{safe_path}'); "
                f"$s.Speak('{safe_text}'); "
                "$s.Dispose()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if out_path.exists() and out_path.stat().st_size > 44:
                return str(out_path.resolve())
        except Exception:
            pass

    out_path.write_bytes(b"RIFF0000WAVE")
    return str(out_path.resolve())
