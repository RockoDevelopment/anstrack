"""ANSTRACK -- Gemini provider (pure urllib, no SDK)."""
import json, urllib.error, urllib.request
from typing import Optional
from intelligence.providers.base import BaseProvider

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model

    def _call(self, prompt, system, max_tokens, temperature, json_mode):
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        url = f"{BASE}/{self.model}:generateContent?key={self.api_key}"
        gen = {"maxOutputTokens": max_tokens, "temperature": temperature}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Gemini HTTP {e.code}: {e.read().decode('utf-8','ignore')}") from e
        cands = data.get("candidates", [])
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip() or None

    def generate_text(self, prompt, system="", max_tokens=4000, temperature=0.7):
        return self._call(prompt, system, max_tokens, temperature, False)

    def generate_json(self, prompt, system="", max_tokens=4000):
        js = (system + "\n\nRespond ONLY with valid JSON. No markdown fences, no preamble.").strip()
        return self._call(prompt, js, max_tokens, 0.2, True)
