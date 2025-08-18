from fastapi import APIRouter
from app.memory.memory_manager import remember_context, get_memory

router = APIRouter()

@router.post("/remember")
def remember(data: dict):
    context = data.get("context", "")
    remember_context(context)
    return {"message": "Context saved"}

@router.get("/recall")
def recall():
    return get_memory()
