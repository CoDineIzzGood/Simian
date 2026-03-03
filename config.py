# Simian/config.py
from pathlib import Path
import os

# --- Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
GEN_OUTPUT_DIR = DATA_DIR / "generated"
IMAGES_DIR = GEN_OUTPUT_DIR / "images"
AUDIO_DIR = GEN_OUTPUT_DIR / "audio"
VIDEO_DIR = GEN_OUTPUT_DIR / "videos"
SANDBOX_DIR = BASE_DIR / "generative" / "sandbox"

for p in (DATA_DIR, GEN_OUTPUT_DIR, IMAGES_DIR, AUDIO_DIR, VIDEO_DIR, SANDBOX_DIR):
    p.mkdir(parents=True, exist_ok=True)

# --- Backends / models
SIMIAN_MODEL = os.getenv("SIMIAN_MODEL", "simian")        # stays 'simian' unless you change it
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
A1111_URL    = os.getenv("A1111_URL", "http://127.0.0.1:7860")
DIFFUSERS_ENABLED = os.getenv("DIFFUSERS_ENABLED", "0") in ("1", "true", "True")
SD21_MODEL = os.getenv("SD21_MODEL", "stabilityai/stable-diffusion-2-1")
SDXL_MODEL = os.getenv("SDXL_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
DIFFUSERS_MODEL = os.getenv("DIFFUSERS_MODEL") or SD21_MODEL

# --- App host/port (read by launcher/GUI)
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
