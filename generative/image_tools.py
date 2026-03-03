from __future__ import annotations
import base64
import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger("simian.generative")

HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=25.0, write=10.0, pool=3.0)

_DIFFUSERS_KNOWN_MODELS: Dict[str, str] = {
    "sd21": os.getenv("SD21_MODEL", "stabilityai/stable-diffusion-2-1"),
    "sdxl": os.getenv("SDXL_MODEL", "stabilityai/stable-diffusion-xl-base-1.0"),
}
_DEFAULT_DIFFUSERS_MODEL = os.getenv("DIFFUSERS_MODEL") or _DIFFUSERS_KNOWN_MODELS["sd21"]

def _resolve_diffusers_model(model: Optional[str]) -> str:
    if not model:
        return _DEFAULT_DIFFUSERS_MODEL
    candidate = model.strip()
    if not candidate:
        return _DEFAULT_DIFFUSERS_MODEL
    if '/' in candidate or candidate.startswith('http'):
        return candidate
    alias = candidate.lower()
    return _DIFFUSERS_KNOWN_MODELS.get(alias, candidate)

_DEF_BG = (8, 10, 20)


def _ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _unique_name(prefix: str, ext: str) -> str:
    return f"{prefix}_{int(time.time()*1000):d}.{ext}"


def _draw_monkey(draw: ImageDraw.ImageDraw, center: tuple[int, int]) -> None:
    cx, cy = center
    body_r = 170
    head_r = 120
    body_color = (60, 45, 35)
    face_color = (205, 170, 120)
    ear_color = (180, 140, 90)
    draw.ellipse((cx-body_r, cy-body_r, cx+body_r, cy+body_r), fill=body_color)
    draw.ellipse((cx-head_r, cy-head_r-170, cx+head_r, cy+head_r-170), fill=body_color)
    draw.ellipse((cx-head_r+25, cy-head_r-110, cx+head_r-25, cy+head_r-200), fill=face_color)
    ear_r = 55
    draw.ellipse((cx-head_r-40-ear_r, cy-head_r-120-ear_r, cx-head_r-40+ear_r, cy-head_r-120+ear_r), fill=body_color)
    draw.ellipse((cx+head_r+40-ear_r, cy-head_r-120-ear_r, cx+head_r+40+ear_r, cy-head_r-120+ear_r), fill=body_color)
    draw.ellipse((cx-head_r-25-ear_r+10, cy-head_r-120-ear_r+10, cx-head_r-25+ear_r-10, cy-head_r-120+ear_r-10), fill=ear_color)
    draw.ellipse((cx+head_r+25-ear_r+10, cy-head_r-120-ear_r+10, cx+head_r+25+ear_r-10, cy-head_r-120+ear_r-10), fill=ear_color)
    eye_r = 18
    draw.ellipse((cx-40-eye_r, cy-220-eye_r, cx-40+eye_r, cy-220+eye_r), fill=(20, 20, 20))
    draw.ellipse((cx+40-eye_r, cy-220-eye_r, cx+40+eye_r, cy-220+eye_r), fill=(20, 20, 20))
    draw.arc((cx-70, cy-160, cx-10, cy-120), start=200, end=340, fill=(10, 10, 10), width=6)
    draw.arc((cx+10, cy-160, cx+70, cy-120), start=200, end=340, fill=(10, 10, 10), width=6)
    draw.pieslice((cx-45, cy-80, cx+45, cy+40), start=20, end=160, fill=(10, 10, 10))
    draw.arc((cx-120, cy+20, cx+120, cy+160), start=20, end=160, fill=(10, 10, 10), width=12)
    tail_pts = [(cx+body_r-10, cy+40), (cx+body_r+120, cy-80), (cx+body_r+30, cy-170)]
    draw.line(tail_pts, fill=body_color, width=30)


