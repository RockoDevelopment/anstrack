#!/usr/bin/env python3
"""
ANSTRACK — append-only event log (the memory backbone).

Every meaningful thing that happens is recorded here as an immutable event:
    token_create     a new launch (dev -> mint association is implicit)
    market_snapshot  a point-in-time (mc, liq, holders, vol) reading for a mint
    migration        a token graduated off the bonding curve  (a dev "win")
    rug              a token was detected as rugged/crashed
    trade            an observed buy/sell by a tracked wallet
    dev_assoc        explicit dev <-> mint association
    score            a scoring snapshot (optional, for audit/backtest)

Design guarantees
  - APPEND-ONLY: rows are never updated or deleted. State is *derived* by replay.
  - Full historical replay: every row carries (ts, type, mint, dev, payload).
  - Queryable by mint, by dev, and by time range (indexed).
  - Postgres when DATABASE_URL is set (survives redeploys); SQLite file vault otherwise.

This module stores raw events. dev_intelligence.py derives metrics from them and
replay.py reconstructs any entity's state at any timestamp. Nothing here depends on
those modules, so the log is the single source of truth.
"""
import os, json, time, sqlite3, threading
from pathlib import Path

# Known event types (append() does not reject unknown types — the log must never
# silently drop an event — but these are the ones the rest of the system reasons about).
EVENT_TYPES = (
    "token_create", "market_snapshot", "migration", "rug", "trade", "dev_assoc", "score",
)

# Event schema version. Every appended event is stamped with this in payload["_v"].
# When the schema evolves, bump this and teach migrate_event() how to upgrade older
# payloads — replay then reads decade-old events through the current code path.
SCHEMA_VERSION = 1


def migrate_event(ev):
    """Upgrade any event to the current schema before consumers see it. Old events with no
    version are treated as v0 and brought forward. Identity today; the hook exists so adding
    fields later never breaks replay of historical data."""
    p = ev.get("payload") or {}
    v = p.get("_v", 0)
    if v == SCHEMA_VERSION:
        return ev
    # --- migration ladder (v0 -> 1 ...) ---
    # v0 had no version field and no schema changes vs v1; just stamp it.
    p = dict(p)
    p["_v"] = SCHEMA_VERSION
    ev = dict(ev)
    ev["payload"] = p
    return ev

_sql_lock = threading.Lock()
_sqlite_conn = None
_pg_ready = False

# Event dispatch: consumers (the Brain) register here and are notified of every appended
# event. This is the single trigger point — nothing consumes raw websocket messages, only
# events. Replay does NOT go through here (it calls the consumer's handler directly while
# reading the log), so a consumer's live path and replay path run the same handler code.
_subscribers = []


def subscribe(fn):
    """Register a consumer fn(event_dict) called on every future append."""
    if fn not in _subscribers:
        _subscribers.append(fn)


def _notify(ev):
    for fn in list(_subscribers):
        try:
            fn(ev)
        except Exception as e:
            print(f"[event_log] subscriber error: {e}")


