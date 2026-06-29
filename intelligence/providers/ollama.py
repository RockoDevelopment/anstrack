"""ANSTRACK -- Ollama provider (local, no key). Default http://localhost:11434"""
import json, urllib.request
from typing import Optional
from intelligence.providers.base import BaseProvider


class OllamaProvider(BaseProvider):
    name = "ollama"

    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def generate_text(self, prompt, system="", max_tokens=4000, temperature=0.7):
        body = {"model": self.model, "prompt": (f"{system}\n\n{prompt}" if system else prompt),
                "stream": False, "options": {"num_predict": max_tokens, "temperature": temperature}}
        req = urllib.request.Request(f"{self.base_url}/api/generate",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        return data.get("response", "").strip() or None
