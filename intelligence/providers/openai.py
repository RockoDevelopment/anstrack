"""ANSTRACK -- OpenAI-compatible provider (OpenAI, OpenRouter, local vLLM)."""
import json, urllib.request
from typing import Optional
from intelligence.providers.base import BaseProvider

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAIProvider(BaseProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o", api_base: str = DEFAULT_BASE):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")

    def generate_text(self, prompt, system="", max_tokens=4000, temperature=0.7):
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        body = {"model": self.model, "messages": msgs,
                "max_tokens": max_tokens, "temperature": temperature}
        req = urllib.request.Request(f"{self.api_base}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"].strip()