def _vault_dir() -> Path:
    try:
        import config
        base = Path(config.VAULT_PATH)
    except Exception:
        base = Path(os.getenv("VAULT_PATH", str(Path.home() / "pumpfun-vault")))
    p = base / "events"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- Postgres path (reuses db.py's cached connection) -------------------------------
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
            CREATE TABLE IF NOT EXISTS events (
                id      BIGSERIAL PRIMARY KEY,
                ts      DOUBLE PRECISION NOT NULL,
                type    TEXT NOT NULL,
                mint    TEXT,
                dev     TEXT,
                payload JSONB
            );
            CREATE INDEX IF NOT EXISTS events_mint_idx ON events(mint);
            CREATE INDEX IF NOT EXISTS events_dev_idx  ON events(dev);
            CREATE INDEX IF NOT EXISTS events_ts_idx   ON events(ts);
            CREATE INDEX IF NOT EXISTS events_type_idx ON events(type);
            CREATE TABLE IF NOT EXISTS dev_metrics (
                dev        TEXT PRIMARY KEY,
                metrics    JSONB NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            );
            """
        )
    _pg_ready = True


# ---- SQLite fallback ----------------------------------------------------------------
def _sq():
    global _sqlite_conn
    if _sqlite_conn is None:
        conn = sqlite3.connect(str(_vault_dir() / "events.db"), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, mint TEXT, dev TEXT, payload TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ev_mint ON events(mint)")
        conn.execute("CREATE INDEX IF NOT EXISTS ev_dev  ON events(dev)")
        conn.execute("CREATE INDEX IF NOT EXISTS ev_ts   ON events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS ev_type ON events(type)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dev_metrics("
            "dev TEXT PRIMARY KEY, metrics TEXT, updated_at REAL)"
        )
        conn.commit()
        _sqlite_conn = conn
    return _sqlite_conn


# ---- write (append-only) ------------------------------------------------------------
def append(type, mint=None, dev=None, payload=None, ts=None):
    """Append one immutable event, then notify every subscriber. Returns True on success."""
    ts = float(ts if ts is not None else time.time())
    payload = dict(payload or {})
    payload.setdefault("_v", SCHEMA_VERSION)
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO events(ts,type,mint,dev,payload) VALUES(%s,%s,%s,%s,%s)",
                    (ts, type, mint, dev, json.dumps(payload)),
                )
            _notify({"ts": ts, "type": type, "mint": mint, "dev": dev, "payload": payload})
            return True
        except Exception as e:
            print(f"[event_log] pg append failed ({e}); using file log")
    with _sql_lock:
        c = _sq()
        c.execute(
            "INSERT INTO events(ts,type,mint,dev,payload) VALUES(?,?,?,?,?)",
            (ts, type, mint, dev, json.dumps(payload)),
        )
        c.commit()
    _notify({"ts": ts, "type": type, "mint": mint, "dev": dev, "payload": payload})
    return True


def _row(r, pg):
    pid, ts, typ, mint, dev, payload = r
    if not pg:
        try:
            payload = json.loads(payload) if payload else {}
        except Exception:
            payload = {}
    return migrate_event({"id": pid, "ts": ts, "type": typ, "mint": mint, "dev": dev, "payload": payload or {}})


# ---- read / query -------------------------------------------------------------------
def query(mint=None, dev=None, types=None, since=None, until=None, limit=200000, order="asc"):
    """Return events (oldest-first by default) filtered by mint/dev/type/time-range."""
    od = "ASC" if order == "asc" else "DESC"
    conn = _pg()
    if conn is not None:
        where, args = [], []
        if mint: where.append("mint=%s"); args.append(mint)
        if dev: where.append("dev=%s"); args.append(dev)
        if types: where.append("type = ANY(%s)"); args.append(list(types))
        if since is not None: where.append("ts>=%s"); args.append(float(since))
        if until is not None: where.append("ts<=%s"); args.append(float(until))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        try:
            with conn.cursor() as c:
                c.execute(
                    f"SELECT id,ts,type,mint,dev,payload FROM events{w} "
                    f"ORDER BY ts {od}, id {od} LIMIT %s",
                    args + [limit],
                )
                return [_row(r, True) for r in c.fetchall()]
        except Exception as e:
            print(f"[event_log] pg query failed ({e}); using file log")
    where, args = [], []
    if mint: where.append("mint=?"); args.append(mint)
    if dev: where.append("dev=?"); args.append(dev)
    if types: where.append("type IN (%s)" % ",".join("?" * len(types))); args += list(types)
    if since is not None: where.append("ts>=?"); args.append(float(since))
    if until is not None: where.append("ts<=?"); args.append(float(until))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with _sql_lock:
        c = _sq()
        cur = c.execute(
            f"SELECT id,ts,type,mint,dev,payload FROM events{w} ORDER BY ts {od}, id {od} LIMIT ?",
            args + [limit],
        )
        return [_row(r, False) for r in cur.fetchall()]


def devs_seen():
    """Distinct dev wallets that appear in the log."""
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute("SELECT DISTINCT dev FROM events WHERE dev IS NOT NULL AND dev<>''")
                return [r[0] for r in c.fetchall()]
        except Exception:
            pass
    with _sql_lock:
        c = _sq()
        return [r[0] for r in c.execute(
            "SELECT DISTINCT dev FROM events WHERE dev IS NOT NULL AND dev<>''").fetchall()]


def mints_seen(dev=None):
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                if dev:
                    c.execute("SELECT DISTINCT mint FROM events WHERE dev=%s AND mint IS NOT NULL", (dev,))
                else:
                    c.execute("SELECT DISTINCT mint FROM events WHERE mint IS NOT NULL")
                return [r[0] for r in c.fetchall()]
        except Exception:
            pass
    with _sql_lock:
        c = _sq()
        if dev:
            cur = c.execute("SELECT DISTINCT mint FROM events WHERE dev=? AND mint IS NOT NULL", (dev,))
        else:
            cur = c.execute("SELECT DISTINCT mint FROM events WHERE mint IS NOT NULL")
        return [r[0] for r in cur.fetchall()]


def count():
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute("SELECT count(*) FROM events")
                return int(c.fetchone()[0])
        except Exception:
            pass
    with _sql_lock:
        c = _sq()
        return int(c.execute("SELECT count(*) FROM events").fetchone()[0])


# ---- derived dev-metrics cache (written by dev_intelligence, read by the server) -----
def save_dev_metrics(dev, metrics):
    ts = time.time()
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO dev_metrics(dev,metrics,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT (dev) DO UPDATE SET metrics=EXCLUDED.metrics, updated_at=EXCLUDED.updated_at",
                    (dev, json.dumps(metrics), ts),
                )
            return True
        except Exception as e:
            print(f"[event_log] metrics save failed ({e}); using file log")
    with _sql_lock:
        c = _sq()
        c.execute("INSERT OR REPLACE INTO dev_metrics(dev,metrics,updated_at) VALUES(?,?,?)",
                  (dev, json.dumps(metrics), ts))
        c.commit()
    return True


def get_dev_metrics(dev=None):
    conn = _pg()
    if conn is not None:
        try:
            with conn.cursor() as c:
                if dev:
                    c.execute("SELECT metrics FROM dev_metrics WHERE dev=%s", (dev,))
                    r = c.fetchone()
                    return r[0] if r else None
                c.execute("SELECT dev, metrics FROM dev_metrics")
                return {d: m for d, m in c.fetchall()}
        except Exception:
            pass
    with _sql_lock:
        c = _sq()
        if dev:
            r = c.execute("SELECT metrics FROM dev_metrics WHERE dev=?", (dev,)).fetchone()
            return json.loads(r[0]) if r else None
        return {d: json.loads(m) for d, m in c.execute("SELECT dev, metrics FROM dev_metrics").fetchall()}


def backend():
    return "postgres" if _pg() is not None else "sqlite"


if __name__ == "__main__":
    # smoke test on the file vault
    append("token_create", mint="MINTTEST", dev="DEVTEST", payload={"mc": 1000})
    append("market_snapshot", mint="MINTTEST", dev="DEVTEST", payload={"mc": 5000, "liq": 2000})
    append("migration", mint="MINTTEST", dev="DEVTEST", payload={})
    print("backend:", backend(), "| count:", count())
    print("MINTTEST timeline:", json.dumps(query(mint="MINTTEST"), indent=2)[:400])