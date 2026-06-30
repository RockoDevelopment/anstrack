#!/usr/bin/env python3
"""
ANSTRACK — the Brain (event-driven consumer of the Event Log).

The Event Log is the single source of truth. The Brain never reads PumpPortal and never
keeps hidden state: it consumes EVENTS and maintains only *projections* that are 100%
rebuildable from the log. Delete every projection and call rebuild() — the entire Brain
comes back identical.

ONE pipeline for live and replay
  - Live:   event_log.append(...) -> _notify -> brain.handle(event)   (subscribed below)
  - Replay: rebuild() clears projections and feeds the log through the SAME handle()
  There is no separate replay code path, so live state and replayed state are identical.

Event dispatch (handle -> _DISPATCH[type]):
  token_create     -> dev memory + token memory (born)            -> recompute dev
  trade            -> token memory (mc point) + outcome stats
  migration        -> dev memory + token memory (graduated)       -> recompute dev + analog
  rug              -> dev memory + token memory (rugged)          -> recompute dev + analog
  market_snapshot  -> token memory (mc/liq time-series point)
  dev_assoc        -> ensure dev<->token link
  score            -> store latest scoring snapshot (audit)

Projections it maintains (all derived):
  dev memory          _dev[dev]      -> dev_intelligence aggregate (+ cached metrics)
  token memory        _token[mint]   -> born/peak_mc/last_mc/migrated/rugged/lifespan/dev
  behavioral features                -> exposed via dev metrics (cadence, ttm, ath dist, momentum)
  reputation trends                  -> momentum/reputation in dev metrics (recency-decayed)
  outcome statistics  _outcome       -> global counters + grad/rug rates
  historical analog index _analogs   -> feature vectors of completed tokens, for retrieval
  cached inference state _dev_cache  -> last computed dev metrics (pure cache)
"""
import time
import threading

import event_log
import dev_intelligence
import scoring
import feature_store

_lock = threading.RLock()
_suspend_features = False   # during rebuild we skip per-event cache writes, then flush once
_processed = 0

# ---- projections (rebuildable from the log) ----------------------------------------
_dev = {}        # dev   -> dev_intelligence aggregate
_dev_cache = {}  # dev   -> last computed metrics
_token = {}      # mint  -> token-memory projection
_analogs = []    # list of {mint, dev, features:[...], outcome, peak_mc, ttm_min}
_outcome = {"launches": 0, "migrations": 0, "rugs": 0, "trades": 0, "snapshots": 0}
_built = False
_last_build = 0.0
_MAX_ANALOGS = 5000


def _reset():
    with _lock:
        _dev.clear(); _dev_cache.clear(); _token.clear(); _analogs.clear()
        for k in _outcome:
            _outcome[k] = 0


def _tok(mint, dev=None):
    t = _token.get(mint)
    if t is None:
        t = {"mint": mint, "dev": dev, "born": None, "peak_mc": 0.0, "last_mc": 0.0,
             "migrated": False, "migrated_at": None, "rugged": False, "rugged_at": None,
             "last_ts": None, "points": 0}
        _token[mint] = t
    if dev and not t.get("dev"):
        t["dev"] = dev
    return t


def _touch_token(ev):
    """Generic token-memory update (mc/liq time-series) for any event carrying a mint."""
    mint = ev.get("mint")
    if not mint:
        return None
    t = _tok(mint, ev.get("dev"))
    mc = dev_intelligence._mc_of(ev.get("payload"))
    if mc > 0:
        t["last_mc"] = mc
        t["peak_mc"] = max(t["peak_mc"], mc)
    t["last_ts"] = ev.get("ts")
    t["points"] += 1
    return t


def _recompute_dev(dev, now):
    """Refresh a dev's cached metrics from its (incrementally maintained) aggregate."""
    if not dev:
        return
    agg = _dev.get(dev)
    if agg is None:
        return
    m = dev_intelligence.metrics_from_aggregate(agg, now=now)
    _dev_cache[dev] = m
    if not _suspend_features:
        try:
            event_log.save_dev_metrics(dev, m)            # pure cache write (rebuildable)
            feature_store.put("dev:" + dev, m, source_events=_processed)
        except Exception:
            pass


