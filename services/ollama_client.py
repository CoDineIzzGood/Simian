
# services/ollama_client.py
import os
from typing import List, Dict, Any
import httpx

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

async def chat(messages: List[Dict[str, str]], model: str | None = None, options: Dict[str, Any] | None = None) -> str:
    """
    Calls the Ollama chat API and returns the assistant's final message content as a string.
    Will raise httpx.HTTPStatusError on failure.
    """
    if model is None or not str(model).strip():
        model = DEFAULT_MODEL
    payload = {"model": model, "messages": messages or [], "stream": False}
    if options:
        payload["options"] = options

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        # Ollama's response usually contains {"message": {"role":"assistant","content":"..."}}
        if isinstance(data, dict):
            msg = data.get("message") or {}
            content = msg.get("content")
            if content:
                return content
        # Fallback: sometimes an array of messages
        if isinstance(data, list) and data and isinstance(data[-1], dict):
            last = data[-1]
            if "content" in last:
                return last["content"]
        return str(data)
