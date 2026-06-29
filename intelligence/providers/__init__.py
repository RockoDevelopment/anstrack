"""
ANSTRACK -- Provider dispatch
Exposes generate_text / generate_json, routing to the provider named by config.LLM_PROVIDER.
This is the import the analyst and synthesizer use:  from intelligence.providers import generate_text, generate_json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from intelligence.providers.base import BaseProvider
from intelligence.providers.gemini import GeminiProvider
from intelligence.providers.openai import OpenAIProvider
from intelligence.providers.ollama import OllamaProvider

_provider: BaseProvider = None


def get_provider() -> BaseProvider:
    global _provider
    if _provider is not None:
        return _provider
    p = (config.LLM_PROVIDER or "gemini").lower()
    if p == "openai":
        _provider = OpenAIProvider(config.OPENAI_API_KEY, config.OPENAI_MODEL)
    elif p == "ollama":
        _provider = OllamaProvider(config.OLLAMA_MODEL)
    else:
        _provider = GeminiProvider(config.GEMINI_API_KEY, config.GEMINI_MODEL)
    return _provider


def generate_text(prompt: str, system: str = "", max_tokens: int = 4000,
                  temperature: float = 0.7):
    return get_provider().generate_text(prompt, system=system,
                                        max_tokens=max_tokens, temperature=temperature)


def generate_json(prompt: str, system: str = "", max_tokens: int = 4000):
    return get_provider().generate_json(prompt, system=system, max_tokens=max_tokens)


def llm_available() -> bool:
    """True only if the selected provider can actually run. Cloud providers need a key;
    Ollama is local (assumed available -- failures are caught at call time). Lets the
    analyst fall back to keyless heuristic scoring instead of erroring."""
    p = (config.LLM_PROVIDER or "").lower()
    if p == "ollama":
        return True
    if p == "openai":
        return bool(config.OPENAI_API_KEY)
    return bool(config.GEMINI_API_KEY)


__all__ = ["generate_text", "generate_json", "get_provider", "llm_available"]