def _placeholder(prompt: str, out_dir: Path) -> Path:
    W = H = 768
    img = Image.new("RGB", (W, H), _DEF_BG)
    gradient = Image.new("RGB", (W, H), _DEF_BG)
    for ring, color in enumerate([(35, 45, 120), (20, 30, 80), (15, 22, 65)], start=1):
        mask = Image.new("L", (W, H), 0)
        mdraw = ImageDraw.Draw(mask)
        radius = int(520 / ring)
        mdraw.ellipse((W//2-radius, H//2-radius, W//2+radius, H//2+radius), fill=120//ring)
        gradient = Image.composite(Image.new("RGB", (W, H), color), gradient, mask)
    img = Image.blend(img, gradient, alpha=0.6)
    draw = ImageDraw.Draw(img)
    import random
    for _ in range(2200):
        x, y = random.randrange(W), random.randrange(H)
        brightness = random.randint(160, 255)
        img.putpixel((x, y), (brightness, brightness, brightness))
        if random.random() < 0.02:
            size = random.choice([1, 2, 3])
            draw.ellipse((x-size, y-size, x+size, y+size), fill=(brightness, brightness, brightness))
    for _ in range(40):
        x, y = random.randrange(W), random.randrange(H)
        draw.line((x-3, y, x+3, y), fill=(220, 220, 255), width=1)
        draw.line((x, y-3, x, y+3), fill=(220, 220, 255), width=1)
    prompt_lower = prompt.lower()
    if "monkey" in prompt_lower or "simian" in prompt_lower:
        _draw_monkey(draw, (W//2, int(H*0.6)))
    elif "cat" in prompt_lower:
        tail_color = (18, 18, 26)
        cx, cy = W//2, int(H*0.62)
        body_r = 190
        draw.ellipse((cx-body_r, cy-body_r, cx+body_r, cy+body_r), fill=tail_color)
        head_r = 130
        draw.ellipse((cx-head_r, cy-head_r-180, cx+head_r, cy+head_r-180), fill=tail_color)
        draw.polygon([(cx-130, cy-190), (cx-10, cy-330), (cx-240, cy-320)], fill=tail_color)
        draw.polygon([(cx+130, cy-190), (cx+10, cy-330), (cx+240, cy-320)], fill=tail_color)
        draw.ellipse((cx-50, cy-270, cx-18, cy-238), fill=(70, 80, 160))
        draw.ellipse((cx+18, cy-270, cx+50, cy-238), fill=(70, 80, 160))
        draw.polygon([(cx-70, cy-96), (cx, cy-28), (cx+70, cy-96)], fill=(70, 80, 160))
        draw.line((cx+body_r-12, cy+38, cx+body_r+90, cy-72), fill=tail_color, width=26)
        draw.line((cx+body_r+86, cy-72, cx+body_r+150, cy-6), fill=tail_color, width=18)
    planet_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(planet_layer)
    pdraw.ellipse((120, 380, 300, 560), fill=(90, 120, 220, 255))
    pdraw.ellipse((110, 370, 310, 570), outline=(180, 180, 255, 160), width=6)
    pdraw.ellipse((440, 160, 680, 400), fill=(220, 150, 90, 255))
    pdraw.ellipse((430, 150, 690, 410), outline=(255, 220, 170, 150), width=6)
    img = Image.alpha_composite(img.convert("RGBA"), planet_layer).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
        draw.text((16, 16), prompt[:140], fill=(235, 235, 245), font=font)
    except Exception:
        pass
    out_dir = _ensure_dir(out_dir)
    out_path = out_dir / _unique_name("img", "png")
    img.save(out_path, "PNG")
    return out_path.resolve()


def _from_a1111(prompt: str, out_dir: Path, a1111_url: str) -> Optional[Path]:
    if not a1111_url:
        return None
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            payload = {"prompt": prompt, "steps": 22, "width": 768, "height": 768}
            resp = client.post(f"{a1111_url.rstrip('/')}/sdapi/v1/txt2img", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("images"):
                return None
            raw = base64.b64decode(data["images"][0].split(",", 1)[-1])
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            out_dir = _ensure_dir(out_dir)
            out_path = out_dir / _unique_name("img", "png")
            img.save(out_path, "PNG")
            return out_path.resolve()
    except Exception as exc:
        logger.debug("Automatic1111 request failed: %s", exc)
        return None


def _from_stability(prompt: str, out_dir: Path, *, api_key: str, api_url: str, model: str) -> Optional[Path]:
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "image/png",
            "Content-Type": "application/json"
        }
        payload = {
            "prompt": prompt,
            "model": model,
            "output_format": "png"
        }
        resp = httpx.post(api_url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        out_dir = _ensure_dir(out_dir)
        out_path = out_dir / _unique_name("img", "png")
        with open(out_path, "wb") as f:
            f.write(resp.content)
        return out_path.resolve()
    except Exception as exc:
        logger.debug("Stability API request failed: %s", exc)
        return None

STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")
STABILITY_API_URL = os.getenv("STABILITY_API_URL", "https://api.stability.ai/v2beta/stable-image/generate/ultra")
STABILITY_MODEL = os.getenv("STABILITY_MODEL", "stable-image-ultra")

_PIPELINE_LOCK = threading.Lock()
_PIPELINE_STATE: Dict[str, str] = {}
_PIPELINE_CACHE: Dict[str, Any] = {}

def _load_diffusers_pipeline(model_id: str, device: str):
    try:
        from diffusers import StableDiffusionPipeline
        import torch
    except Exception as exc:
        logger.debug("Diffusers unavailable: %s", exc)
        return None
    try:
        torch_dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = StableDiffusionPipeline.from_pretrained(
            model_id,
            safety_checker=None,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=False,  # avoid offload_state_dict kwargs issues on some transformer builds
        )
        pipe = pipe.to(device)
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass
        pipe.safety_checker = None
        return pipe
    except Exception as exc:
        logger.warning("Failed to load diffusers pipeline %s on %s: %s", model_id, device, exc)
        return None


def _diffusers_device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception as exc:
        logger.debug("Torch unavailable for diffusers: %s", exc)
        return "cpu"


def _ensure_pipeline_async(model_id: str, device: str) -> None:
    with _PIPELINE_LOCK:
        state = _PIPELINE_STATE.get(model_id, "uninitialized")
        if state in {"loading", "ready"}:
            return
        if state == "failed":
            return
        _PIPELINE_STATE[model_id] = "loading"
        threading.Thread(
            target=_load_pipeline_worker, args=(model_id, device), daemon=True
        ).start()


def _load_pipeline_worker(model_id: str, device: str) -> None:
    pipe = _load_diffusers_pipeline(model_id, device)
    with _PIPELINE_LOCK:
        if pipe is not None:
            _PIPELINE_CACHE[model_id] = pipe
            _PIPELINE_STATE[model_id] = "ready"
        else:
            _PIPELINE_STATE[model_id] = "failed"


def _clamp_steps(value: Optional[int]) -> int:
    if value is None:
        return 36
    return max(1, min(int(value), 150))


def _clamp_guidance(value: Optional[float]) -> float:
    if value is None:
        return 8.0
    return max(0.0, min(float(value), 30.0))


def _sanitize_dimension(value: Optional[int], default: int) -> int:
    if value is None:
        value = default
    try:
        dim = int(value)
    except (TypeError, ValueError):
        dim = default
    dim = max(256, min(dim, 1024))
    dim = dim - (dim % 8)
    if dim < 256:
        dim = 256
    return dim


def _diffusers_generate(
    prompt: str,
    out_dir: Path,
    *,
    steps: Optional[int],
    guidance_scale: Optional[float],
    width: Optional[int],
    height: Optional[int],
    model_id: str = _DEFAULT_DIFFUSERS_MODEL,
) -> Optional[Path]:
    resolved_model = _resolve_diffusers_model(model_id)
    device = _diffusers_device()
    with _PIPELINE_LOCK:
        state = _PIPELINE_STATE.get(resolved_model, "uninitialized")
        pipe = _PIPELINE_CACHE.get(resolved_model) if state == "ready" else None
    if state == "failed":
        return None
    if pipe is None:
        _ensure_pipeline_async(resolved_model, device)
        for _ in range(16):
            time.sleep(0.5)
            with _PIPELINE_LOCK:
                state = _PIPELINE_STATE.get(resolved_model, "uninitialized")
                if state == "ready":
                    pipe = _PIPELINE_CACHE.get(resolved_model)
                    if pipe is not None:
                        break
                if state == "failed":
                    break
        if pipe is None and state != "failed":
            pipe = _load_diffusers_pipeline(resolved_model, device)
            with _PIPELINE_LOCK:
                if pipe is not None:
                    _PIPELINE_CACHE[resolved_model] = pipe
                    _PIPELINE_STATE[resolved_model] = "ready"
                elif _PIPELINE_STATE.get(resolved_model) != "ready":
                    _PIPELINE_STATE[resolved_model] = "failed"
            if pipe is None:
                return None
        elif pipe is None:
            return None
    prompt_enhanced = prompt.strip() or "a detailed illustration"
    if "cat" in prompt.lower():
        prompt_enhanced += ", detailed cat astronaut, shimmering fur, cosmic lighting"
    elif "monkey" in prompt.lower() or "simian" in prompt.lower():
        prompt_enhanced += ", playful space monkey, expressive eyes, floating pose"
    prompt_enhanced += ", ultra detailed, 4k, high resolution, cinematic lighting, vivid colors"
    kwargs = {
        "num_inference_steps": _clamp_steps(steps),
        "guidance_scale": _clamp_guidance(guidance_scale),
        "width": _sanitize_dimension(width, 768),
        "height": _sanitize_dimension(height, 768),
        "negative_prompt": "low quality, blurry, distorted, mutated, deformed, text, watermark",
    }
    try:
        image = pipe(prompt_enhanced, **kwargs).images[0]
    except Exception as exc:
        logger.warning("Diffusers generation failed: %s", exc)
        with _PIPELINE_LOCK:
            if _PIPELINE_STATE.get(resolved_model) != "failed":
                _PIPELINE_STATE[resolved_model] = "failed"
        return None
    out_dir = _ensure_dir(out_dir)
    out_path = out_dir / _unique_name("img", "png")
    image.save(out_path, "PNG")
    return out_path.resolve()


def txt2img_auto(
    prompt: str,
    out_dir: str | os.PathLike,
    backend: str = "auto",
    a1111_url: str = "",
    diffusers_enabled: bool = False,
    *,
    steps: Optional[int] = None,
    guidance_scale: Optional[float] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    model: Optional[str] = None,
) -> str:
    out_dir = _ensure_dir(out_dir)
    prompt = prompt.strip()
    if backend in ("auto", "a1111"):
        result = _from_a1111(prompt, out_dir, a1111_url)
        if result:
            return str(result)
    use_diffusers = backend == "diffusers" or (backend == "auto" and diffusers_enabled)
    if use_diffusers:
        result = _diffusers_generate(
            prompt,
            out_dir,
            steps=steps,
            guidance_scale=guidance_scale,
            width=width,
            height=height,
            model_id=model or _DEFAULT_DIFFUSERS_MODEL,
        )
        if result:
            return str(result)
    return str(_placeholder(prompt, out_dir))

