#!/usr/bin/env python3
"""
ANSTRACK — feature store (the Brain's notebook).

The Event Log is the Brain's *memory* (immutable facts). The Feature Store is the Brain's
*notes*: derived facts the Brain computes once and writes down so the API/UI/prediction
layer doesn't recompute them on every page load.

Crucial property: the store holds nothing that isn't derivable from the Event Log. Wipe it
and the Brain rebuilds every feature by replaying history. It is a cache, never a source.

Entities are namespaced strings: "token:<mint>" or "dev:<wallet>".
Each record: {entity, features{...}, computed_at, source_events}  (source_events = the log
size when computed, so staleness is detectable).

Postgres when DATABASE_URL is set, SQLite file vault otherwise (reuses the events vault).
"""
import os, json, time, sqlite3, threading
from pathlib import Path

_lock = threading.Lock()
_sqlite_conn = None
_pg_ready = False


def _vault_dir():
    try:
        import config
        base = Path(config.VAULT_PATH)
    except Exception:
        base = Path(os.getenv("VAULT_PATH", str(Path.home() / "pumpfun-vault")))
    p = base / "events"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pg():
    try:
        import db
        if db.enabled():
            conn = db._connect()
            if conn is not None:
                _ensure_pg(conn)
                return conn
    except Exception:
        pass
    return None


def _ensure_pg(conn):
    global _pg_ready
    if _pg_ready:
        return
    with conn.cursor() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS features (
                entity        TEXT PRIMARY KEY,
                features      JSONB NOT NULL,
                computed_at   DOUBLE PRECISION NOT NULL,
                source_events BIGINT
            );
            """
        )
    _pg_ready = True


def _sq():
    global _sqlite_conn
    if _sqlite_conn is None:
        conn = sqlite3.connect(str(_vault_dir() / "features.db"), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS features("
            "entity TEXT PRIMARY KEY, features TEXT, computed_at REAL, source_events INTEGER)"
        )
        conn.commit()
        _sqlite_conn = conn
    return _sqlite_conn


def put(entity, features, source_events=None):
    rec_ts = time.time()
    se = int(source_events) if source_events is not None else None
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO features(entity,features,computed_at,source_events) VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT (entity) DO UPDATE SET features=EXCLUDED.features, "
                    "computed_at=EXCLUDED.computed_at, source_events=EXCLUDED.source_events",
                    (entity, json.dumps(features), rec_ts, se),
                )
            return True
        except Exception as e:
            print(f"[feature_store] pg put failed ({e}); using file store")
    with _lock:
        c = _sq()
        c.execute("INSERT OR REPLACE INTO features(entity,features,computed_at,source_events) VALUES(?,?,?,?)",
                  (entity, json.dumps(features), rec_ts, se))
        c.commit()
    return True


def get(entity):
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute("SELECT features, computed_at, source_events FROM features WHERE entity=%s", (entity,))
                r = c.fetchone()
                if not r:
                    return None
                return {"entity": entity, "features": r[0], "computed_at": r[1], "source_events": r[2]}
        except Exception:
            pass
    with _lock:
        c = _sq()
        r = c.execute("SELECT features, computed_at, source_events FROM features WHERE entity=?", (entity,)).fetchone()
        if not r:
            return None
        return {"entity": entity, "features": json.loads(r[0]), "computed_at": r[1], "source_events": r[2]}


def features_of(entity):
    rec = get(entity)
    return rec["features"] if rec else None


def clear():
    """Wipe every cached feature (the Brain will rebuild them from the log)."""
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute("DELETE FROM features")
            return True
        except Exception:
            pass
    with _lock:
        c = _sq()
        c.execute("DELETE FROM features")
        c.commit()
    return True


def count():
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute("SELECT count(*) FROM features")
                return int(c.fetchone()[0])
        except Exception:
            pass
    with _lock:
        c = _sq()
        return int(c.execute("SELECT count(*) FROM features").fetchone()[0])


def token(mint):
    return features_of("token:" + mint)


def dev(wallet):
    return features_of("dev:" + wallet)


if __name__ == "__main__":
    put("token:TESTMINT", {"liq_slope": 1.2, "holder_growth": 0.3}, source_events=10)
    print("token:", token("TESTMINT"), "| count:", count())
    clear()
    print("after clear count:", count())