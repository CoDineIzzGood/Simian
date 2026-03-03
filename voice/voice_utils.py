# voice/voice_utils.py
from voice.edge_tts_speak import speak as edge_tts_speak

def edge_voice_choices():
    # Minimal, known-good voices. You can extend later.
    return [
        "auto",
        "en-US-GuyNeural",
        "en-US-JennyNeural",
        "en-GB-RyanNeural",
        "en-GB-SoniaNeural",
        "en-AU-NatashaNeural",
    ]

def label_to_id(label: str) -> str | None:
    return None if label.lower() == "auto" else label

def split_for_tts(text: str, max_len: int = 600):
    # Split on paragraph/period boundaries to avoid very long TTS buffers
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if len(buf) >= max_len and ch in ".!?":
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts

def tts_speak_full(text: str, voice_label: str):
    voice = label_to_id(voice_label)
    for chunk in split_for_tts(text):
        edge_tts_speak(chunk, voice)
