#!/usr/bin/env python3
"""
ANSTRACK — WHY engine.

Turns the Brain's state + prediction into plain-language reasons, so the UI can show
"here's why" instead of a bare number. Every line is grounded in a recorded fact or a
derived metric — no invented narrative.

explain(mint) returns:
  reasons      : ordered human-readable strings
  prediction   : the prediction-engine output (probability + confidence)
  score_note   : why the alpha number is what it is (e.g. the repeat-grad cap)
"""
import brain
import prediction
import scoring


def _pct(x):
    try:
        return f"{round(float(x) * 100)}%"
    except Exception:
        return "?"


def explain(mint):
    tb = brain.token_brain(mint) or {}
    dev = tb.get("dev")
    devm = brain.dev_brain(dev) if dev else None
    pred = prediction.predict_token(mint)

    reasons = []

    # 1) score cap reasoning (the long-standing "why 46?" question)
    if devm and devm.get("migrations", 0) >= 1 and not tb.get("migrated"):
        reasons.append(
            f"Alpha is capped at {scoring.REPEAT_GRAD_CAP} because this dev has graduated "
            f"{devm['migrations']} token(s) before — their record lifts the DEV score, not this "
            f"token's alpha, until it graduates itself.")

    # 2) dev track record
    if devm:
        if devm.get("launches"):
            reasons.append(
                f"Dev has launched {devm['launches']}, graduated {devm['migrations']} "
                f"({_pct(devm.get('grad_rate'))} hit rate), rugged {devm['rugs']} "
                f"({_pct(devm.get('rug_rate'))}).")
        mo = devm.get("momentum", 0.0)
        if mo > 0.05:
            reasons.append(f"Dev momentum is improving — recent graduation rate is up ({mo:+.2f}).")
        elif mo < -0.05:
            reasons.append(f"Dev momentum is decaying — recent graduation rate is down ({mo:+.2f}).")
        if devm.get("ath_median"):
            reasons.append(f"Their tokens' median peak market cap is about ${int(devm['ath_median']):,}.")

    # 3) live trajectory from token memory (time-series)
    if tb.get("peak_mc"):
        reasons.append(f"Peak market cap observed so far: ${int(tb['peak_mc']):,}.")
    if tb.get("points", 0) >= 3:
        trend = "up" if (tb.get("last_mc", 0) >= tb.get("peak_mc", 0) * 0.9) else "off its peak"
        reasons.append(f"Market cap is currently {trend} across {tb['points']} recorded readings.")

    # 4) analog / pattern recognition
    b = pred["basis"]
    if b["analogs"]:
        reasons.append(
            f"This launch resembles {b['analogs']} past launches "
            f"(closeness {_pct(b['analog_closeness'])}); {_pct(b['analog_migrated_rate'])} of them migrated.")

    # 5) the prediction itself, with confidence
    conf_word = "high" if pred["confidence"] >= 0.66 else "moderate" if pred["confidence"] >= 0.33 else "low"
    reasons.append(
        f"Estimated migration probability {_pct(pred['migration_probability'])} "
        f"at {conf_word} confidence ({_pct(pred['confidence'])}) — "
        f"based on {b['analogs']} analogs and a dev sample of {b['dev_sample']}.")

    if not reasons:
        reasons.append("Not enough recorded history on this token or dev yet to explain a score.")

    return {
        "mint": mint, "dev": dev,
        "reasons": reasons,
        "prediction": pred,
        "outcome": tb.get("outcome"),
    }


if __name__ == "__main__":
    brain.ensure_built()
    print("why engine ready (needs live data to explain a specific mint)")