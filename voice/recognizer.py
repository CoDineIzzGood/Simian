# voice/recognizer.py
# Simple, dependable mic recognizer.
# Tries SpeechRecognition first; if unavailable, falls back to Vosk (if model present).

import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger("simian.voice")

def recognize_speech(timeout: float = 5.0, phrase_time_limit: float = 10.0) -> str:
    """Return transcribed text or '(error) ...' string."""
    # Try SpeechRecognition (pip install SpeechRecognition)
    try:
        import speech_recognition as sr
        r = sr.Recognizer()
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.5)
            audio = r.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        try:
            return r.recognize_google(audio)  # offline alt below
        except sr.UnknownValueError:
            return "(couldnâ€™t catch that)"
        except Exception as e:
            return f"(error) {e}"
    except Exception as e:
        logger.debug(f"SpeechRecognition unavailable, falling back to Vosk: {e}")

    # Fallback: Vosk (offline)
    try:
        from vosk import Model, KaldiRecognizer
        import pyaudio, json

        # auto-find a vosk model folder under voice/
        root = Path(__file__).resolve().parent
        model_dir = next((p for p in root.glob("vosk-model-*") if p.is_dir()), None)
        if not model_dir:
            return "(error) no Vosk model found in voice/"

        model = Model(str(model_dir))
        rec = KaldiRecognizer(model, 16000)

        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                         frames_per_buffer=8000)
        stream.start_stream()

        collected = []
        total_ms = 0
        while total_ms < int(phrase_time_limit * 1000):
            data = stream.read(4000, exception_on_overflow=False)
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                collected.append(res.get("text", ""))
                break
            total_ms += 250

        stream.stop_stream()
        stream.close()
        pa.terminate()

        text = " ".join(collected).strip()
        return text or "(couldnâ€™t catch that)"
    except Exception as e:
        return f"(error) {e}"

# --- compatibility shim ---
def transcribe_from_mic(timeout: float = 5.0, phrase_time_limit: float = 10.0) -> str:
    """Provide the newer name by delegating to recognize_speech()."""
    try:
        return recognize_speech(timeout=timeout, phrase_time_limit=phrase_time_limit)
    except Exception as e:
        logger.error(f"transcribe_from_mic failed: {e}")
        return ""
