from fastapi import APIRouter, Request
from pydantic import BaseModel
import subprocess
import threading

from modules import screen_recorder, gui_toggle, context_memory, ml_model

router = APIRouter()

class TextInput(BaseModel):
    text: str

# --- Screen Recorder Routes ---
@router.post("/record/start")
def start_screen_recording():
    threading.Thread(target=screen_recorder.start_recording).start()
    return {"status": "recording started"}

@router.post("/record/stop")
def stop_screen_recording():
    screen_recorder.stop_recording()
    return {"status": "recording stopped"}

# --- GUI Toggle ---
@router.get("/gui/toggle")
def toggle_gui():
    threading.Thread(target=gui_toggle.toggle_gui).start()
    return {"status": "GUI toggled"}

# --- Context Learning ---
@router.post("/context/save")
def save_context(input_data: TextInput):
    context_memory.save_context(input_data.text)
    return {"status": "context saved"}

@router.get("/context/load")
def load_context():
    return {"context": context_memory.load_context()}

# --- ML Inference ---
@router.post("/ml/classify")
def classify_input(input_data: TextInput):
    result = ml_model.classify(input_data.text)
    return {"classification": result}

# --- CLI Command Example ---
@router.post("/cli/run")
def run_cli_command(input_data: TextInput):
    try:
        output = subprocess.check_output(input_data.text, shell=True)
        return {"output": output.decode("utf-8")}
    except subprocess.CalledProcessError as e:
        return {"error": str(e)}

# --- Voice Toggle Simulation ---
@router.post("/voice/toggle")
def toggle_voice():
    print("[Voice] Voice output toggled.")
    return {"status": "voice toggled"}
