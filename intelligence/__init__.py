"""
Pump.fun Brain -- Intelligence package
Reuses the LAIS provider layer (base/gemini/ollama/openai) unchanged. Copy your existing
intelligence/providers/ directory in next to this file -- nothing about it needs editing.
This package just re-exports analyse_signals from the alpha analyst so processor.py's
`from intelligence import analyse_signals` works as-is.
"""
from intelligence.alpha_analyst import analyse_signals

__all__ = ["analyse_signals"]
