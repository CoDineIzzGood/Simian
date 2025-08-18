
from fastapi import APIRouter
import subprocess
import pathlib
import threading
import sys

router = APIRouter()

# Global to track process
_gui_proc = None

@router.post("/gui/start")
def start_gui():
    global _gui_proc
    if _gui_proc and _gui_proc.poll() is None:
        return {"status": "GUI already running"}

    gui_path = pathlib.Path(__file__).parent.parent / "gui" / "simian_gui.py"
    _gui_proc = subprocess.Popen([sys.executable, str(gui_path)])
    return {"status": f"Started GUI (PID: {_gui_proc.pid})"}

@router.post("/gui/stop")
def stop_gui():
    global _gui_proc
    if _gui_proc and _gui_proc.poll() is None:
        _gui_proc.terminate()
        _gui_proc.wait(timeout=5)
        return {"status": "GUI stopped"}
    return {"status": "GUI was not running"}
