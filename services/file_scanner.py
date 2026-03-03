"""
FileScannerService: summarize ANY local file (best-effort) + batch directory scans.

Goals:
- Works offline with simple extraction (text, metadata)
- If Ollama is running (and a model is configured), upgrades to rich summaries
  - Text model: general summarization
  - Vision model: image/frame description (Ollama supports base64 images)

No network exfiltration: everything stays local unless user deliberately sends it.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.settings_store import load_settings


def _sha256(path: Path, max_bytes: int = 2_000_000) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        remaining = max_bytes
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _guess_mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    # fallback by extension
    ext = path.suffix.lower()
    if ext in (".md", ".txt", ".log", ".py", ".json", ".yaml", ".yml", ".toml", ".ini"):
        return "text/plain"
    if ext in (".pdf",):
        return "application/pdf"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        return "image/*"
    if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
        return "video/*"
    return "application/octet-stream"


def _read_text(path: Path, limit_chars: int = 200_000) -> str:
    data = path.read_bytes()
    # naive decode
    for enc in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)[:limit_chars]
        except Exception:
            continue
    return data[:limit_chars].decode("latin-1", errors="replace")


def _extract_pdf_text(path: Path, limit_chars: int = 300_000) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        out = []
        for page in reader.pages[:50]:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(out)[:limit_chars]
    except Exception:
        # fallback: nothing
        return ""


def _ollama_available(base_url: str = "http://127.0.0.1:11434") -> bool:
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _ollama_generate(prompt: str, model: str, images_b64: Optional[List[str]] = None,
                     base_url: str = "http://127.0.0.1:11434", timeout: float = 120.0) -> str:
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if images_b64:
        payload["images"] = images_b64
    r = httpx.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()


@dataclass
class FileScanResult:
    path: str
    size_bytes: int
    mime: str
    sha256: str
    summary: str
    details: Dict[str, Any]


class FileScannerService:
    def __init__(self, log_cb=None):
        self.log = log_cb or (lambda msg: None)
        self.settings = load_settings()

        # Optional: user can set these env vars
        self.ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        self.ollama_text_model = os.environ.get("OLLAMA_TEXT_MODEL", "llama3.1:8b")
        self.ollama_vision_model = os.environ.get("OLLAMA_VISION_MODEL", "llava:7b")

    def summarize_path(self, file_path: str) -> FileScanResult:
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(file_path)

        mime = _guess_mime(p)
        size = p.stat().st_size
        sha = _sha256(p)

        base_details: Dict[str, Any] = {
            "name": p.name,
            "ext": p.suffix.lower(),
        }

        # Extract content
        extracted_text = ""
        images_b64: Optional[List[str]] = None

        if mime.startswith("text/") or mime in ("application/json",):
            extracted_text = _read_text(p)
        elif mime == "application/pdf":
            extracted_text = _extract_pdf_text(p)
        elif mime.startswith("image/") or mime == "image/*":
            try:
                from PIL import Image
                im = Image.open(p)
                base_details["image_size"] = im.size
                base_details["image_mode"] = im.mode
                # for vision model
                images_b64 = [base64.b64encode(p.read_bytes()).decode("utf-8")]
            except Exception:
                images_b64 = [base64.b64encode(p.read_bytes()).decode("utf-8")]
        elif mime.startswith("video/") or mime == "video/*":
            # Best-effort: we won't decode video in pure python. Provide metadata + suggest ffmpeg frame extraction.
            base_details["note"] = "Video detected. For rich summary: enable ffmpeg frame extraction + vision model."
        else:
            base_details["note"] = "Binary/unknown type. Showing metadata only."

        # Summarize
        summary = self._summarize(mime=mime, path=p, extracted_text=extracted_text, images_b64=images_b64, details=base_details)

        return FileScanResult(
            path=str(p),
            size_bytes=size,
            mime=mime,
            sha256=sha,
            summary=summary,
            details=base_details,
        )

    def summarize_batch(self, paths: List[str], max_files: int = 200) -> Dict[str, Any]:
        results = []
        for fp in paths[:max_files]:
            try:
                results.append(self.summarize_path(fp).__dict__)
            except Exception as e:
                results.append({"path": fp, "error": str(e)})
        return {"count": len(results), "results": results}

    def summarize_directory(self, directory: str, recursive: bool = True, max_files: int = 200) -> Dict[str, Any]:
        d = Path(directory)
        if not d.exists() or not d.is_dir():
            raise NotADirectoryError(directory)
        files = []
        it = d.rglob("*") if recursive else d.glob("*")
        for p in it:
            if p.is_file():
                files.append(str(p))
            if len(files) >= max_files:
                break
        return self.summarize_batch(files, max_files=max_files)

    def _summarize(self, mime: str, path: Path, extracted_text: str, images_b64: Optional[List[str]], details: Dict[str, Any]) -> str:
        # If Ollama is available, use it for better summaries.
        if _ollama_available(self.ollama_url):
            try:
                if images_b64:
                    prompt = (
                        "Describe the image in detail. Include: main subjects, text (if any), setting, notable objects, "
                        "and anything that could matter for investigation or documentation. Be factual."
                    )
                    return _ollama_generate(prompt, model=self.ollama_vision_model, images_b64=images_b64, base_url=self.ollama_url)
                if extracted_text:
                    prompt = (
                        "Summarize the following content. "
                        "Return: (1) 5-bullet executive summary, (2) key entities (people/orgs/places), "
                        "(3) timeline if relevant, (4) anything suspicious/interesting. "
                        "Content:\n\n" + extracted_text[:120000]
                    )
                    return _ollama_generate(prompt, model=self.ollama_text_model, base_url=self.ollama_url)
            except Exception as e:
                self.log(f"[FileScanner] Ollama failed; falling back. ({e})")

        # Offline fallback summaries
        if images_b64:
            return "Image file detected. (Local vision model not available) Stored metadata only."
        if extracted_text:
            # naive
            snippet = extracted_text.strip().replace("\r", "")
            snippet = snippet[:1200]
            return f"Text extracted ({len(extracted_text)} chars). Preview:\n\n{snippet}"
        return f"{mime} file. Size={details.get('name')} ({details.get('ext')})"


# Back-compat alias used by GUI patches
__all__ = ["FileScannerService", "FileScanResult"]
