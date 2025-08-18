# main.py
import os, logging, requests
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("simian")

app = FastAPI()

# ---- Llama endpoint config ----
# Defaults to OLLAMA (http://127.0.0.1:11434, model llama3.1)
LLAMA_MODE = os.getenv("LLAMA_MODE", "ollama").lower()           # "ollama" or "openai"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "llama3.1")

# If you run an OpenAI-compatible server (e.g., llama.cpp server, LM Studio, vLLM):
OPENAI_BASE = os.getenv("OPENAI_BASE", "http://127.0.0.1:8001/v1")  # example
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

class ChatIn(BaseModel):
    prompt: str

@app.get("/health")
def health():
    return {"ok": True, "mode": LLAMA_MODE}

def ask_ollama(prompt: str) -> str:
    # POST /api/generate {model, prompt, stream}
    url = f"{OLLAMA_URL.rstrip('/')}/api/generate"
    r = requests.post(url, json={"model": LLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=120)
    r.raise_for_status()
    js = r.json()
    # Ollama returns {"response": "...", ...}
    return js.get("response", "").strip()

def ask_openai_compatible(prompt: str) -> str:
    # Chat completions format
    url = f"{OPENAI_BASE.rstrip('/')}/chat/completions"
    headers = {}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"
    data = {
        "model": LLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    r = requests.post(url, json=data, timeout=120, headers=headers)
    r.raise_for_status()
    js = r.json()
    # Standard OpenAI-like response
    return js["choices"][0]["message"]["content"].strip()

@app.post("/chat")
def chat(body: ChatIn):
    prompt = body.prompt
    log.info("chat prompt: %s", prompt)
    try:
        if LLAMA_MODE == "openai":
            reply = ask_openai_compatible(prompt)
        else:
            reply = ask_ollama(prompt)
    except Exception as e:
        # try the other mode as a fallback once
        try:
            if LLAMA_MODE == "openai":
                reply = ask_ollama(prompt)
            else:
                reply = ask_openai_compatible(prompt)
        except Exception as e2:
            log.exception("LLM call failed")
            return {"response": f"(error) LLM unavailable: {e2}"}
    return {"response": reply}
