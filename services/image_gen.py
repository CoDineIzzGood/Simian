from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

_BACKEND_PROC: Optional[subprocess.Popen] = None


def _ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _placeholder(prompt: str, out_path: Path, width: int = 768, height: int = 768) -> Path:
    img = Image.new("RGB", (width, height), (10, 12, 18))
    draw = ImageDraw.Draw(img)
    text = (prompt or "")[:220]
    draw.text((20, 20), "Simian image tool (placeholder)", fill=(220, 230, 255))
    draw.text((20, 60), text, fill=(180, 200, 255))
    draw.rectangle((40, 140, width - 40, height - 40), outline=(90, 130, 220), width=4)
    draw.text((60, 160), "No real image backend configured.", fill=(255, 200, 160))
    img.save(out_path, "PNG")
    return out_path.resolve()


def _load_backend():
    try:
        mod = importlib.import_module("generative.image_tools")
        return getattr(mod, "txt2img_auto", None)
    except Exception:
        return None


def ensure_image_backend_ready() -> bool:
    global _BACKEND_PROC
    if callable(_load_backend()):
        return True
    cmd = (os.environ.get("SIMIAN_IMAGE_BACKEND_CMD") or "").strip()
    if cmd and _BACKEND_PROC is None:
        try:
            _BACKEND_PROC = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        except Exception:
            _BACKEND_PROC = None
    return callable(_load_backend())


def txt2img_generate(
    prompt: str,
    seed: Optional[int] = None,
    out_path: Optional[str] = None,
    backend: str = "auto",
    width: int = 768,
    height: int = 768,
) -> str:
    out_dir = _ensure_dir(Path(out_path).parent if out_path else Path("data/generated/images"))
    target = Path(out_path) if out_path else out_dir / f"img_{int(time.time()*1000)}.png"

    txt2img_auto = _load_backend()
    if callable(txt2img_auto):
        result = txt2img_auto(prompt=prompt, out_dir=out_dir, backend=backend, width=width, height=height)
        return str(Path(result).resolve())

    if ensure_image_backend_ready():
        txt2img_auto = _load_backend()
        if callable(txt2img_auto):
            result = txt2img_auto(prompt=prompt, out_dir=out_dir, backend=backend, width=width, height=height)
            return str(Path(result).resolve())

    allow_placeholder = os.environ.get("SIMIAN_ALLOW_PLACEHOLDER_IMAGES", "1").strip().lower() not in {"0", "false", "no"}
    if allow_placeholder:
        return str(_placeholder(prompt, target, width=width, height=height))

    raise RuntimeError(
        "No real image backend is configured. Install the generative.image_tools backend or set SIMIAN_IMAGE_BACKEND_CMD."
    )
