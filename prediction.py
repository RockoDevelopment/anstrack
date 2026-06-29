#!/usr/bin/env python3
"""
ANSTRACK — prediction engine + confidence layer.

Memory becomes a forecast. For a token we build a feature vector, ask the Brain's analog
index for the nearest historical launches, and combine:

  analog signal  : how often the most-similar past launches migrated
  dev signal     : the launching dev's Beta-smoothed migration probability (p_mig)

into a single migration probability. Alongside it we report CONFIDENCE — not how high the
probability is, but how much evidence stands behind it:

  - many close analogs + a big dev sample  -> high confidence
  - few/distant analogs + a thin sample    -> low confidence, even for a bold number

Everything is derived from the Event Log via the Brain, so predictions are reproducible.
"""
import math

import brain
import dev_intelligence
import event_log


def _token_features(mint):
    """Build the [dev_grad_rate, peak_mc, ttm_min] feature vector the analog index uses."""
    tb = brain.token_brain(mint) or {}
    dev = tb.get("dev")
    devm = brain.dev_brain(dev) if dev else None
    grad = float((devm or {}).get("grad_rate", 0.0))
    peak = float(tb.get("peak_mc", 0.0))
    born = tb.get("born")
    last = tb.get("last_ts")
    ttm = ((last - born) / 60.0) if (born and last) else 0.0
    return [grad, peak, ttm], dev, devm, tb


def predict_token(mint, k=8):
    """Return a migration probability + confidence + the evidence behind it."""
    feats, dev, devm, tb = _token_features(mint)
    ana = brain.find_analogs(feats, k=k)
    matches = ana.get("matches", [])
    n = ana.get("n", 0)

    analog_p = ana.get("migrated_rate")             # None if no analogs
    dev_p = float((devm or {}).get("p_mig", 0.0)) if devm else None
    dev_sample = int((devm or {}).get("sample", 0)) if devm else 0

    # closeness: nearer analogs -> more trustworthy (distance is normalized 0..~1.7)
    avg_dist = (sum(m.get("distance", 1.0) for m in matches) / len(matches)) if matches else 1.0
    closeness = max(0.0, 1.0 - min(1.0, avg_dist))

    # blend analog and dev signals by how much evidence each carries
    parts, weights = [], []
    if analog_p is not None:
        parts.append(analog_p); weights.append(0.6 * (0.4 + 0.6 * closeness) * min(1.0, n / 5.0))
    if dev_p is not None:
        parts.append(dev_p); weights.append(0.4 * min(1.0, dev_sample / 4.0) + 0.1)
    if parts and sum(weights) > 0:
        prob = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    elif dev_p is not None:
        prob = dev_p
    else:
        prob = 0.0

    # confidence: evidence volume (n, dev_sample) * closeness, squashed to 0..1
    evidence = n + dev_sample
    confidence = (1 - math.exp(-evidence / 6.0)) * (0.5 + 0.5 * closeness)
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    return {
        "mint": mint, "dev": dev,
        "migration_probability": round(max(0.0, min(1.0, prob)), 3),
        "confidence": confidence,
        "basis": {
            "analog_migrated_rate": analog_p, "analogs": n, "analog_closeness": round(closeness, 3),
            "dev_p_mig": round(dev_p, 3) if dev_p is not None else None, "dev_sample": dev_sample,
            "nearest": [{"mint": m["mint"], "outcome": m["outcome"], "peak_mc": m["peak_mc"],
                         "distance": m["distance"]} for m in matches[:5]],
        },
    }


def predict_from_features(grad_rate, peak_mc, ttm_min, dev_p_mig=None, dev_sample=0, k=8):
    """Predict for a hypothetical/feature-only token (no mint needed) — used for what-ifs."""
    ana = brain.find_analogs([grad_rate, peak_mc, ttm_min], k=k)
    n = ana.get("n", 0)
    analog_p = ana.get("migrated_rate")
    matches = ana.get("matches", [])
    avg_dist = (sum(m.get("distance", 1.0) for m in matches) / len(matches)) if matches else 1.0
    closeness = max(0.0, 1.0 - min(1.0, avg_dist))
    parts, weights = [], []
    if analog_p is not None:
        parts.append(analog_p); weights.append(0.6 * (0.4 + 0.6 * closeness) * min(1.0, n / 5.0))
    if dev_p_mig is not None:
        parts.append(dev_p_mig); weights.append(0.4 * min(1.0, dev_sample / 4.0) + 0.1)
    prob = (sum(p * w for p, w in zip(parts, weights)) / sum(weights)) if (parts and sum(weights) > 0) else (dev_p_mig or 0.0)
    evidence = n + dev_sample
    confidence = round(max(0.0, min(1.0, (1 - math.exp(-evidence / 6.0)) * (0.5 + 0.5 * closeness))), 3)
    return {"migration_probability": round(max(0.0, min(1.0, prob)), 3), "confidence": confidence,
            "analogs": n, "analog_migrated_rate": analog_p}


if __name__ == "__main__":
    brain.ensure_built()
    print(predict_from_features(0.5, 60000, 1440, dev_p_mig=0.4, dev_sample=4))