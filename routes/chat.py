import os
import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
SIMIAN_MODEL = os.getenv("SIMIAN_MODEL", "simian")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "8"))
CONNECT_TIMEOUT = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "3"))
READ_TIMEOUT = max(3.0, min(OLLAMA_TIMEOUT, 30.0))
CONNECT_TIMEOUT = max(1.0, min(CONNECT_TIMEOUT, 15.0))
READ_TIMEOUT = max(3.0, READ_TIMEOUT)
OLLAMA_PROBE_TIMEOUT = float(os.getenv("OLLAMA_PROBE_TIMEOUT", "2"))
OLLAMA_PROBE_PATH = "/api/tags"

def _offline_reply(message: str, extra: str | None = None) -> str:
    lowered = message.lower()
    if any(g in lowered for g in ("hello", "hi", "hey")):
        base = "Hi there! I'm still here even though Ollama isn't reachable right now."
    elif "help" in lowered or "how" in lowered:
        base = "Ollama looks offline. Try restarting it (check the Ollama app or run `ollama serve`) and then resend your message."
    else:
        base = f"I couldn't reach the Ollama model yet. Once it's running on {OLLAMA_URL} I'll be able to answer fully."
    if extra:
        base += f" Details: {extra}"
    return base

def _probe_ollama() -> tuple[bool, str | None]:
    probe_url = f"{OLLAMA_URL.rstrip('/')}{OLLAMA_PROBE_PATH}"
    try:
        resp = requests.get(
            probe_url,
            timeout=(CONNECT_TIMEOUT, OLLAMA_PROBE_TIMEOUT),
        )
        resp.raise_for_status()
        return True, None
    except requests.exceptions.RequestException as exc:
        return False, str(exc)

def chat_reply(message: str, session_id: str | None = None) -> str:
    try:
        available, hint = _probe_ollama()
        if not available:
            return _offline_reply(message, hint)
        payload = {
            "model": SIMIAN_MODEL,
            "prompt": message,
            "stream": False,
        }
        if session_id:
            payload["context"] = session_id
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        r.raise_for_status()
        data = r.json()
        return (data.get("response") or "").strip()
    except requests.exceptions.Timeout as exc:
        return _offline_reply(message, str(exc))
    except requests.exceptions.ConnectionError as exc:
        return _offline_reply(message, str(exc))
    except requests.exceptions.RequestException as exc:
        return _offline_reply(message, str(exc))
    except Exception as e:
        raise RuntimeError(f"ollama_error: {e!s}")
