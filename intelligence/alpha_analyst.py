"""
Pump.fun Brain -- Alpha Analyst
Adapts the LAIS analyst to score on-chain signals. Same provider plumbing
(generate_json), new framing: "is this a launch worth a HUMAN looking at and possibly
buying" -- with rug risk treated as a first-class, score-lowering dimension.

Design choice that matters: this never returns "buy". It returns a watch decision plus
an honest risk read. The center surfaces it; a person decides and executes. An LLM score
on launch metadata has no contract-level rug detection edge, so turning its number
straight into spent SOL would be selling you false confidence. The score routes
attention, not money.
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from intelligence.providers import generate_json, llm_available

SYSTEM_PROMPT = """You are an on-chain alpha analyst for Pump.fun launches and whale activity.
You read raw signals -- new launches, migrations, tracked-wallet trades, and X mentions --
and judge which deserve a human's attention RIGHT NOW.

Hard rules:
- You never tell the user to buy. You produce a WATCH decision and an honest risk read.
- Rug risk is a primary, score-LOWERING dimension. A "hot" launch with red flags scores low.
- "Repeat dev" and "whale buy" raise attention but NEVER confirm safety -- they are
  routinely spoofed to bait trackers. Treat them as bait until the rest checks out.
- You cannot see the token contract. Say so. Flag what a human must verify before risking
  money (mint/freeze authority, top-holder concentration, LP status, dev sell history).

Respond ONLY with valid JSON. No markdown fences, no preamble."""

ALPHA_SCHEMA = {
    "calls": [
        {
            "mint":          "token mint address from the signal",
            "name":          "token name / ticker",
            "thesis":        "Why this might be alpha, in 2-3 plain sentences (the loop: who launched, what momentum, what tracked entity is involved)",
            "alpha_score":   60,
            "rug_risk":      "low | medium | high | unknown",
            "red_flags":     ["concrete things that lower confidence"],
            "must_verify":   ["what a human MUST check on-chain before risking SOL"],
            "watched_entity":"name of any tracked dev/whale/caller involved, or 'none'",
            "confidence":    "low | medium | high",
            "why_now":       "what makes the timing live (curve momentum, fresh launch, whale entry)",
            "source_signals":["signal title(s) this came from"],
        }
    ],
    "meta": {
        "signals_analysed":  0,
        "calls_found":       0,
        "summary":           "One sentence on the pattern across these signals.",
    },
}

SCORING_GUIDE = """
ALPHA SCORING -- score each candidate 0-100 for "worth a human's eyes now", NOT "safe to buy".

