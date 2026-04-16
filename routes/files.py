from __future__ import annotations

from pathlib import Path
import tempfile

from fastapi import APIRouter, File, Form, UploadFile

from core.response_models import ErrorInfo, ServiceResponse
from core.task_runner import get_task_runner
from services.file_scanner import FileScannerService

router = APIRouter(prefix="/files", tags=["files"])
_task_runner = get_task_runner()


def _safe_error_payload(code: str, exc: Exception) -> ServiceResponse:
    return ServiceResponse(
        status="error",
        message="File summarization failed.",
        error=ErrorInfo(code=code, message=str(exc)),
    )


@router.post("/summarize-path")
async def summarize_path(path: str = Form(...)):
    svc = FileScannerService()
    try:
        res = await _task_runner.run_blocking(svc.summarize_path, path)
        return ServiceResponse(status="ok", data=res.__dict__)
    except Exception as exc:
        return _safe_error_payload("file_summarize_path_failed", exc)


@router.post("/summarize-upload")
async def summarize_upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "upload").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    svc = FileScannerService()
    try:
        res = await _task_runner.run_blocking(svc.summarize_path, tmp_path)
        payload = dict(res.__dict__)
        payload["original_filename"] = file.filename
        return ServiceResponse(status="ok", data=payload)
    except Exception as exc:
        return _safe_error_payload("file_summarize_upload_failed", exc)
