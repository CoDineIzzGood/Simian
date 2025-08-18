
from fastapi import APIRouter
from pydantic import BaseModel
from modules import context_memory

router = APIRouter()

class TextInput(BaseModel):
    text: str

@router.post("/context/save")
def save_context(input_data: TextInput):
    context_memory.save_context(input_data.text)
    return {"status": "context saved"}

@router.get("/context/load")
def load_context():
    return {"context": context_memory.load_context()}