def _write_token_features(mint):
    if _suspend_features or not mint:
        return
    tb = token_brain(mint)
    if not tb:
        return
    try:
        feats = {
            "peak_mc": tb.get("peak_mc", 0.0), "last_mc": tb.get("last_mc", 0.0),
            "lifespan_min": tb.get("lifespan_min"), "outcome": tb.get("outcome"),
            "migrated": tb.get("migrated"), "rugged": tb.get("rugged"),
            "readings": tb.get("points", 0), "dev": tb.get("dev"),
        }
        feature_store.put("token:" + mint, feats, source_events=_processed)
    except Exception:
        pass


def _dev_agg(dev):
    a = _dev.get(dev)
    if a is None:
        a = dev_intelligence.blank_agg(dev)
        _dev[dev] = a
    return a


def _add_analog(t, outcome):
    """Record a completed token as a retrievable historical analog."""
    born = t.get("born")
    ttm = ((t.get("migrated_at") or t.get("rugged_at") or t.get("last_ts") or 0) - born) / 60.0 if born else 0.0
    devm = _dev_cache.get(t.get("dev")) or {}
    feats = [
        float(devm.get("grad_rate", 0.0)),     # dev quality at the time
        float(t.get("peak_mc", 0.0)),          # how high it ran
        float(ttm),                            # how fast it resolved
    ]
    _analogs.append({"mint": t["mint"], "dev": t.get("dev"), "features": feats,
                     "outcome": outcome, "peak_mc": round(t.get("peak_mc", 0.0), 2),
                     "ttm_min": round(ttm, 2)})
    if len(_analogs) > _MAX_ANALOGS:
        del _analogs[0:len(_analogs) - _MAX_ANALOGS]


# ---- per-type handlers --------------------------------------------------------------
def _on_create(ev):
    dev = ev.get("dev")
    dev_intelligence.agg_apply(_dev_agg(dev), ev) if dev else None
    t = _touch_token(ev)
    if t and t["born"] is None:
        t["born"] = ev.get("ts")
    _outcome["launches"] += 1
    _recompute_dev(dev, ev.get("ts"))
    _write_token_features(ev.get("mint"))


def _on_trade(ev):
    dev = ev.get("dev")
    if dev:
        dev_intelligence.agg_apply(_dev_agg(dev), ev)  # captures mc point (ATH) for own tokens
    _touch_token(ev)
    _outcome["trades"] += 1


def _on_snapshot(ev):
    dev = ev.get("dev")
    if dev:
        dev_intelligence.agg_apply(_dev_agg(dev), ev)  # market-cap time-series point -> ATH
    _touch_token(ev)
    _outcome["snapshots"] += 1


def _on_migration(ev):
    dev = ev.get("dev")
    if not dev:
        # migration events usually omit the deployer wallet; recover it from the token's launch (held in token
        # memory, set by the token_create event earlier in the log). This makes graduations credit the right dev on
        # every rebuild from the log alone -- not dependent on a live in-memory launch map that's empty after restart.
        mint = ev.get("mint")
        t = _token.get(mint) if mint else None
        dev = (t or {}).get("dev")
    if dev:
        dev_intelligence.agg_apply(_dev_agg(dev), ev)
    t = _touch_token(ev)
    if t:
        t["migrated"] = True
        t["migrated_at"] = ev.get("ts")
        _add_analog(t, "migrated")
    _outcome["migrations"] += 1
    _recompute_dev(dev, ev.get("ts"))
    _write_token_features(ev.get("mint"))


def _on_rug(ev):
    dev = ev.get("dev")
    if not dev:
        mint = ev.get("mint")
        t = _token.get(mint) if mint else None
        dev = (t or {}).get("dev")
    if dev:
        dev_intelligence.agg_apply(_dev_agg(dev), ev)
    t = _touch_token(ev)
    if t:
        t["rugged"] = True
        t["rugged_at"] = ev.get("ts")
        _add_analog(t, "rugged")
    _outcome["rugs"] += 1
    _recompute_dev(dev, ev.get("ts"))
    _write_token_features(ev.get("mint"))


def _on_assoc(ev):
    _tok(ev.get("mint"), ev.get("dev"))


def _on_score(ev):
    t = _tok(ev.get("mint"), ev.get("dev"))
    t["last_score"] = (ev.get("payload") or {}).get("score")


_DISPATCH = {
    "token_create": _on_create,
    "trade": _on_trade,
    "market_snapshot": _on_snapshot,
    "migration": _on_migration,
    "rug": _on_rug,
    "dev_assoc": _on_assoc,
    "score": _on_score,
}


# ---- the single entry point (live AND replay use this) ------------------------------
def handle(event):
    event = event_log.migrate_event(event)   # always read through the current schema
    fn = _DISPATCH.get(event.get("type"))
    if fn is None:
        return
    with _lock:
        global _processed
        _processed += 1
        fn(event)


