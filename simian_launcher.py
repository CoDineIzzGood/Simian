import os
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.chat import router as chat_router

APP_PORT = int(os.environ.get("SIMIAN_PORT", "8000"))
APP_HOST = os.environ.get("SIMIAN_HOST", "127.0.0.1")
SIMIAN_MODE = os.environ.get("SIMIAN_MODE", "full").lower()  # full|api|gui

app = FastAPI(title="Simian API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api")

def run_api():
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")

def run_gui():
    try:
        from gui.simian_gui import run as run_simian_gui
        run_simian_gui(f"http://{APP_HOST}:{APP_PORT}")
    except Exception as e:
        print(f"[WARNING] No GUI available. ({e})")

if __name__ == "__main__":
    print("[BOOT] Mode:", SIMIAN_MODE)
    if SIMIAN_MODE in ("full", "api"):
        api_thread = threading.Thread(target=run_api, daemon=True)
        api_thread.start()
    if SIMIAN_MODE in ("full", "gui"):
        try:
            webbrowser.open(f"http://{APP_HOST}:{APP_PORT}/docs", new=0)
        except Exception:
            pass
        run_gui()
    else:
        run_api()
