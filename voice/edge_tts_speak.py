from __future__ import annotations
from pathlib import Path
from typing import Optional
import asyncio
import logging
import tempfile

try:
    import edge_tts  # type: ignore
except Exception:  # pragma: no cover - edge-tts optional
    edge_tts = None  # type: ignore

logger = logging.getLogger("simian.voice.tts")

# --- Internal async synthesis ---
async def _async_synth(text: str, out_path: Path, voice_id: Optional[str]):
    if edge_tts is None:
        raise RuntimeError("edge-tts is not available")
    tts = edge_tts.Communicate(text, voice=voice_id or "en-US-JennyNeural")
    with open(out_path, "wb") as f:
        async for chunk in tts.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def _pyttsx3_fallback(text: str, out_path: Path, voice_id: Optional[str]) -> bool:
    try:
        import pyttsx3  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dep
        logger.debug("pyttsx3 unavailable: %s", exc)
        return False

    try:
        engine = pyttsx3.init()
        if voice_id:
            for v in engine.getProperty("voices"):
                if voice_id.lower() in (v.id.lower(), v.name.lower()):
                    engine.setProperty("voice", v.id)
                    break
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as exc:
        logger.warning("pyttsx3 synthesis failed: %s", exc)
        return False


def synthesize_to_wav(text: str, out_path: Path, voice_id: Optional[str] = None) -> None:
    """Generate a WAV file at out_path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if edge_tts is not None:
        try:
            asyncio.run(_async_synth(text, out_path, voice_id))
            return
        except Exception as exc:
            logger.warning("edge-tts synthesis failed: %s", exc)

    if _pyttsx3_fallback(text, out_path, voice_id):
        return

    raise RuntimeError("No TTS backend succeeded")


def speak_text(text: str, voice_id: Optional[str] = None) -> str:
    """
    High-level wrapper: takes text and optional voice_id,
    writes to a temporary WAV file, and returns its path.
    """
    tmp_dir = Path(tempfile.gettempdir())
    out_path = tmp_dir / "simian_tts.wav"
    synthesize_to_wav(text, out_path, voice_id)
    return str(out_path)
