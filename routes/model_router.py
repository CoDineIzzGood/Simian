from __future__ import annotations

from typing import Dict

from fastapi import APIRouter
from pydantic import BaseModel

from services.model_router import ModelRouterService

router = APIRouter(tags=["model-router"])


class RouterUpdateRequest(BaseModel):
    router: Dict[str, str]


@router.get("/routing")
def get_routing():
    return ModelRouterService.get_routing()


@router.post("/routing")
def update_routing(payload: RouterUpdateRequest):
    return ModelRouterService.update_routing(payload.router)
