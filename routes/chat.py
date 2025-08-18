import os
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

router = APIRouter()

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    model: str = Field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "llama3.1"))
    options: Dict[str, Any] = {}

@router.post("/chat")
def chat(req: ChatRequest):
    # Always call Ollama /api/chat directly. No stubs or fallbacks.
    if not req.messages and req.prompt:
        req.messages = [ChatMessage(role="user", content=req.prompt)]
    if not req.messages:
        raise HTTPException(status_code=400, detail="Provide 'prompt' or 'messages'.")

    payload = {
        "model": req.model or OLLAMA_MODEL,
        "messages": [m.dict() for m in req.messages],
        "stream": False,
        **({"options": req.options} if req.options else {}),
    }

    try:
        url = f"{OLLAMA_HOST.rstrip('/')}/api/chat"
        r = requests.post(url, json=payload, timeout=120)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()
        out = {
            "model": data.get("model"),
            "created_at": data.get("created_at"),
            "response": data.get("message", {}).get("content") or data.get("response"),
            "done": data.get("done", True),
            "total_duration": data.get("total_duration"),
            "eval_count": data.get("eval_count"),
        }
        return out
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Ollama unavailable: {e}")
