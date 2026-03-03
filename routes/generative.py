# routes/generative.py
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field, validator

# -----------------------------------------------------------------------------
# Paths & folders
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent                    # .../routes
ROOT_DIR = BASE_DIR.parent                                    # repo root
DATA_DIR = (ROOT_DIR / "data")
GEN_DIR = DATA_DIR / "generated"
IMG_DIR = GEN_DIR / "images"
CLIP_DIR = GEN_DIR / "clips"
AUD_DIR = GEN_DIR / "audio"
for d in (DATA_DIR, GEN_DIR, IMG_DIR, CLIP_DIR, AUD_DIR):
    d.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# Soft imports for your local tools (optional & non-fatal)
# -----------------------------------------------------------------------------
_img_tool = None
_up_tool = None
_tts_tool = None
try:
    # if you kept these under generative/, adjust import to: from generative.image_tools ...
    from generative.image_tools import txt2img_generate as _img_tool
except Exception:
    pass

try:
    from generative.image_tools import upscale_image as _up_tool  # optional helper if you have it
except Exception:
    pass

try:
    from generative.audio_tools import tts_generate_and_play as _tts_tool
except Exception:
    pass


# -----------------------------------------------------------------------------
# Pydantic models (Swagger will render a JSON body box now)
# -----------------------------------------------------------------------------
class GenResponse(BaseModel):
    file: str
    path: str
    rel_path: str
    saved: Optional[str] = None


class Txt2ImgRequest(BaseModel):
    prompt: str = Field(..., examples=["neon rain alley, cinematic"])
    engine: Optional[str] = Field("auto", examples=["auto"])
    width: int = Field(768, ge=64, le=1920)
    height: int = Field(768, ge=64, le=1920)
    steps: int = Field(30, ge=1, le=200)
    cfg_scale: float = Field(7.5, ge=1, le=20)


class UpscaleRequest(BaseModel):
    input_path: str = Field(..., description="Absolute path to input image")
    target_w: int = Field(1920, ge=16, le=4096)
    target_h: int = Field(1080, ge=16, le=4096)
    prefer: str = Field("auto", description='"auto" | "onnx" | "pil"')
    onnx_model_path: Optional[str] = Field(None, description="Local .onnx model file")
    onnx_scale: Optional[int] = Field(2, description="If using ONNX, scale factor (e.g., 2/3/4)")

    @validator("prefer")
    def _valid_prefer(cls, v: str) -> str:
        v = (v or "auto").lower()
        if v not in {"auto", "onnx", "pil"}:
            raise ValueError('prefer must be one of: "auto", "onnx", "pil"')
        return v


class VideoRequest(BaseModel):
    prompt: str = Field(..., examples=["neon waterfall, cinematic"])
    seconds: int = Field(10, ge=1, le=60)
    preset: str = Field("hd", description="future: quality preset selector")
    fps: int = Field(24, ge=1, le=60)
    width: int = Field(1920, ge=64, le=4096)
    height: int = Field(1080, ge=64, le=4096)


# -----------------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------------
router = APIRouter(prefix="/api", tags=["gen"])


@router.get("/health")
def api_health():
    return {"ok": True}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _ts() -> str:
    # 13-digit millisecond ts to match the style you've been seeing
    return str(int(datetime.now().timestamp() * 1000))


def _resp(path: Path) -> GenResponse:
    """
    Build GUI-friendly response. Also returns rel_path routed under /generated
    (make sure main.py mounts StaticFiles at /generated -> data/generated).
    """
    path = path.resolve()
    # compute relative path under GEN_DIR
    try:
        rel = "/" + str(path.relative_to(GEN_DIR)).replace("\\", "/")
    except Exception:
        # not inside GEN_DIR; just expose filename
        rel = "/" + path.name
    return GenResponse(
        file=str(path),
        path=str(path),
        rel_path=f"/generated{rel}",
        saved=str(path),
    )


