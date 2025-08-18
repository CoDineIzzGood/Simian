# listener_bootstrap.py
# Lightweight Vosk-only voice wake trigger for "Simian, begin"

import os
import sys
import queue
import json
import subprocess
import traceback
from vosk import Model, KaldiRecognizer
import sounddevice as sd
import difflib

CONFIG_PATH = "config/listener_config.json"
DEFAULT_WAKE_PHRASES = [
    "simian begin",
    "simeon begin",
    "simian",
    "begin simian",
    "semyon began",
    "simeon began",
    "begin semyon"
]

DEFAULT_CONFIG = {
    "wake_phrases": DEFAULT_WAKE_PHRASES,
    "boot_mode": "full",
    "confirmation_voice": "goodmorning",
    "auth_mode": "none"  # future support: "face", "voice", "combined"
}

# ------------------ Crash Logging ------------------ #
def log_crash(exc_type, exc_value, exc_traceback):
    os.makedirs("logs", exist_ok=True)
    with open("logs/bootstrap_crash.log", "a", encoding="utf-8") as f:
        f.write("=== BOOTSTRAP CRASH ===\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = log_crash

# ------------------ Dynamic Vosk Path ------------------ #
def get_model_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        model_root = os.path.join(base_path, "voice")
    else:
        model_root = os.path.join("voice")

    for folder in os.listdir(model_root):
        if folder.startswith("vosk-model"):
            return os.path.join(model_root, folder)

    raise FileNotFoundError("[BOOTSTRAP] No Vosk model found.")

# ------------------ Load or Create Listener Config ------------------ #
def load_config():
    os.makedirs("config", exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        print("[BOOTSTRAP] Created default listener_config.json")

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[BOOTSTRAP] Failed to load config: {e}")
        return DEFAULT_CONFIG

# ------------------ Fuzzy Match Wake Phrase ------------------ #
def is_match(transcript, wake_phrases):
    close = difflib.get_close_matches(transcript, wake_phrases, n=1, cutoff=0.75)
    return bool(close)

# ------------------ Simian Confirmation Voice ------------------ #
def speak_confirmation(voice_phrase):
    try:
        from gui.voice.edge_tts_speak import speak
        speak(voice_phrase)
    except Exception as e:
        print(f"[BOOTSTRAP] Failed to speak confirmation: {e}")

# ------------------ Main Wake Listener ------------------ #
def wait_for_wake():
    config = load_config()
    wake_phrases = config.get("wake_phrases", DEFAULT_WAKE_PHRASES)
    confirmation_voice = config.get("confirmation_voice", "goodmorning")
    boot_mode = config.get("boot_mode", "full")
    auth_mode = config.get("auth_mode", "none")  # placeholder for future

    model_path = get_model_path()
    model = Model(model_path)
    recognizer = KaldiRecognizer(model, 16000)
    audio_queue = queue.Queue()

    print("[BOOTSTRAP] Listening for wake phrase...")

    def callback(indata, frames, time, status):
        audio_queue.put(bytes(indata))

    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16', channels=1, callback=callback):
        while True:
            data = audio_queue.get()
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                if 'text' in result:
                    transcript = result['text'].strip().lower()
                    print(f"[BOOTSTRAP] Heard: {transcript}")
                    if is_match(transcript, wake_phrases):
                        print("[BOOTSTRAP] Wake phrase detected.")
                        speak_confirmation(confirmation_voice)
                        launch_simian(boot_mode)
                        break

# ------------------ Launch Main Assistant ------------------ #
def launch_simian(boot_mode):
    try:
        cmd = ["simian_launcher.py"]
        if getattr(sys, 'frozen', False):
            exe_path = os.path.join("dist", "Simian", "Simian.exe")
            cmd = [exe_path, f"--mode={boot_mode}"]
        else:
            cmd.append(f"--mode={boot_mode}")

        subprocess.Popen(cmd, shell=True)
    except Exception as e:
        print(f"[BOOTSTRAP] Failed to launch Simian: {e}")
        traceback.print_exc()

# ------------------ Entry ------------------ #
if __name__ == "__main__":
    wait_for_wake()