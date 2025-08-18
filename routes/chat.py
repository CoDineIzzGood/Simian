
# routes/chat.py
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from services.ollama_client import chat as ollama_chat

router = APIRouter()

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: Optional[str] = Field(default=None, description="Optional single-turn prompt")
    messages: Optional[List[ChatMessage]] = Field(default=None, description="Multi-turn messages")
    model: Optional[str] = Field(default=None, description="Ollama model to use")
    options: Dict[str, Any] = Field(default_factory=dict)

@router.post("/api/chat", response_model=str)
async def chat_api(req: ChatRequest) -> str:
    messages = req.messages or []
    if req.prompt and not messages:
        messages = [{"role": "user", "content": req.prompt}]
    # ensure types
    msgs = [{"role": m["role"] if isinstance(m, dict) else m.role,
             "content": m["content"] if isinstance(m, dict) else m.content} for m in messages]
    reply = await ollama_chat(msgs, model=req.model, options=req.options or {})
    return reply
