from fastapi import APIRouter, Body
from typing import Optional

router = APIRouter()

@router.post("/video")
async def generate_video(prompt: str = Body(..., embed=True), seconds: int = Body(4, embed=True)):
    try:
        from services.video_gen import video_generate  # type: ignore
        out = video_generate(prompt, seconds=seconds)
        return {"status": "ok", "path": out}
    except Exception as e:
        return {"status": "stub", "message": f"video route loaded but generator unavailable: {e}", "prompt": prompt, "seconds": seconds}
