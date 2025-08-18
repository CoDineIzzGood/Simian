
# simian_launcher.py
import os
import threading
import uvicorn
from fastapi import FastAPI
from routes.chat import router as chat_router

MODE = os.getenv("SIMIAN_MODE", "full")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

def run_api():
    app = FastAPI(title="Simian API", version="1.0.0")
    app.include_router(chat_router)
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")

def run_gui():
    from gui.simian_gui import run as run_gui
    run_gui(f"http://{APP_HOST}:{APP_PORT}")

if __name__ == "__main__":
    if MODE in ("api", "full"):
        t = threading.Thread(target=run_api, daemon=True)
        t.start()
    if MODE in ("gui", "full"):
        run_gui()
