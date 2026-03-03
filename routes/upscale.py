from fastapi import APIRouter, Body
from typing import Optional

router = APIRouter()

@router.post("/upscale")
async def upscale_image(input_path: str = Body(..., embed=True), target_w: int = Body(1920, embed=True), target_h: int = Body(1080, embed=True)):
    try:
        # If you later add a service util, import & call it here.
        return {"status": "stub", "message": "upscale stub; add real implementation in services", "input_path": input_path, "target_w": target_w, "target_h": target_h}
    except Exception as e:
        return {"status": "error", "error": str(e)}
