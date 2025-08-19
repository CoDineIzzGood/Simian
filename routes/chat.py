
import os
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

OLLAMA_BASE = os.getenv("OLLAMA_BASE", "http://127.0.0.1:11434")
MODEL_NAME = os.getenv("SIMIAN_MODEL", "simian")

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    model: Optional[str] = None
    options: Optional[Dict[str, Any]] = None

app = FastAPI(title="Simian API", version="1.0.0")

@app.post("/api/chat")
async def chat_api(payload: ChatRequest) -> str:
    msgs: List[Dict[str, str]] = []
    if payload.messages:
        msgs = [{"role": m.role, "content": m.content} for m in payload.messages]
    elif payload.prompt:
        msgs = [{"role": "user", "content": payload.prompt}]
    else:
        raise HTTPException(status_code=422, detail="Provide 'prompt' or 'messages'.")

    model = payload.model or MODEL_NAME
    body = {"model": model, "messages": msgs}
    if payload.options:
        body["options"] = payload.options

    url = f"{OLLAMA_BASE}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            data = r.json()
            if "message" in data and "content" in data["message"]:
                return data["message"]["content"]
            if "response" in data:
                return data["response"]
            return str(data)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ollama error: {e}")
