#!/usr/bin/env python3
"""
ANSTRACK — replay engine.

The Event Log is the source of truth; everything else is a projection of it. Replay does
NOT contain its own intelligence logic — it drives the SAME pieces live mode uses:

  reconstruct_dev_at(dev, ts)   -> dev_intelligence.recompute_dev(dev, now=ts)   (same fold)
  reconstruct_brain_at(ts)      -> brain.rebuild_to(ts)                           (same handle)
  replay_token(mint)            -> projection of the token's events
  replay_range(start, end)      -> aggregate window + deterministic re-score (scoring.py)

Because reconstruct_brain_at() feeds the log through brain.handle() — the exact function
that runs live — replaying history to time T produces the same Brain state that existed at
time T. There is no separate replay code path.
"""
import time

import event_log
import scoring
import dev_intelligence
import brain


def reconstruct_dev_at(dev, ts):
    """A dev's derived metrics as they would have been at `ts` (same fold as live)."""
    return dev_intelligence.recompute_dev(dev, now=ts, persist=False)


def reconstruct_brain_at(ts):
    """Reconstruct the WHOLE Brain state at `ts` by replaying the log through brain.handle()."""
    brain.rebuild_to(ts)
    snap = brain.snapshot()
    snap["as_of"] = ts
    return snap


def replay_token(mint):
    """A token's full lifecycle: ordered events + a derived summary (peak mc, lifespan)."""
    evs = event_log.query(mint=mint, order="asc")
    peak_mc = 0.0
    born = migrated_at = rugged_at = last = dev = None
    for e in evs:
        dev = dev or e.get("dev")
        last = e["ts"]
        if e["type"] == "token_create" and born is None:
            born = e["ts"]
        if e["type"] == "migration" and migrated_at is None:
            migrated_at = e["ts"]
        if e["type"] == "rug" and rugged_at is None:
            rugged_at = e["ts"]
        mc = dev_intelligence._mc_of(e.get("payload"))
        if mc > peak_mc:
            peak_mc = mc
    summary = {
        "mint": mint, "dev": dev, "events": len(evs),
        "born": born, "migrated_at": migrated_at, "rugged_at": rugged_at,
        "peak_mc": round(peak_mc, 2),
        "lifespan_min": round(((last - born) / 60.0), 2) if (born and last) else None,
        "outcome": ("rugged" if rugged_at else "migrated" if migrated_at else "open"),
    }
    return {"summary": summary, "timeline": evs}


def replay_range(start, end, rescore=False):
    """Fold a window of history. With rescore=True, deterministically re-score every token
    at its outcome using the CURRENT scoring model — the core of model backtesting."""
    evs = event_log.query(since=start, until=end, order="asc")
    agg = {"events": len(evs), "launches": 0, "migrations": 0, "rugs": 0, "by_type": {}}
    per_mint = {}
    for e in evs:
        agg["by_type"][e["type"]] = agg["by_type"].get(e["type"], 0) + 1
        if e["type"] == "token_create":
            agg["launches"] += 1
        elif e["type"] == "migration":
            agg["migrations"] += 1
        elif e["type"] == "rug":
            agg["rugs"] += 1
        m = e["mint"]
        if not m:
            continue
        slot = per_mint.setdefault(m, {"dev": e.get("dev"), "peak_mc": 0.0, "migrated": False, "rugged": False})
        slot["peak_mc"] = max(slot["peak_mc"], dev_intelligence._mc_of(e.get("payload")))
        if e["type"] == "migration":
            slot["migrated"] = True
        if e["type"] == "rug":
            slot["rugged"] = True

    out = {"window": {"start": start, "end": end}, "aggregate": agg, "tokens": len(per_mint)}
    if rescore:
        rescored = []
        for mint, slot in per_mint.items():
            feats = {
                "base": 60 if slot["migrated"] else 30,
                "market_bonus": 20 if slot["peak_mc"] > 50000 else 0,
                "core_pass": 6 if slot["migrated"] else 3,
                "migrated": slot["migrated"],
                "rugger": slot["rugged"],
            }
            rescored.append({"mint": mint, "dev": slot["dev"], "peak_mc": round(slot["peak_mc"], 2),
                             "outcome": "rugged" if slot["rugged"] else "migrated" if slot["migrated"] else "open",
                             "score": scoring.score_token(feats)})
        rescored.sort(key=lambda r: r["score"], reverse=True)
        out["rescored"] = rescored
    return out


def system_state(now=None):
    now = float(now if now is not None else time.time())
    return {
        "events": event_log.count(),
        "devs": len(event_log.devs_seen()),
        "mints": len(event_log.mints_seen()),
        "backend": event_log.backend(),
        "as_of": now,
    }


if __name__ == "__main__":
    print("system:", system_state())
    print("brain @ now:", reconstruct_brain_at(time.time()))