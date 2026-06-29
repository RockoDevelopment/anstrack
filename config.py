"""
Pump.fun Brain -- Config
Auto-loads a local .env (no dependency) then reads from the environment, so it works the
same in PowerShell, cmd, and bash -- no manual `export` needed.
"""
import os
from pathlib import Path


def _load_dotenv():
    """Minimal .env loader: KEY=VALUE lines, ignores # comments and blanks.
    Existing real environment variables win over .env."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

# ---- Vault ----------------------------------------------------------------
VAULT_PATH = os.path.expanduser(os.getenv("VAULT_PATH", str(Path.home() / "pumpfun-vault")))

# ---- LLM provider (reuse LAIS providers verbatim) -------------------------
LLM_PROVIDER   = os.getenv("LLM_PROVIDER", "gemini")     # gemini | openai | ollama
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3")

# ---- PumpPortal -----------------------------------------------------------
# Data API (launches + migrations) is FREE and needs no key.
# A key + funded wallet (>= 0.02 SOL) is ONLY needed for metered wallet/token
# trade streams. Leave ENABLE_WALLET_TRADES off until you've seeded winners.
PUMPPORTAL_API_KEY   = os.getenv("PUMPPORTAL_API_KEY", "")
ENABLE_WALLET_TRADES = os.getenv("ENABLE_WALLET_TRADES", "false").lower() == "true"

# ---- Scoring thresholds ---------------------------------------------------
ALPHA_WATCH_THRESHOLD  = int(os.getenv("ALPHA_WATCH_THRESHOLD", "40"))   # surface to UI
ALPHA_ALERT_THRESHOLD  = int(os.getenv("ALPHA_ALERT_THRESHOLD", "70"))   # push a hard alert
# Kept for processor.py compatibility (LAIS names):
OPPORTUNITY_SAVE_THRESHOLD   = ALPHA_WATCH_THRESHOLD
OPPORTUNITY_LAUNCH_THRESHOLD = ALPHA_ALERT_THRESHOLD
