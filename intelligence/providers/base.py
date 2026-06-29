"""ANSTRACK -- Provider base. All LLM providers implement this interface."""
from abc import ABC, abstractmethod
from typing import Optional


class BaseProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate_text(self, prompt: str, system: str = "",
                      max_tokens: int = 4000, temperature: float = 0.7) -> Optional[str]:
        ...

    def generate_json(self, prompt: str, system: str = "", max_tokens: int = 4000) -> Optional[str]:
        json_system = (system + "\n\nRespond ONLY with valid JSON. No markdown fences, no preamble.").strip()
        return self.generate_text(prompt, system=json_system, max_tokens=max_tokens, temperature=0.2)

    def health_check(self) -> dict:
        try:
            return {"ok": bool(self.generate_text("Say OK", max_tokens=10)), "provider": self.name}
        except Exception as e:
            return {"ok": False, "provider": self.name, "error": str(e)}