async def _extract_payload(request: Request) -> dict:
    """
    Fallback extractor so raw JSON or form still works with curl, etc.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.body()
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {}
    elif "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        form = await request.form()
        return {k: v for k, v in form.items()}
    return {}


def _ffmpeg_ok() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False


def _placeholder_image(path: Path, text: str, w: int, h: int) -> Path:
    from PIL import Image, ImageDraw
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (w, h), (10, 12, 14))
    dr = ImageDraw.Draw(im)
    dr.text((20, 20), text[:120], fill=(200, 200, 220))
    im.save(path)
    return path


# -----------------------------------------------------------------------------
# TXT2IMG
# -----------------------------------------------------------------------------
@router.post(
    "/gen/txt2img",
    response_model=GenResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "prompt": "neon rain alley, cinematic",
                        "engine": "auto",
                        "width": 1024,
                        "height": 576,
                        "steps": 36,
                        "cfg_scale": 8.0,
                    }
                }
            }
        }
    },
)
async def api_gen_txt2img(request: Request, body: Optional[Txt2ImgRequest] = Body(default=None)):
    payload = body.model_dump() if body else await _extract_payload(request)

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return {"detail": 'txt2img_error: prompt is required'}, 422  # Swagger shows nicely

    w = int(payload.get("width", 768))
    h = int(payload.get("height", 768))
    engine = (payload.get("engine") or "auto").strip()

    out = IMG_DIR / f"img_{_ts()}.png"

    if _img_tool:
        try:
            # Your local generator should accept these; adjust if needed
            _img_tool(prompt=prompt, backend=engine or None, width=w, height=h, out_path=str(out))
            return _resp(out)
        except Exception:
            # graceful fallback
            _placeholder_image(out, f"prompt: {prompt}", w, h)
            return _resp(out)
    else:
        _placeholder_image(out, f"prompt: {prompt}", w, h)
        return _resp(out)


# -----------------------------------------------------------------------------
# UPSCALE
# -----------------------------------------------------------------------------
@router.post(
    "/gen/upscale",
    response_model=GenResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "input_path": r"C:\Users\Alexander Munson\OneDrive\Desktop\Project C.H.I.M.P\Simian\data\generated\images\img_1761511012008.png",
                        "target_w": 1920,
                        "target_h": 1080,
                        "prefer": "auto",
                        "onnx_model_path": None,
                        "onnx_scale": 2,
                    }
                }
            }
        }
    },
)
async def api_gen_upscale(request: Request, body: Optional[UpscaleRequest] = Body(default=None)):
    payload = body.model_dump() if body else await _extract_payload(request)

    input_path = Path(payload.get("input_path") or "")
    if not input_path.exists():
        return {"detail": "upscale_error: input_path not found"}, 422

    tw = int(payload.get("target_w", 1920))
    th = int(payload.get("target_h", 1080))
    prefer = (payload.get("prefer") or "auto").lower()
    onnx_model = payload.get("onnx_model_path")
    onnx_scale = int(payload.get("onnx_scale", 2))

    out = IMG_DIR / f"up_{_ts()}.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Path A: use your ONNX/quality upscaler if present & requested
    if prefer == "onnx" and _up_tool and onnx_model:
        try:
            _up_tool(
                input_path=str(input_path),
                out_path=str(out),
                target_w=tw,
                target_h=th,
                prefer="onnx",
                onnx_model_path=onnx_model,
                onnx_scale=onnx_scale,
            )
            return _resp(out)
        except Exception:
            # fall through to PIL upscale
            pass

    # Path B: high-quality PIL Lanczos (local-only fallback)
    try:
        from PIL import Image
        im = Image.open(str(input_path)).convert("RGB")
        im = im.resize((tw, th), resample=Image.LANCZOS)
        im.save(str(out))
        return _resp(out)
    except Exception as e:
        return {"detail": f"upscale_error: {e}"}, 422


# -----------------------------------------------------------------------------
# VIDEO (simple 1080p or chosen size demo)
#   1) create a key image (via your txt2img or placeholder)
#   2) Ken-Burns style pan/zoom (ffmpeg filter)
#   3) encode to mp4
# -----------------------------------------------------------------------------
@router.post(
    "/gen/video",
    response_model=GenResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "prompt": "neon waterfall, cinematic",
                        "seconds": 12,
                        "preset": "hd",
                        "fps": 24,
                        "width": 1920,
                        "height": 1080,
                    }
                }
            }
        }
    },
)
async def api_gen_video(request: Request, body: Optional[VideoRequest] = Body(default=None)):
    payload = body.model_dump() if body else await _extract_payload(request)

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return {"detail": "video_error: prompt is required"}, 422

    seconds = int(payload.get("seconds", 10))
    fps = int(payload.get("fps", 24))
    width = int(payload.get("width", 1920))
    height = int(payload.get("height", 1080))

    if not _ffmpeg_ok():
        return {"detail": "video_error: ffmpeg not available in PATH"}, 422

    # 1) create base image
    base_img = IMG_DIR / f"vid_base_{_ts()}.png"
    if _img_tool:
        try:
            _img_tool(prompt=prompt, backend="auto", width=width, height=height, out_path=str(base_img))
        except Exception:
            _placeholder_image(base_img, f"prompt: {prompt}", width, height)
    else:
        _placeholder_image(base_img, f"prompt: {prompt}", width, height)

    # 2) build simple Ken-Burns zoom
    #    Start slightly zoomed-in and slowly pan.
    #    (This is a placeholder filter â€” swap with AnimateDiff/SVD when ready.)
    out_mp4 = CLIP_DIR / f"vid_{_ts()}.mp4"
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    zoom_end = 1.08  # small zoom across the clip
    # ffmpeg filter: zoompan or scale + crop pan simulation
    # Simpler approach: use zoompan with fixed zoom across frames
    total_frames = seconds * fps
    zoom_per_frame = (zoom_end - 1.0) / max(total_frames, 1)

    filter_expr = (
        f"zoompan=z='min(zoom+{zoom_per_frame:.6f}, {zoom_end:.6f})':"
        f"d=1:fps={fps},"
        f"scale={width}:{height}"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-i", str(base_img),
        "-vf", filter_expr,
        "-t", str(seconds),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        str(out_mp4),
    ]

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError as e:
        return {"detail": f"video_error: ffmpeg failed ({e})"}, 422

    return _resp(out_mp4)


# -----------------------------------------------------------------------------
# TTS (optional: falls back to stub file so GUI never breaks)
# -----------------------------------------------------------------------------
@router.post(
    "/gen/tts",
    response_model=GenResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {"text": "Hello from Simian!", "voice": "en-US-ChristopherNeural"}
                }
            }
        }
    },
)
async def api_gen_tts(request: Request, body: Optional[dict] = Body(default=None)):
    payload = body if body else await _extract_payload(request)
    text = (payload.get("text") or "").strip()
    voice = payload.get("voice")
    if not text:
        return {"detail": "tts_error: text is required"}, 422

    out = AUD_DIR / f"tts_{_ts()}.mp3"

    if _tts_tool:
        try:
            _tts_tool(text=text, voice_id=voice, out_path=str(out))
            return _resp(out)
        except Exception:
            # continue to stub
            pass

    # stub mp3 (GUI-safe placeholder)
    with open(out, "wb") as f:
        f.write(b"ID3")  # minimal header byte to avoid FileNotFound errors
    return _resp(out)