Dimensions:
  1. Momentum        -- curve filling, volume, buyer count climbing (not one dev's own buy)
  2. Tracked entity  -- a known dev/whale/caller involved (BONUS attention, NOT safety)
  3. Timing / why-now -- is the window live, or already gone
  4. Distribution    -- early signs of spread vs one wallet holding everything
  5. Survival        -- migrations / sustained trading beat a 2-minute pump
  6. Narrative       -- is there a real hook, or random noise

Then SUBTRACT for rug risk:
  - Dev's own buy is the only volume                 -> big subtract
  - Tracked "repeat dev" with no other confirmation  -> subtract (honeypot pattern)
  - Nothing verifiable, pure metadata                -> cap score, rug_risk=unknown

Bands:
  80-100: strong live momentum + multiple confirmations + low obvious rug signs
  60-79 : worth watching closely, some confirmation, risks named
  40-59 : interesting, unconfirmed, watch only
  20-39 : weak or likely bait
  0-19  : noise / obvious trap

Be conservative. Most launches are noise. Only return calls scoring >= 40.
Always populate must_verify -- you cannot see the contract, the human must."""


def _loads_loose(text: str) -> Optional[dict]:
    """Tolerant JSON parser (same approach as LAIS analyst)."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start = next((i for i, ch in enumerate(t) if ch in "{["), None)
    if start is None:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(start, len(t)):
        ch = t[j]
        if in_str:
            if esc:            esc = False
            elif ch == "\\":   esc = True
            elif ch == '"':    in_str = False
        else:
            if ch == '"':      in_str = True
            elif ch in "{[":   depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    try:    return json.loads(t[start:j + 1])
                    except Exception: return None
    return None


def _heuristic_calls(signals: List[Dict]) -> Dict:
    """KEYLESS scoring. No LLM needed. Turns launch signals into calls using the on-chain
    heuristic already computed at ingest, plus a templated rug read. Honest about its
    limits: no written thesis, no contract insight -- it routes attention from metadata
    only. This is what runs when you have no API key and no local model."""
    calls = []
    for s in signals:
        meta = s.get("meta", {}) or {}
        if meta.get("event") not in ("launch", "whale_trade", "migration"):
            continue
        score = int(meta.get("heuristic", 0) or s.get("score_raw", 0) or 0)
        if meta.get("event") == "migration":
            score = max(score, 70)  # survived the curve
        vsol = float(meta.get("vsol") or 0)
        init = float(meta.get("initial_buy") or 0)
        dev_flag = meta.get("dev_flag") or {}
        flags = []
        if vsol < 25:
            flags.append("thin bonding curve — little liquidity behind it")
        if init and vsol and vsol <= init * 1.1:
            flags.append("dev's own buy looks like most of the volume")
        if dev_flag.get("is_repeat"):
            flags.append("repeat-dev tag is also the classic honeypot setup — confirm independently")
        rug = "high" if len(flags) >= 2 else "medium" if flags else "unknown"

        calls.append({
            "mint":         meta.get("mint", ""),
            "name":         meta.get("name") or s.get("title", ""),
            "thesis":       "Heuristic surface only (no LLM): " + s.get("title", ""),
            "alpha_score":  score,
            "rug_risk":     rug,
            "red_flags":    flags,
            "must_verify":  ["mint & freeze authority revoked?",
                             "top-holder concentration",
                             "is the dev's own buy the only volume?",
                             "LP not dev-controlled"],
            "watched_entity": dev_flag.get("label", "none") if dev_flag else "none",
            "confidence":   "low",
            "why_now":      "live launch / event",
            "source_signals": [s.get("title", "")],
        })
    calls = [c for c in calls if c.get("alpha_score", 0) >= config.ALPHA_WATCH_THRESHOLD]
    return {
        "calls": calls,
        "opportunities": [{**c, "title": c.get("name", c.get("mint", "?")),
                           "score": c.get("alpha_score", 0)} for c in calls],
        "meta": {"signals_analysed": len(signals), "calls_found": len(calls),
                 "mode": "heuristic-keyless"},
    }


def analyse_signals(signals: List[Dict]) -> Dict:
    """Same entrypoint name as LAIS so processor.py needs no change.
    Falls back to keyless heuristic scoring when no LLM is configured."""
    if not signals:
        return {"calls": [], "opportunities": [],
                "meta": {"signals_analysed": 0, "calls_found": 0}}

    # KEYLESS PATH: no API key and no local model -> score from metadata, don't error.
    if not llm_available():
        return _heuristic_calls(signals)

    signal_text = "\n\n".join(
        f"[{i+1}] SOURCE: {s['source']}\n"
        f"TITLE: {s['title']}\n"
        f"CONTENT: {s.get('content','')[:700]}\n"
        f"META: {json.dumps(s.get('meta', {}))[:500]}"
        for i, s in enumerate(signals[:50])
    )

    prompt = f"""Analyse these {len(signals)} live Pump.fun / X signals.
Identify which launches or movements deserve a human's eyes right now.

For each: give the mint, a plain thesis, an alpha_score, an honest rug_risk read,
red_flags, and must_verify items (the on-chain checks a human must do before risking SOL).
You cannot see token contracts -- never imply a launch is safe.

SIGNALS:
{signal_text}

{SCORING_GUIDE}

Respond with this exact JSON structure:
{json.dumps(ALPHA_SCHEMA, indent=2)}"""

    text = generate_json(prompt, system=SYSTEM_PROMPT, max_tokens=4000)
    if not text:
        return _heuristic_calls(signals)  # provider failed -> keyless fallback, never error out

    result = _loads_loose(text)
    if result is None:
        return _heuristic_calls(signals)  # unparseable -> fall back instead of dropping signals

    calls = result.get("calls", [])
    calls = [c for c in calls if c.get("alpha_score", 0) >= config.ALPHA_WATCH_THRESHOLD]
    result["calls"] = calls
    # Mirror to "opportunities" so memory.save_opportunity / the processor stay unchanged.
    result["opportunities"] = [
        {**c, "title": c.get("name", c.get("mint", "?")), "score": c.get("alpha_score", 0)}
        for c in calls
    ]
    result.setdefault("meta", {})["calls_found"] = len(calls)
    return result
