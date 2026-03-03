"""Voice module public interface.

Provides thin wrappers so older imports continue to work even though the
underlying helpers now live in dedicated modules.
"""
from __future__ import annotations
from typing import Optional

from .edge_tts_speak import speak_text as _speak_text
from .recognizer import recognize_speech as _recognize_speech, transcribe_from_mic as _transcribe_from_mic

__all__ = [
    "recognize_command",
    "recognize_speech",
    "transcribe_from_mic",
    "speak_text",
]


def recognize_command(timeout: float = 5.0, phrase_time_limit: float = 10.0) -> str:
    """Backward-compatible alias for recognize_speech()."""
    return _recognize_speech(timeout=timeout, phrase_time_limit=phrase_time_limit)


def recognize_speech(timeout: float = 5.0, phrase_time_limit: float = 10.0) -> str:
    """Expose recognizer.recognize_speech via the voice namespace."""
    return _recognize_speech(timeout=timeout, phrase_time_limit=phrase_time_limit)


def transcribe_from_mic(timeout: float = 5.0, phrase_time_limit: float = 10.0) -> str:
    """Expose recognizer.transcribe_from_mic via the voice namespace."""
    return _transcribe_from_mic(timeout=timeout, phrase_time_limit=phrase_time_limit)


def speak_text(text: str, voice_id: Optional[str] = None, voice: Optional[str] = None) -> str:
    """Speak text with Edge TTS.

    Backwards compatible:
      - older code passes voice_id=
      - newer GUI code passes voice=
    """
    chosen = voice_id or voice
    return _speak_text(text=text, voice_id=chosen)
