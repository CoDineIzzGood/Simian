from fastapi import APIRouter, Body, HTTPException
from typing import Optional

router = APIRouter()

@router.post("/txt2img")
async def txt2img(prompt: str = Body(..., embed=True), seed: Optional[int] = Body(None, embed=True)):
    # Try to delegate to services.image_gen if available
    try:
        from services.image_gen import txt2img_generate  # type: ignore
        path = txt2img_generate(prompt, seed=seed)
        return {"status": "ok", "path": path}
    except Exception as e:
        # Fallback stub
        return {"status": "stub", "message": f"txt2img route loaded but generator unavailable: {e}", "prompt": prompt, "seed": seed}
