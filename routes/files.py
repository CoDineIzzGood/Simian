from __future__ import annotations

from fastapi import APIRouter, UploadFile, File, Form
from pathlib import Path
import tempfile

from services.file_scanner import FileScannerService

router = APIRouter(prefix="/files", tags=["files"])


@router.post("/summarize-path")
def summarize_path(path: str = Form(...)):
    svc = FileScannerService()
    res = svc.summarize_path(path)
    return res.__dict__


@router.post("/summarize-upload")
async def summarize_upload(file: UploadFile = File(...)):
    # Save to temp and scan
    suffix = Path(file.filename or "upload").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    svc = FileScannerService()
    res = svc.summarize_path(tmp_path)
    # overwrite path with original name
    payload = res.__dict__
    payload["original_filename"] = file.filename
    return payload
