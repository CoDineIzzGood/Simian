
import requests
from typing import List, Dict

class LLMClient:
    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")

    def chat(self, messages: List[Dict[str,str]] | None = None, prompt: str | None = None, model: str | None = None) -> str:
        url = f"{self.api_base}/api/chat"
        body = {"messages": messages, "prompt": prompt, "model": model}
        r = requests.post(url, json=body, timeout=120)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type","").startswith("application/json") else r.text
