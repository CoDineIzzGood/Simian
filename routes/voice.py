
from fastapi import APIRouter
from voice.recognizer import recognize_command
from voice.edge_tts_speak import speak

router = APIRouter()

@router.get("/voice")
async def listen_and_speak():
    command = recognize_command()
    speak_text(f"You said: {command}")
    return {"response": command}