def rebuild():
    """Clear every projection and replay the whole log through handle(). Proves the Brain
    is fully reproducible from the Event Log alone."""
    return rebuild_to(None)


def rebuild_to(cutoff_ts):
    """Replay history through the SAME handle() up to cutoff_ts (None = all). This is the
    replay pipeline — identical to live, just bounded in time."""
    global _built, _last_build, _suspend_features, _processed
    with _lock:
        _reset()
        _processed = 0
        _suspend_features = True            # don't write the feature cache per-event during replay
        try:
            for e in event_log.query(until=cutoff_ts, order="asc"):
                handle(e)
        finally:
            _suspend_features = False
        _flush_features()                   # write the notebook once, at the end
        _built = True
        _last_build = time.time()
    return outcome_stats()


def _flush_features():
    """Write every dev + token feature row once (used after a replay rebuild)."""
    now = time.time()
    for dev, agg in list(_dev.items()):
        m = dev_intelligence.metrics_from_aggregate(agg, now=now)
        _dev_cache[dev] = m
        try:
            event_log.save_dev_metrics(dev, m)
            feature_store.put("dev:" + dev, m, source_events=_processed)
        except Exception:
            pass
    for mint in list(_token.keys()):
        _write_token_features(mint)


def ensure_built():
    if not _built:
        rebuild()


def ensure_fresh(ttl=20.0):
    """For a read-replica process (the API server): rebuild from the log if the projection
    is older than ttl seconds. Live consumers (ingestion) never need this."""
    if (not _built) or (time.time() - _last_build) > ttl:
        rebuild()


# ---- read API (projections) ---------------------------------------------------------
def dev_brain(dev, now=None):
    with _lock:
        agg = _dev.get(dev)
        if agg is not None:
            return dev_intelligence.metrics_from_aggregate(agg, now=now)
    # not in memory (e.g. fresh server process) -> derive from the log
    return dev_intelligence.recompute_dev(dev, now=now, persist=False)


def token_brain(mint):
    with _lock:
        t = _token.get(mint)
        if not t:
            return None
        out = dict(t)
    born = out.get("born")
    end = out.get("migrated_at") or out.get("rugged_at") or out.get("last_ts")
    out["lifespan_min"] = round((end - born) / 60.0, 2) if (born and end) else None
    out["outcome"] = "rugged" if out["rugged"] else "migrated" if out["migrated"] else "open"
    return out


def outcome_stats():
    with _lock:
        s = dict(_outcome)
    s["grad_rate"] = round(s["migrations"] / s["launches"], 4) if s["launches"] else 0.0
    s["rug_rate"] = round(s["rugs"] / s["launches"], 4) if s["launches"] else 0.0
    s["devs"] = len(_dev)
    s["tokens"] = len(_token)
    s["analogs"] = len(_analogs)
    return s


def find_analogs(features, k=5):
    """Nearest historical launches to a feature vector [grad_rate, peak_mc, ttm_min].
    Returns the closest completed tokens + the outcome mix (the basis for the WHY engine)."""
    with _lock:
        rows = list(_analogs)
    if not rows or not features:
        return {"matches": [], "migrated_rate": None, "n": 0}

    def norm(v):
        return [float(v[0] or 0) / 1.0, float(v[1] or 0) / 1e5, float(v[2] or 0) / 1440.0]
    fq = norm(features)
    scored = []
    for r in rows:
        fr = norm(r["features"])
        d = sum((a - b) ** 2 for a, b in zip(fq, fr)) ** 0.5
        scored.append((d, r))
    scored.sort(key=lambda x: x[0])
    top = [dict(r, distance=round(d, 4)) for d, r in scored[:k]]
    migs = sum(1 for r in top if r["outcome"] == "migrated")
    return {"matches": top, "migrated_rate": round(migs / len(top), 3) if top else None, "n": len(top)}


def snapshot():
    """Full Brain telemetry projection (for /api/brain)."""
    return {"outcome": outcome_stats(), "built": _built, "backend": event_log.backend(),
            "events": event_log.count(), "features": feature_store.count(),
            "schema_version": event_log.SCHEMA_VERSION}


# ---- subscribe so EVERY appended event triggers the Brain (the one dispatch point) ---
event_log.subscribe(handle)


if __name__ == "__main__":
    rebuild()
    print("brain snapshot:", snapshot())