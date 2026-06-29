#!/usr/bin/env python3
"""
ANSTRACK — vault database layer
On Render (or anywhere DATABASE_URL is set) the brain's vault persists to Postgres so
tracked devs, the wallet registry, ansem relays and injection logs survive redeploys.
Locally (no DATABASE_URL) everything falls back to the file vault — nothing to install.

What it stores
  wallets       : the wallet registry (one JSONB row per wallet) -> the dev brain
  kv            : small JSON blobs (ansem feed, misc state)
  injections    : auto-LP liquidity injections logged by the bot

First boot with an empty Postgres + an existing file vault auto-migrates the registry.

Usage from the rest of the app:
  import db
  if db.enabled():            # True when DATABASE_URL is set and reachable
      data = db.load_registry()
      db.save_registry(data)
      db.log_injection({...})
      db.recent_injections(50)
"""
import os, json, datetime

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pg = None            # cached connection
_ok = None            # cached "is postgres usable" flag


def _normalize_url(url: str) -> str:
    # Render sometimes hands out postgres:// — psycopg2 accepts it, but normalize anyway
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _connect():
    global _pg
    if _pg is not None:
        return _pg
    try:
        import psycopg2
    except ImportError:
        print("[db] DATABASE_URL set but psycopg2 not installed — run: pip install psycopg2-binary")
        return None
    try:
        sslmode = os.getenv("PGSSLMODE", "require")  # Render Postgres requires SSL
        _pg = psycopg2.connect(_normalize_url(DATABASE_URL), sslmode=sslmode)
        _pg.autocommit = True
        _init_schema(_pg)
        return _pg
    except Exception as e:
        print(f"[db] could not connect to Postgres ({e}); using file vault")
        return None


def enabled() -> bool:
    """True if Postgres is configured AND reachable; otherwise the app uses files."""
    global _ok
    if _ok is not None:
        return _ok
    _ok = bool(DATABASE_URL) and _connect() is not None
    return _ok


def _init_schema(conn):
    with conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                wallet     TEXT PRIMARY KEY,
                data       JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS kv (
                k          TEXT PRIMARY KEY,
                v          JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS injections (
                id         SERIAL PRIMARY KEY,
                signature  TEXT UNIQUE,
                mint       TEXT,
                amount_sol DOUBLE PRECISION,
                ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
                meta       JSONB
            );
        """)


# ---- wallet registry (the dev brain) -------------------------------------------------
def load_registry():
    """Return {wallet: entry}. None means 'not using Postgres' (caller should read the file)."""
    if not enabled():
        return None
    conn = _connect()
    with conn.cursor() as c:
        c.execute("SELECT wallet, data FROM wallets")
        return {w: d for (w, d) in c.fetchall()}


def save_registry(data: dict) -> bool:
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        for wallet, entry in (data or {}).items():
            c.execute(
                "INSERT INTO wallets (wallet, data, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (wallet) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (wallet, json.dumps(entry)),
            )
    return True


def save_wallet(wallet: str, entry: dict) -> bool:
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO wallets (wallet, data, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (wallet) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (wallet, json.dumps(entry)),
        )
    return True


def delete_wallet(wallet: str) -> bool:
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        c.execute("DELETE FROM wallets WHERE wallet = %s", (wallet,))
    return True


# ---- small key/value blobs (ansem feed, misc) ---------------------------------------
def kv_get(key: str):
    if not enabled():
        return None
    conn = _connect()
    with conn.cursor() as c:
        c.execute("SELECT v FROM kv WHERE k = %s", (key,))
        row = c.fetchone()
        return row[0] if row else None


def kv_set(key: str, value) -> bool:
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO kv (k, v, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()",
            (key, json.dumps(value)),
        )
    return True


# ---- injections (auto-LP proof) -----------------------------------------------------
def log_injection(entry: dict) -> bool:
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO injections (signature, mint, amount_sol, meta) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (signature) DO NOTHING",
            (entry.get("signature"), entry.get("mint"), entry.get("amountSol"), json.dumps(entry)),
        )
    return True


def recent_injections(limit: int = 50):
    if not enabled():
        return []
    conn = _connect()
    with conn.cursor() as c:
        c.execute("SELECT signature, mint, amount_sol, ts, meta FROM injections ORDER BY ts DESC LIMIT %s", (limit,))
        out = []
        for sig, mint, amt, ts, meta in c.fetchall():
            row = dict(meta or {})
            row.update({"signature": sig, "mint": mint, "amountSol": amt,
                        "ts": ts.isoformat() if ts else None})
            out.append(row)
        return out


# ---- one-time migration: file vault -> Postgres -------------------------------------
def migrate_from_file_if_needed(registry_path) -> bool:
    """If Postgres is empty but a registry.json exists, import it. Safe to call on every boot."""
    if not enabled():
        return False
    conn = _connect()
    with conn.cursor() as c:
        c.execute("SELECT count(*) FROM wallets")
        if c.fetchone()[0] > 0:
            return False  # already populated
    try:
        import pathlib
        p = pathlib.Path(registry_path)
        if not p.exists():
            return False
        data = json.loads(p.read_text())
        if data:
            save_registry(data)
            print(f"[db] migrated {len(data)} wallets from {p} into Postgres")
            return True
    except Exception as e:
        print(f"[db] migration skipped: {e}")
    return False


if __name__ == "__main__":
    # quick CLI: `python db.py migrate`  (force-imports the file registry into Postgres)
    import sys, config
    if not DATABASE_URL:
        print("DATABASE_URL is not set — nothing to do (running on the file vault).")
        sys.exit(0)
    if not enabled():
        print("DATABASE_URL set but Postgres unreachable.")
        sys.exit(1)
    reg = os.path.join(config.VAULT_PATH, "wallets", "registry.json")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "migrate":
        save_registry(json.loads(open(reg).read())) if os.path.exists(reg) else None
        print("Done.")
    else:
        data = load_registry() or {}
        print(f"Postgres OK — {len(data)} wallets stored.")