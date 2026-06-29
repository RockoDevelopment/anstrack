#!/usr/bin/env python3
"""
ANSTRACK — dev intelligence engine.

The metric math lives in ONE place: metrics_from_aggregate(agg, now). Two callers feed it:
  - the Brain (brain.py) folds events into an aggregate incrementally as they arrive live;
  - recompute_dev() folds the same events out of the log in a batch.
Both produce byte-identical metrics, so live and replay can never diverge.

An "aggregate" is the minimal derived state needed to compute every metric:
  launches, migrations, rugs (counts), launch_ts {mint:ts}, mig_ts [(mint,ts)],
  rug_ts [ts], peak_mc {mint:mc}.  It is itself fully rebuildable from the event log.

Derived metrics: grad_rate, rug_rate, Beta-smoothed p_mig/p_rug, avg & median ATH,
avg time-to-migration, recency-weighted momentum & reputation, deterministic score.
"""
import time
import statistics

import event_log
import scoring

HALF_LIFE_SEC = 14 * 86400        # 14-day recency half-life for momentum/reputation
BETA_PRIOR_A = 1.0                 # Beta(1,4): assume tokens usually don't graduate
BETA_PRIOR_B = 4.0


def _mc_of(payload):
    if not payload:
        return 0.0
    for k in ("mc", "market_cap", "marketCap", "market_cap_usd"):
        if payload.get(k):
            try:
                return float(payload[k])
            except Exception:
                pass
    if payload.get("market_cap_sol"):
        try:
            return float(payload["market_cap_sol"])
        except Exception:
            return 0.0
    return 0.0


# ---- aggregate: the minimal rebuildable derived state ------------------------------
def blank_agg(dev):
    return {"dev": dev, "launches": 0, "migrations": 0, "rugs": 0,
            "launch_ts": {}, "mig_ts": [], "rug_ts": [], "peak_mc": {}}


def agg_apply(agg, event):
    """Fold one event into a dev aggregate. Pure; launch_ts first-write wins."""
    t = event.get("type")
    mint = event.get("mint")
    ts = event.get("ts")
    p = event.get("payload") or {}
    if t == "token_create":
        agg["launches"] += 1
        if mint and mint not in agg["launch_ts"]:
            agg["launch_ts"][mint] = ts
    elif t == "migration":
        agg["migrations"] += 1
        agg["mig_ts"].append((mint, ts))
    elif t == "rug":
        agg["rugs"] += 1
        agg["rug_ts"].append(ts)
    # ATH only for tokens this dev actually launched (ignore mc from trades on others' tokens)
    mc = _mc_of(p)
    if mint and mc > 0 and mint in agg["launch_ts"]:
        agg["peak_mc"][mint] = max(agg["peak_mc"].get(mint, 0.0), mc)
    return agg


def metrics_from_aggregate(agg, now=None):
    """The single metric definition. Pure function of the aggregate + a reference time."""
    now = float(now if now is not None else time.time())
    n_launch = agg["launches"]
    n_mig = agg["migrations"]
    n_rug = agg["rugs"]

    grad_rate = (n_mig / n_launch) if n_launch else 0.0
    rug_rate = (n_rug / n_launch) if n_launch else 0.0

    denom = (n_launch + BETA_PRIOR_A + BETA_PRIOR_B)
    p_mig = (n_mig + BETA_PRIOR_A) / denom if n_launch else BETA_PRIOR_A / (BETA_PRIOR_A + BETA_PRIOR_B)
    p_rug = (n_rug + BETA_PRIOR_A) / denom if n_launch else 0.0

    ttms = []
    for mint, mts in agg["mig_ts"]:
        born = agg["launch_ts"].get(mint)
        if born is not None and mts >= born:
            ttms.append((mts - born) / 60.0)
    ttm_avg_min = statistics.mean(ttms) if ttms else 0.0

    aths = [v for v in agg["peak_mc"].values() if v > 0]
    ath_avg = statistics.mean(aths) if aths else 0.0
    ath_median = statistics.median(aths) if aths else 0.0

    w_launch = sum(0.5 ** ((now - t) / HALF_LIFE_SEC) for t in agg["launch_ts"].values())
    w_win = sum(0.5 ** ((now - t) / HALF_LIFE_SEC) for (_, t) in agg["mig_ts"])
    w_rug = sum(0.5 ** ((now - t) / HALF_LIFE_SEC) for t in agg["rug_ts"])
    recent_grad = (w_win / w_launch) if w_launch else 0.0
    momentum = max(-1.0, min(1.0, recent_grad - grad_rate))
    reputation = max(0, min(100, round(100 * (w_win / (w_launch + 1e-9)) - 60 * (w_rug / (w_launch + 1e-9)))))

    metrics = {
        "dev": agg.get("dev"),
        "launches": n_launch, "migrations": n_mig, "rugs": n_rug,
        "grad_rate": round(grad_rate, 4), "rug_rate": round(rug_rate, 4),
        "p_mig": round(p_mig, 4), "p_rug": round(p_rug, 4),
        "ath_avg": round(ath_avg, 2), "ath_median": round(ath_median, 2),
        "ttm_avg_min": round(ttm_avg_min, 2),
        "momentum": round(momentum, 4), "reputation": reputation,
        "computed_at": now, "sample": n_launch,
    }
    metrics["score"] = scoring.score_dev(metrics)
    return metrics


# ---- batch path: build the aggregate straight from the log -------------------------
def aggregate_from_log(dev, now=None):
    agg = blank_agg(dev)
    for e in event_log.query(dev=dev, order="asc"):
        if now is not None and e["ts"] > now:
            continue
        agg_apply(agg, e)
    return agg


def recompute_dev(dev, now=None, persist=True):
    """Derive (and optionally persist) a dev's full profile from the event log."""
    m = metrics_from_aggregate(aggregate_from_log(dev, now=now), now=now)
    if persist:
        try:
            event_log.save_dev_metrics(dev, m)
        except Exception as e:
            print(f"[dev_intelligence] persist failed for {dev[:8]}: {e}")
    return m


def recompute_all(now=None, persist=True):
    out = {}
    for dev in event_log.devs_seen():
        if not dev:
            continue
        try:
            out[dev] = recompute_dev(dev, now=now, persist=persist)
        except Exception as e:
            print(f"[dev_intelligence] recompute failed for {dev[:8]}: {e}")
    return out


def profile(dev, recompute_if_missing=True):
    m = event_log.get_dev_metrics(dev)
    if m is None and recompute_if_missing:
        m = recompute_dev(dev)
    return m


if __name__ == "__main__":
    res = recompute_all(persist=False)
    print(f"recomputed {len(res)} dev profiles")
    for d, m in list(res.items())[:3]:
        print(d[:8], "->", {k: m[k] for k in ("launches", "migrations", "rugs", "grad_rate", "score")})