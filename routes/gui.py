# routes/gui.py
from __future__ import annotations
from fastapi import APIRouter
from ..services.simian import time_of_day_greeting, SYSTEM_PERSONA, SIMIAN_NAME

router = APIRouter()

@router.get("/hello")
def hello():
    return {"greeting": time_of_day_greeting(), "name": SIMIAN_NAME}

@router.get("/persona")
def persona():
    return {"system": SYSTEM_PERSONA}
