
import os
import threading
import uvicorn

def run_api():
    from routes.chat import app
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")

def run_gui():
    from gui.simian_gui import run
    api_base = f"http://{os.getenv('APP_HOST','127.0.0.1')}:{os.getenv('APP_PORT','8000')}"
    run(api_base=api_base)

if __name__ == "__main__":
    mode = os.getenv("SIMIAN_MODE","full").lower()
    if mode == "api":
        run_api()
    elif mode == "gui":
        run_gui()
    else:
        t = threading.Thread(target=run_api, daemon=True)
        t.start()
        run_gui()
