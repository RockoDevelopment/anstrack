"""
ANSTRACK -- Memory / Vault layer (self-contained, no LAIS files needed)
Hot signal store in SQLite for speed; opportunities + briefings ALSO written as markdown
into the Obsidian vault so the brain compounds the way the second-brain philosophy wants.

Public API (everything processor.py / alpha_analyst.py / synthesizer call):
  save_signal, get_unprocessed_signals, mark_signals_processed,
  save_opportunity, get_opportunities, get_launch_candidates,
  read_vault_context, get_vault_stats, save_briefing
"""
import hashlib
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
import config

VAULT = Path(config.VAULT_PATH)
DB_PATH = VAULT / "anstrack.db"
_lock = threading.Lock()


def _vault_dirs():
    for d in ("signals", "opportunities", "briefings", "wallets", "x_inbox"):
        (VAULT / d).mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    VAULT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    _vault_dirs()
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup TEXT UNIQUE,
            source TEXT, title TEXT, url TEXT, content TEXT,
            score_raw INTEGER DEFAULT 0, meta TEXT DEFAULT '{}',
            processed INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS opportunities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT, name TEXT, title TEXT,
            score INTEGER DEFAULT 0, confidence TEXT, rug_risk TEXT,
            data TEXT DEFAULT '{}', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS briefings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, content TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sig_proc ON signals(processed);
        CREATE INDEX IF NOT EXISTS ix_opp_score ON opportunities(score);
        """)


_init()


# ---------------- signals ----------------

def save_signal(source: str, title: str, url: str = "", content: str = "",
                score_raw: int = 0, meta: Dict = None) -> Optional[int]:
    """Insert a signal, deduped by mint (if present) else source+title+url.
    Returns row id, or None if it was a duplicate."""
    meta = meta or {}
    key_src = meta.get("mint") or f"{source}|{title}|{url}"
    dedup = hashlib.sha1(key_src.encode("utf-8")).hexdigest()
    now = datetime.now().isoformat()
    with _lock, _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO signals(dedup,source,title,url,content,score_raw,meta,processed,created_at)"
                " VALUES(?,?,?,?,?,?,?,0,?)",
                (dedup, source, title, url, content, int(score_raw or 0), json.dumps(meta), now),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already have it


def get_unprocessed_signals(limit: int = 15) -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id,source,title,url,content,score_raw,meta FROM signals"
            " WHERE processed=0 ORDER BY score_raw DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception: d["meta"] = {}
        out.append(d)
    return out


def mark_signals_processed(ids: List[int]) -> None:
    if not ids:
        return
    with _lock, _conn() as c:
        c.executemany("UPDATE signals SET processed=1 WHERE id=?", [(i,) for i in ids])


# ---------------- opportunities (calls) ----------------

def save_opportunity(opp: Dict) -> int:
    now = datetime.now().isoformat()
    mint  = opp.get("mint", "")
    name  = opp.get("name") or opp.get("title", "")
    title = opp.get("title") or name
    score = int(opp.get("score") or opp.get("alpha_score") or 0)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO opportunities(mint,name,title,score,confidence,rug_risk,data,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (mint, name, title, score, opp.get("confidence", ""), opp.get("rug_risk", ""),
             json.dumps(opp), now),
        )
        oid = cur.lastrowid
    _write_opportunity_md(oid, opp, score)
    return oid


def _write_opportunity_md(oid: int, opp: Dict, score: int):
    safe = "".join(ch for ch in (opp.get("name") or opp.get("mint") or str(oid)) if ch.isalnum() or ch in "-_")[:40]
    path = VAULT / "opportunities" / f"{datetime.now():%Y%m%d}-{score:03d}-{safe}.md"
    flags = opp.get("red_flags", []) or []
    verify = opp.get("must_verify", []) or []
    md = f"""# {opp.get('name') or opp.get('mint','?')}  ({score}/100)

- **mint:** {opp.get('mint','?')}
- **rug risk:** {opp.get('rug_risk','?')}  |  **confidence:** {opp.get('confidence','?')}
- **tracked entity:** {opp.get('watched_entity','none')}
- **why now:** {opp.get('why_now','')}

## Thesis
{opp.get('thesis','')}

## Red flags
{chr(10).join('- '+f for f in flags) if flags else '- (none listed)'}

## Verify on-chain before risking SOL
{chr(10).join('- '+v for v in verify) if verify else '- authorities, holder spread, LP, dev sells'}

> ANSTRACK call {datetime.now():%Y-%m-%d %H:%M} — score routes attention, not money.
"""
    try: path.write_text(md, encoding="utf-8")
    except Exception: pass


def get_opportunities(min_score: int = 0, limit: int = 100) -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM opportunities WHERE score>=? ORDER BY score DESC, id DESC LIMIT ?",
            (min_score, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d.update(json.loads(d.get("data") or "{}"))
        except Exception: pass
        d.setdefault("problem", d.get("thesis", ""))
        d.setdefault("niche", d.get("watched_entity", ""))
        d.setdefault("monetization", "")
        out.append(d)
    return out


def get_launch_candidates() -> List[Dict]:
    return get_opportunities(min_score=config.ALPHA_ALERT_THRESHOLD, limit=50)


# ---------------- context / stats / briefings ----------------

def read_vault_context(hours: int = 168) -> str:
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        sigs = c.execute(
            "SELECT source,title,content FROM signals WHERE created_at>=? ORDER BY id DESC LIMIT 200",
            (since,)).fetchall()
    chunks = [f"[{s['source']}] {s['title']}\n{(s['content'] or '')[:300]}" for s in sigs]
    return "\n\n".join(chunks)


def get_vault_stats() -> Dict:
    with _conn() as c:
        sig_total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        sig_proc  = c.execute("SELECT COUNT(*) FROM signals WHERE processed=1").fetchone()[0]
        opp_total = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        launch    = c.execute("SELECT COUNT(*) FROM opportunities WHERE score>=?",
                              (config.ALPHA_ALERT_THRESHOLD,)).fetchone()[0]
    return {
        "signals":       {"total": sig_total, "processed": sig_proc},
        "opportunities": {"total": opp_total, "launch_ready": launch},
    }


def save_briefing(kind: str, content: str) -> int:
    now = datetime.now().isoformat()
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO briefings(kind,content,created_at) VALUES(?,?,?)",
                        (kind, content, now))
        bid = cur.lastrowid
    try:
        (VAULT / "briefings" / f"{datetime.now():%Y%m%d-%H%M}-{kind}.md").write_text(content, encoding="utf-8")
    except Exception:
        pass
    return bid
