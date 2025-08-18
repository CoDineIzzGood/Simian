# mic_listener.py — Simian Voice Command Router (Self-Healing)

import os
import sys
import queue
import json
import threading
import traceback
from datetime import datetime

# Load global auto-installer
from auto_installer import safe_import

# Dependencies
sd = safe_import("sounddevice")
vosk_mod = safe_import("vosk")
Model = getattr(vosk_mod, "Model")
KaldiRecognizer = getattr(vosk_mod, "KaldiRecognizer")

# Crash logger
def log_crash(exc_type, exc_value, exc_traceback):
    os.makedirs("logs", exist_ok=True)
    with open("logs/crash.log", "a", encoding="utf-8") as f:
        f.write("=== MIC LISTENER CRASH ===\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
sys.excepthook = log_crash

# Callbacks
message_callback = None
tts_callback = None
command_callback = None
memory_logger = None

# Model path detection
def get_model_path():
    try:
        base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.abspath(".")
        model_base = os.path.join(base, "voice")
        for folder in os.listdir(model_base):
            if folder.startswith("vosk-model"):
                return os.path.join(model_base, folder)
    except Exception as e:
        print(f"[FATAL] Could not locate Vosk model: {e}")
        traceback.print_exc()
    raise FileNotFoundError("No Vosk model folder found.")

# Setup recognizer
MODEL_PATH = get_model_path()
model = Model(MODEL_PATH)
recognizer = KaldiRecognizer(model, 16000)
audio_queue = queue.Queue()

# Logging / TTS
def log(msg):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    full_msg = f"{timestamp} Simian: {msg}"
    if message_callback:
        message_callback(full_msg)
    else:
        print(full_msg)

def speak(text):
    if tts_callback:
        tts_callback(text)
    else:
        print(f"Simian says: {text}")

# Command routing
def process_command(text):
    if not text.strip():
        return
    cmd = text.lower().strip()
    log(f"You said: {cmd}")
    if memory_logger:
        memory_logger("voice_command", {"text": cmd})

    commands = {
        "clip that": ("Clipping the last 5 minutes.", "clip_that"),
        "start recording": ("Starting screen and audio recording.", "start_recording"),
        "stop recording": ("Stopping the recording now.", "stop_recording"),
        "open spotify": ("Opening Spotify.", "open_spotify"),
        "begin simian": ("Welcome back. I'm online.", "wake_simian"),
        "wake up": ("Welcome back. I'm online.", "wake_simian"),
        "stop listening": ("Voice recognition paused.", None)
    }

    for key, (speech, action) in commands.items():
        if key in cmd:
            speak(speech)
            if action and command_callback:
                command_callback(action)
            return

    speak(f"You said: {text}")

# Listener
def listen_audio():
    try:
        with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16', channels=1,
                               callback=lambda indata, frames, time, status: audio_queue.put(bytes(indata))):
            log("Mic activated (offline Vosk)")
            while True:
                data = audio_queue.get()
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    if "text" in result:
                        process_command(result["text"])
    except Exception as e:
        log(f"Mic listener error: {e}")
        traceback.print_exc()

# Entry
def start_listening():
    threading.Thread(target=listen_audio, daemon=True).start()
    log("Background mic listener started.")
