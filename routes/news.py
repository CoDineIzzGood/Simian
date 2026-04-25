from __future__ import annotations

from fastapi import APIRouter, Query

from core.response_models import ErrorInfo, ServiceResponse
from core.task_runner import get_task_runner
from services.news_service import fetch_news
from services.settings_store import load_settings

router = APIRouter(prefix="/news", tags=["news"])
_task_runner = get_task_runner()


@router.get("")
async def get_news(
    category: str | None = Query(None),
    limit: int = Query(40, ge=1, le=200),
):
    settings = load_settings()
    effective_category = (category or getattr(settings, "news_default_category", "tech") or "tech").strip().lower()
    try:
        items = await _task_runner.run_blocking(fetch_news, effective_category, limit)
        return ServiceResponse(
            status="ok",
            data={
                "category": effective_category,
                "count": len(items),
                "items": [it.__dict__ for it in items],
            },
        )
    except Exception as exc:
        return ServiceResponse(
            status="error",
            message="Failed to load news.",
            error=ErrorInfo(code="news_fetch_failed", message=str(exc)),
            data={"category": effective_category, "count": 0, "items": []},
        )
