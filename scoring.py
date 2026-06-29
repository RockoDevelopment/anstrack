#!/usr/bin/env python3
"""
ANSTRACK — deterministic scoring framework.

Pure functions. No I/O, no globals, no time, no randomness: the same inputs always
produce the same outputs. That is what makes scoring *replayable* — replay.py can feed
historical features through score_token() and get exactly the number the live system
would have produced at that moment, which is the basis for backtesting new models.

This mirrors the client scorer (ANSTRACK.html liveScore/devScore) so the server, the
UI, and the replay engine all agree on one definition of a score.
"""

# Ceiling rises as on-chain checks are verified; you cannot reach 95 without all 6,
# nor 100 without the secured-LP star.
SCORE_CEIL = [40, 50, 59, 68, 77, 87, 95]

# Repeat-grad cap: a fresh token from a dev who has migrated before is still unproven on
# its own. The dev's record lifts their DEV score, not this token's alpha — hard cap at 46
# until the token graduates itself.
REPEAT_GRAD_CAP = 46
RUGGER_CAP = 35


def score_token(features: dict) -> int:
    """
    features keys (all optional, default 0/False):
      base          int   dev-reputation base points
      market_bonus  int    live market momentum/volume/liquidity bonus
      safety_bonus  int    on-chain safety bonus
      core_pass     int    number of the 6 core checks passed (0..6)
      has_star      bool   liquidity verifiably secured (the +5 to 100)
      dev_is_winner bool   dev has >=1 prior migration
      migrated      bool   this token itself has graduated
      rugger        bool   dev is flagged a rugger
    Returns an integer alpha score 0..100.
    """
    base = _num(features.get("base"))
    mb = _num(features.get("market_bonus"))
    sb = _num(features.get("safety_bonus"))
    signal = max(0, base + mb + sb)

    if features.get("rugger"):
        return max(0, min(RUGGER_CAP, round(signal)))

    cp = max(0, min(6, int(features.get("core_pass", 0) or 0)))
    ceiling = SCORE_CEIL[cp]
    sc = min(round(signal), ceiling)

    if features.get("has_star") and cp == 6:
        sc = 100
    if features.get("dev_is_winner") and not features.get("migrated"):
        sc = min(sc, REPEAT_GRAD_CAP)
    return int(sc)


def score_dev(metrics: dict) -> int:
    """
    Deterministic 0..100 dev score from a derived metrics dict (see dev_intelligence).
    Transparent composite: recency-weighted migration probability is primary, graduation
    count and hit-rate add, rug behaviour subtracts. Hard-capped for ruggers.
    """
    launches = _num(metrics.get("launches"))
    wins = _num(metrics.get("migrations"))
    rugs = _num(metrics.get("rugs"))
    grad_rate = _frac(metrics.get("grad_rate"))
    p_mig = _frac(metrics.get("p_mig", metrics.get("grad_rate")))
    p_rug = _frac(metrics.get("p_rug", metrics.get("rug_rate")))
    momentum = float(metrics.get("momentum", 0.0) or 0.0)  # -1..+1

    if launches >= 3 and p_rug >= 0.5:
        return max(0, min(RUGGER_CAP, int(round(20 + 30 * p_mig))))

    score = 0.0
    score += 55 * p_mig                      # primary: smoothed migration probability
    score += min(20, wins * 6)               # proven graduations (diminishing)
    score += 12 * grad_rate                   # raw hit-rate
    score += 8 * max(-1.0, min(1.0, momentum))  # improving vs decaying behaviour
    score -= 40 * p_rug                        # rug behaviour penalty
    # small confidence bump for sample size
    score += min(5, launches)
    return int(max(0, min(100, round(score))))


def _num(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _frac(v):
    try:
        v = float(v or 0)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, v))


if __name__ == "__main__":
    # repeat-grad token, 5/6 checks -> capped at 46 (the bug the UI had on first paint)
    print("repeat-grad 5/6:", score_token({"base": 80, "market_bonus": 10, "core_pass": 5, "dev_is_winner": True}))
    # same checks, not a winner -> rises to its ceiling
    print("tracked 5/6:    ", score_token({"base": 80, "market_bonus": 10, "core_pass": 5}))
    print("rugger:         ", score_token({"base": 90, "rugger": True}))
    print("dev score:      ", score_dev({"launches": 10, "migrations": 4, "rugs": 1, "grad_rate": 0.4, "p_mig": 0.38, "p_rug": 0.1, "momentum": 0.3}))