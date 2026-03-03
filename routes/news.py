from __future__ import annotations

from fastapi import APIRouter, Query
from services.news_service import fetch_news

router = APIRouter(prefix="/news", tags=["news"])

@router.get("")
def get_news(category: str = Query("tech"), limit: int = Query(40, ge=1, le=200)):
    items = fetch_news(category=category, limit=limit)
    return {"category": category, "count": len(items), "items": [it.__dict__ for it in items]}
