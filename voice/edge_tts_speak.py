# voice/edge_tts_speak.py
import os, tempfile, subprocess, logging

# First choice: pyttsx3 (offline, reliable in EXEs)
try:
    import pyttsx3
except Exception:
    pyttsx3 = None

# Fallback chain: edge-tts -> mp3 -> ffmpeg -> wav -> simpleaudio
try:
    import edge_tts
except Exception:
    edge_tts = None
try:
    import simpleaudio as sa
except Exception:
    sa = None

def _ffmpeg():
    return "ffmpeg"

def speak(text: str, voice: str = "en-US-GuyNeural"):
    # Try pyttsx3 first
    if pyttsx3:
        try:
            eng = pyttsx3.init()
            eng.say(text)
            eng.runAndWait()
            return
        except Exception as e:
            logging.info(f"[TTS] pyttsx3 error: {e}")

    # Fallback: edge-tts -> play via ffmpeg+simpleaudio
    if not edge_tts or not sa:
        logging.info("[TTS] edge_tts/simpleaudio not available.")
        return

    import asyncio
    async def _run():
        try:
            with tempfile.TemporaryDirectory() as td:
                mp3 = os.path.join(td, "tts.mp3")
                wav = os.path.join(td, "tts.wav")

                comm = edge_tts.Communicate(text, voice=voice)
                await comm.save(mp3)

                # Convert mp3 -> wav (PCM) for simpleaudio
                subprocess.run([_ffmpeg(), "-y", "-i", mp3, "-acodec", "pcm_s16le", "-ar", "24000", wav],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

                wave_obj = sa.WaveObject.from_wave_file(wav)
                play_obj = wave_obj.play()
                play_obj.wait_done()
        except Exception as e:
            logging.info(f"[TTS] fallback error: {e}")

    try:
        asyncio.run(_run())
    except RuntimeError:
        asyncio.get_event_loop().create_task(_run())
