from fastapi import APIRouter, Body
from typing import Optional

router = APIRouter()

@router.post("/tts")
async def tts_generate(text: str = Body(..., embed=True), voice: str = Body("en-US-GuyNeural", embed=True)):
    try:
        from services.tts_edge import synthesize_to_file  # type: ignore
        path = synthesize_to_file(text=text, voice=voice)
        return {"status": "ok", "path": path}
    except Exception as e:
        return {"status": "stub", "message": f"tts route loaded but tts service unavailable: {e}", "text": text, "voice": voice}
