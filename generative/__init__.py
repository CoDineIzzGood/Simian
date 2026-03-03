"""High-level generative helpers.

This file restores the legacy single-function interface that other parts of
the application import (e.g. ``from generative import generate_image``) while
internally delegating to the newer modular helpers that live alongside it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .image_tools import txt2img_auto

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data" / "generated"
_IMAGE_DIR = _DATA_DIR / "images"
_AUDIO_DIR = _DATA_DIR / "audio"
for _dir in (_IMAGE_DIR, _AUDIO_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

_A1111_URL = os.getenv("A1111_URL", "")
_DIFFUSERS_ENABLED = os.getenv("DIFFUSERS_ENABLED", "0") == "1"

__all__ = ["generate_image"]


def generate_image(
    prompt: str,
    engine: str = "auto",
    *,
    image_dir: Optional[Path] = None,
    a1111_url: Optional[str] = None,
    diffusers_enabled: Optional[bool] = None,
    model: Optional[str] = None,
    steps: Optional[int] = None,
    guidance_scale: Optional[float] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> Path:
    """Return a generated image Path, mirroring the historic API.

    Parameters largely match the previous implementation. ``engine`` is passed
    through to ``image_tools.txt2img_auto`` while ``image_dir`` lets callers
    override the storage directory (defaulting to ``data/generated/images``).
    Environment variables still control A1111 and diffusers integration so any
    existing configuration keeps working.
    """
    out_dir = Path(image_dir or _IMAGE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    target_url = a1111_url if a1111_url is not None else _A1111_URL
    use_diffusers = (
        diffusers_enabled if diffusers_enabled is not None else _DIFFUSERS_ENABLED
    )

    path = txt2img_auto(
        prompt=prompt,
        out_dir=out_dir,
        backend=engine or "auto",
        a1111_url=target_url,
        diffusers_enabled=use_diffusers,
        model=model,
        steps=steps,
        guidance_scale=guidance_scale,
        width=width,
        height=height,
    )
    return Path(path).resolve()
