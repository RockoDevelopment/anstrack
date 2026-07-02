"""
ANSTRACK -- Local server
Serves ANSTRACK.html and exposes the self-building wallet registry over a tiny JSON API
so the terminal and the Python brain share ONE watchlist. Stdlib only, no dependencies.

Run:  python server.py        (then open http://localhost:8787)

Endpoints:
  GET  /                 -> ANSTRACK.html  (same-origin, so the fetch below just works)
  GET  /api/registry     -> the registry.json the brain writes wins into
  POST /api/registry     -> {"action":"add","wallet":"...","label":"..."} | {"action":"remove","wallet":"..."}

The brain credits devs on migration (ingestion/pumpfun.py). This server lets you SEE
those devs appear live in the terminal, and lets wallets you add in the UI persist into
the same file the brain reads -- one watchlist, both directions.
"""
# console output -> UTF-8 so a star/emoji in any log line can't crash a print on a Windows (cp1252) console
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
import json
import hashlib
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
from ingestion.wallets import WalletRegistry

HERE = Path(__file__).parent
# Render injects PORT; locally we fall back to ANSTRACK_PORT, then 8787.
PORT = int(__import__("os").getenv("PORT") or __import__("os").getenv("ANSTRACK_PORT") or "8787")
REGISTRY = WalletRegistry()  # same path the brain uses: VAULT_PATH/wallets/registry.json

X_HANDLE = "blknoiz06"
CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def _ansem_path() -> Path:
    p = Path(config.VAULT_PATH) / "x_inbox" / f"{X_HANDLE}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---- pump.fun dev profile (image, username, followers) for the Best Devs page ----
_PUMPDEV_CACHE = {}
def _pumpdev(wallet: str):
    if not wallet or len(wallet) < 32:
        return {"ok": False, "error": "bad wallet"}
    now = time.time()
    hit = _PUMPDEV_CACHE.get(wallet)
    if hit and now - hit[0] < 600:
        return hit[1]
    out = {"ok": False, "wallet": wallet}
    prof = {}
    for url in (f"https://frontend-api-v3.pump.fun/users/{wallet}",
                f"https://frontend-api.pump.fun/users/{wallet}"):
        try:
            req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                prof = json.loads(r.read().decode("utf-8", "ignore")) or {}
            if prof:
                break
        except Exception:
            continue
    created = None
    try:
        url = f"https://frontend-api-v3.pump.fun/coins/user-created-coins/{wallet}?offset=0&limit=1&includeNsfw=false"
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
            if isinstance(j, dict):
                created = j.get("total") or j.get("count")
    except Exception:
        pass
    if prof:
        out = {"ok": True, "wallet": wallet,
               "username": prof.get("username") or prof.get("name") or "",
               "image": prof.get("profile_image") or prof.get("profileImage") or prof.get("image") or "",
               "followers": prof.get("follower_count", prof.get("followers", prof.get("followerCount"))),
               "following": prof.get("following_count", prof.get("following")),
               "bio": (prof.get("bio") or "")[:160],
               "created": created}
    _PUMPDEV_CACHE[wallet] = (now, out)
    if len(_PUMPDEV_CACHE) > 4000:
        _PUMPDEV_CACHE.clear()
    return out


# ---- pump.fun fee-sharing detection (the "creator fees -> auto liquidity" routing) ----
# A token has this when a fee-sharing config account exists at the PDA
# findProgramAddress(["sharing-config", mint], PUMP_FEE_PROGRAM_ID).
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
_ED_P = 2 ** 255 - 19
_FEESHARE_CACHE = {}
_RPCS = ["https://api.mainnet-beta.solana.com",
         "https://solana-rpc.publicnode.com",
         "https://rpc.ankr.com/solana"]


def _b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    pad = len(s) - len(s.lstrip("1"))
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + raw


def _b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + s


def _on_curve(b):
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    d = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
    y2 = (y * y) % _ED_P
    u = (y2 - 1) % _ED_P
    v = (d * y2 + 1) % _ED_P
    xx = (u * pow(v, _ED_P - 2, _ED_P)) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * pow(2, (_ED_P - 1) // 4, _ED_P)) % _ED_P
    return (x * x - xx) % _ED_P == 0


def _find_pda(seeds, program):
    pb = _b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for s in seeds:
            h.update(s)
        h.update(bytes([bump]))
        h.update(pb)
        h.update(b"ProgramDerivedAddress")
        dg = h.digest()
        if not _on_curve(dg):
            return _b58encode(dg)
    return None


def _rpc_account_exists(pubkey):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                       "params": [pubkey, {"encoding": "base64"}]}).encode()
    for url in _RPCS:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            return (d.get("result") or {}).get("value") is not None
        except Exception:
            continue
    return None


def _feeshare(mint: str):
    if not mint or len(mint) < 32:
        return {"ok": False, "error": "bad mint"}
    now = time.time()
    hit = _FEESHARE_CACHE.get(mint)
    if hit and now - hit[0] < 300:
        return hit[1]
    out = {"ok": False, "mint": mint}
    try:
        pda = _find_pda([b"sharing-config", _b58decode(mint)], _PUMP_FEE_PROGRAM_ID)
        exists = _rpc_account_exists(pda) if pda else None
        if exists is None:
            out = {"ok": False, "mint": mint, "error": "rpc unavailable"}
        else:
            out = {"ok": True, "mint": mint, "feeShare": bool(exists), "configPda": pda}
    except Exception as e:
        out = {"ok": False, "mint": mint, "error": str(e)[:120]}
    _FEESHARE_CACHE[mint] = (now, out)
    if len(_FEESHARE_CACHE) > 4000:
        _FEESHARE_CACHE.clear()
    return out


# ---- LLM-written rug report (warn others; copy-paste into a pump.fun report) ----
def _rug_report(payload: dict) -> dict:
    wallet = (payload.get("wallet") or "").strip()
    if len(wallet) < 32:
        return {"ok": False, "error": "bad wallet"}
    reg = WalletRegistry()
    entry = reg._data.get(wallet, {})
    history = entry.get("rug_history", []) or payload.get("history", [])
    label = entry.get("label") or payload.get("label") or wallet[:6]
    rugs = entry.get("rugs", len(history))
    # evidence lines
    ev_lines = []
    for h in history[-8:]:
        mint = h.get("mint", "")
        ev_lines.append(f"- Token {mint} | {h.get('note','rugged')} | {h.get('at','')}\n"
                        f"  chart: https://dexscreener.com/solana/{mint}  |  token: https://solscan.io/token/{mint}")
    evidence = "\n".join(ev_lines) or "- (recent rug detected this session)"
    facts = (f"Dev wallet: {wallet}\nWallet explorer: https://solscan.io/account/{wallet}\n"
             f"pump.fun profile: https://pump.fun/profile/{wallet}\n"
             f"Confirmed rug events: {rugs}\n\nEvidence:\n{evidence}")
    report = None
    try:
        from intelligence.providers import generate_text, llm_available
        if llm_available():
            sys_p = ("You are a fraud-report writer for a Solana memecoin community. Write a concise, "
                     "factual, non-defamatory abuse report suitable for submitting to the pump.fun team. "
                     "State only what the evidence supports (token collapsed >85% within minutes of "
                     "migration while the deployer wallet sold the majority of its holdings). Include the "
                     "wallet, the token links, and a clear request to review/restrict the wallet. No insults, "
                     "no speculation beyond the data. Plain text, ready to paste.")
            report = generate_text(f"Write the report from these facts:\n\n{facts}", system=sys_p, max_tokens=900)
    except Exception as e:
        print(f"  [anstrack] report LLM error: {str(e)[:90]}")
    if not report:
        report = (f"SUBJECT: Abuse report \u2014 serial rug deployer ({label})\n\n"
                  f"Hello pump.fun team,\n\nI'm reporting a deployer wallet that repeatedly launches tokens, "
                  f"lets them migrate, then dumps the majority of its holdings while the price collapses "
                  f">85% within minutes \u2014 harming buyers.\n\n{facts}\n\n"
                  f"Request: please review this wallet for rug/scam behavior and restrict it from the platform. "
                  f"Evidence links above are on-chain and publicly verifiable. Thank you.")
    return {"ok": True, "wallet": wallet, "report": report,
            "links": {"wallet": f"https://solscan.io/account/{wallet}",
                      "profile": f"https://pump.fun/profile/{wallet}"}}


# ---- LLM investigation of a token: what's missing + where the supply sits ----
def _investigate(payload: dict) -> dict:
    mint = (payload.get("mint") or "").strip()
    if len(mint) < 32:
        return {"ok": False, "error": "bad mint"}
    sym = payload.get("sym") or ""
    name = payload.get("name") or ""
    missing = payload.get("missing") or []
    holders = payload.get("holders") or []
    market = payload.get("market") or {}
    fee = bool(payload.get("feeShare"))
    top = holders[:8]
    hlines = []
    for h in top:
        a = (h.get("address") or "")
        pct = h.get("pct", 0) or 0
        hlines.append(f"- {a[:6]}…{a[-4:]} holds {pct:.1f}%  (https://solscan.io/account/{a})")
    hsummary = "\n".join(hlines) or "- (holder data unavailable)"
    miss = "\n".join(f"- {m}" for m in missing) or "- (all core checks are passing)"
    facts = (f"Token: {name} (${sym})\nMint: {mint}\n"
             f"DexScreener: https://dexscreener.com/solana/{mint}\n"
             f"5m volume ${market.get('vol',0):,.0f} | 5m change {market.get('ch',0):.1f}% | liquidity ${market.get('liq',0):,.0f}\n"
             f"ANSOM PROMISE (creator fees -> liquidity): {'yes' if fee else 'no / unknown'}\n\n"
             f"Missing / unverified checks:\n{miss}\n\nTop holders:\n{hsummary}")
    report = None
    try:
        from intelligence.providers import generate_text, llm_available
        if llm_available():
            sys_p = ("You are an on-chain research analyst for Solana memecoins. Given a token's failing checks and its "
                     "top-holder distribution, write a SHORT research note — a research signal, NOT financial advice. "
                     "First, explain plainly what each missing item means (e.g. the dev has not revoked mint authority yet, "
                     "the project did not opt into ANSOM PROMISE fee-sharing, volume is only the dev's own buy). Then analyze "
                     "concentration: if one non-pool wallet holds a large share, name it by short address and reason about WHY "
                     "a project might route a large portion to a single wallet — sometimes supply is intentionally sent to a "
                     "KOL/influencer, to a donation/charity/justice cause, or to a support/help wallet, which can be legitimate; "
                     "other times it signals dump risk. Stay balanced and factual, note that the largest holder on a fresh "
                     "pump.fun token is usually the bonding-curve/pool itself, and end with exactly what to verify on Solscan. "
                     "No hype, no price calls, no financial advice.")
            report = generate_text(f"Investigate this token:\n\n{facts}", system=sys_p, max_tokens=750)
    except Exception as e:
        print(f"  [anstrack] investigate LLM error: {str(e)[:90]}")
    if not report:
        big = top[0] if top else None
        if big and (big.get("pct", 0) or 0) >= 10:
            a = big.get("address", "")
            conc = (f"The largest holder ({a[:6]}…) controls {big.get('pct',0):.1f}% of supply. On a fresh pump.fun token "
                    "the top wallet is usually the bonding-curve/pool itself, so confirm that on Solscan first. If it is a "
                    "regular wallet, a share this large is worth understanding — large allocations are sometimes intentional "
                    "(KOL/influencer supply, a donation or support/justice wallet) and sometimes a dump risk.")
        else:
            conc = "No single wallet holds an unusually large share of supply (top holder under ~10%)."
        report = (f"INVESTIGATION — {name} (${sym})\n\n"
                  f"What's missing / unverified:\n{miss}\n\n"
                  f"Holder concentration:\n{conc}\n\nTop holders:\n{hsummary}\n\n"
                  f"Research signal only — not financial advice. Verify the contract and the wallets above on Solscan before acting.\n"
                  f"(Written analysis is offline — configure a provider in intelligence/providers for the full LLM narrative.)")
    return {"ok": True, "mint": mint, "report": report}


# ---- RugCheck proxy (server-side fetch avoids browser CORS; gives real LP-lock %) ----
_RUG_CACHE = {}  # mint -> (ts, compact)
def _rugcheck(mint: str):
    if not mint or len(mint) < 32:
        return {"ok": False, "error": "bad mint"}
    now = time.time()
    hit = _RUG_CACHE.get(mint)
    if hit and now - hit[0] < 120:
        return hit[1]
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
    out = {"ok": False, "mint": mint}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        tok = data.get("token") or {}
        markets = data.get("markets") or []
        lp_locked = 0.0
        for m in markets:
            lp = (m.get("lp") or {})
            for k in ("lpLockedPct", "lpLockedPercentage"):
                if lp.get(k) is not None:
                    lp_locked = max(lp_locked, float(lp.get(k) or 0))
        top = None
        th = data.get("topHolders") or []
        if th and isinstance(th, list):
            p = th[0].get("pct")
            if p is not None:
                top = round(float(p), 1)
        out = {
            "ok": True, "mint": mint,
            "lpLockedPct": round(lp_locked, 1),
            "mintAuthRevoked": tok.get("mintAuthority") in (None, "", "11111111111111111111111111111111"),
            "freezeAuthRevoked": tok.get("freezeAuthority") in (None, "", "11111111111111111111111111111111"),
            "topPct": top,
            "score": data.get("score_normalised", data.get("score")),
            "risks": [r.get("name") for r in (data.get("risks") or []) if r.get("name")][:6],
        }
    except Exception as e:
        out = {"ok": False, "mint": mint, "error": str(e)[:120]}
    _RUG_CACHE[mint] = (now, out)
    if len(_RUG_CACHE) > 4000:
        _RUG_CACHE.clear()
    return out


def _read_ansem(n: int = 40):
    """Non-consuming read of the dropfile; newest first, with CAs extracted."""
    path = _ansem_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = {"text": line}
        text = row.get("text", "")
        row["cas"] = [m for m in CA_RE.findall(text) if not m.startswith("http")]
        rows.append(row)
    rows.reverse()
    return rows


# ---- automatic @blknoiz06 fetch (best-effort; X actively fights scraping) ----
NITTER_HOSTS = ["nitter.net", "nitter.poast.org", "nitter.privacydev.net", "nitter.1d4.us", "nitter.lucabased.xyz"]
_ANSEM_STATUS = {"source": "starting", "count": 0, "at": None}
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://platform.twitter.com/",
}


def _walk_tweets(node, out, avatar, handle=X_HANDLE):
    """Recursively pull tweet-like objects out of arbitrary syndication JSON, so we
    survive X reshaping the payload. Collects {text, created_at} and a profile image."""
    if isinstance(node, dict):
        txt = node.get("full_text") or node.get("text")
        ts = node.get("created_at") or node.get("createdAt")
        if isinstance(txt, str) and txt.strip() and (ts or "entities" in node):
            out.append({"text": txt.strip(),
                        "ts": ts or datetime.now().isoformat(),
                        "handle": handle,
                        "url": f"https://x.com/{handle}"})
        img = node.get("profile_image_url_https") or node.get("profile_image_url")
        if isinstance(img, str) and img and not avatar[0]:
            avatar[0] = img.replace("_normal", "_400x400")
        for v in node.values():
            _walk_tweets(v, out, avatar, handle)
    elif isinstance(node, list):
        for v in node:
            _walk_tweets(v, out, avatar, handle)


# ---- generic per-handle fetch for configurable INTEL FEED accounts (cached) ----
_FEED_CACHE = {}  # handle -> (ts, rows)
def _fetch_handle_tweets(handle, n=18):
    handle = re.sub(r"[^A-Za-z0-9_]", "", handle or "")[:30]
    if not handle:
        return []
    now = time.time()
    c = _FEED_CACHE.get(handle)
    if c and now - c[0] < 90:  # serve cached for 90s; X fights frequent scraping
        return c[1]
    rows = []
    try:
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}?showReplies=false"
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            data = json.loads(m.group(1))
            out, avatar = [], [None]
            _walk_tweets(data, out, avatar, handle)
            seen = set()
            for t in out:
                if t["text"] in seen:
                    continue
                seen.add(t["text"])
                if avatar[0]:
                    t["avatar"] = avatar[0]
                t["cas"] = [x for x in CA_RE.findall(t["text"]) if not x.startswith("http")]
                rows.append(t)
            rows = rows[:n]
    except Exception:
        rows = []
    _FEED_CACHE[handle] = (now, rows)
    return rows


def _fetch_syndication():
    """Pull recent tweets from X's public syndication timeline (no auth)."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{X_HANDLE}?showReplies=false"
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", "ignore")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    out, avatar = [], [None]
    _walk_tweets(data, out, avatar)
    # dedupe by text, keep order
    seen, uniq = set(), []
    for t in out:
        if t["text"] not in seen:
            seen.add(t["text"])
            if avatar[0]:
                t["avatar"] = avatar[0]
            uniq.append(t)
    return uniq


def _fetch_nitter():
    """Fallback: Nitter RSS across a few instances."""
    for host in NITTER_HOSTS:
        try:
            req = urllib.request.Request(f"https://{host}/{X_HANDLE}/rss", headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                xml = r.read().decode("utf-8", "ignore")
            items = re.findall(r"<item>(.*?)</item>", xml, re.S)
            out = []
            for it in items[:25]:
                tm = re.search(r"<title>(.*?)</title>", it, re.S)
                dm = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
                if tm:
                    txt = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
                    out.append({"text": txt, "ts": dm.group(1).strip() if dm else datetime.now().isoformat(),
                                "url": f"https://x.com/{X_HANDLE}"})
            if out:
                return out
        except Exception:
            continue
    return []


def _ansem_poller(interval: int = 60):
    """Background thread: auto-fetch tweets and append new ones to the dropfile."""
    path = _ansem_path()
    while True:
        rows, source = [], "none"
        try:
            rows = _fetch_syndication()
            if rows:
                source = "syndication"
        except Exception as e:
            print(f"  [anstrack] ansem syndication error: {str(e)[:90]}")
        if not rows:
            try:
                rows = _fetch_nitter()
                if rows:
                    source = "nitter"
            except Exception:
                rows = []
        _ANSEM_STATUS.update({"source": source, "count": len(rows), "at": datetime.now().isoformat()})
        if rows:
            try:
                existing = set()
                if path.exists():
                    for ln in path.read_text(encoding="utf-8").splitlines():
                        try:
                            existing.add(json.loads(ln).get("text", "").strip())
                        except Exception:
                            existing.add(ln.strip())
                new = [r for r in reversed(rows) if r.get("text", "").strip() not in existing]
                if new:
                    with path.open("a", encoding="utf-8") as f:
                        for r in new:
                            f.write(json.dumps(r) + "\n")
                    print(f"  [anstrack] ansem: +{len(new)} new tweets via {source}")
            except Exception as e:
                print(f"  [anstrack] ansem write error: {e}")
        else:
            print(f"  [anstrack] ansem: no tweets this cycle (X may be blocking syndication + nitter)")
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quiet
        pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        # Landing page is the front door; the tracker lives at /ANSTRACK.html
        if self.path in ("/", "/index.html"):
            f = HERE / "index.html"
            if f.exists():
                return self._send(200, f.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return self._send(200, (HERE / "ANSTRACK.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if self.path in ("/ANSTRACK.html", "/app", "/tracker"):
            return self._send(200, (HERE / "ANSTRACK.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if self.path.startswith("/anstrack-logo.png"):
            p = HERE / "anstrack-logo.png"
            if p.exists():
                return self._send(200, p.read_bytes(), "image/png")
            return self._send(404, b"")
        if self.path.startswith("/api/injections"):
            # prefer Postgres (Render), fall back to the bot's local log file
            try:
                import db
                if db.enabled():
                    return self._send(200, json.dumps(db.recent_injections(50)))
            except Exception:
                pass
            p = HERE / "injections.json"
            try:
                return self._send(200, p.read_text(encoding="utf-8") if p.exists() else "[]")
            except Exception:
                return self._send(200, "[]")
        if self.path.startswith("/api/registry"):
            # Reload fresh each time so brain-written wins show immediately.
            reg = WalletRegistry()
            return self._send(200, json.dumps(reg._data))
        if self.path.startswith("/api/feed"):
            handles = []
            if "?" in self.path:
                from urllib.parse import parse_qs
                raw = parse_qs(self.path.split("?", 1)[1]).get("handles", [""])[0]
                handles = [h for h in re.split(r"[,\s]+", raw) if h][:12]
            merged = []
            for h in handles:
                try:
                    merged.extend(_fetch_handle_tweets(h))
                except Exception:
                    pass
            return self._send(200, json.dumps(merged))
        if self.path.startswith("/api/ansem/status"):
            return self._send(200, json.dumps(_ANSEM_STATUS))
        if self.path.startswith("/api/ansem"):
            return self._send(200, json.dumps(_read_ansem()))
        if self.path.startswith("/api/rug"):
            mint = ""
            if "?" in self.path:
                from urllib.parse import parse_qs
                mint = parse_qs(self.path.split("?", 1)[1]).get("mint", [""])[0]
            return self._send(200, json.dumps(_rugcheck(mint)))
        if self.path.startswith("/api/feeshare"):
            mint = ""
            if "?" in self.path:
                from urllib.parse import parse_qs
                mint = parse_qs(self.path.split("?", 1)[1]).get("mint", [""])[0]
            return self._send(200, json.dumps(_feeshare(mint)))
        if self.path.startswith("/api/pumpdev"):
            wallet = ""
            if "?" in self.path:
                from urllib.parse import parse_qs
                wallet = parse_qs(self.path.split("?", 1)[1]).get("wallet", [""])[0]
            return self._send(200, json.dumps(_pumpdev(wallet)))
        # ---- event-sourced intelligence layer ----
        if self.path.startswith("/api/devmetrics"):
            from urllib.parse import parse_qs
            dev = parse_qs(self.path.split("?", 1)[1]).get("dev", [""])[0] if "?" in self.path else ""
            try:
                import dev_intelligence, event_log
                if dev:
                    return self._send(200, json.dumps(dev_intelligence.profile(dev) or {}))
                return self._send(200, json.dumps(event_log.get_dev_metrics() or {}))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/events"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import event_log
                typ = q.get("type", [None])[0]
                return self._send(200, json.dumps(event_log.query(
                    mint=q.get("mint", [None])[0], dev=q.get("dev", [None])[0],
                    types=[typ] if typ else None,
                    limit=int(q.get("limit", ["500"])[0]), order="desc")))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/replay"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import replay
                if q.get("mint"):
                    return self._send(200, json.dumps(replay.replay_token(q["mint"][0])))
                if q.get("dev") and q.get("ts"):
                    return self._send(200, json.dumps(replay.reconstruct_dev_at(q["dev"][0], float(q["ts"][0]))))
                if q.get("start") and q.get("end"):
                    rescore = q.get("rescore", ["0"])[0] in ("1", "true", "yes")
                    return self._send(200, json.dumps(replay.replay_range(float(q["start"][0]), float(q["end"][0]), rescore=rescore)))
                return self._send(200, json.dumps(replay.system_state()))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/brain"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import brain
                brain.ensure_fresh(20.0)  # read replica: rebuild from the log if stale
                if q.get("dev"):
                    return self._send(200, json.dumps(brain.dev_brain(q["dev"][0]) or {}))
                if q.get("mint"):
                    return self._send(200, json.dumps(brain.token_brain(q["mint"][0]) or {}))
                return self._send(200, json.dumps(brain.snapshot()))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/predict"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import brain, prediction
                brain.ensure_fresh(20.0)
                if q.get("mint"):
                    return self._send(200, json.dumps(prediction.predict_token(q["mint"][0])))
                return self._send(200, json.dumps(prediction.predict_from_features(
                    float(q.get("grad", ["0"])[0]), float(q.get("peak", ["0"])[0]),
                    float(q.get("ttm", ["0"])[0]),
                    dev_p_mig=float(q["devp"][0]) if q.get("devp") else None,
                    dev_sample=int(q.get("devn", ["0"])[0]))))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/why"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import brain, why_engine
                brain.ensure_fresh(20.0)
                if not q.get("mint"):
                    return self._send(200, json.dumps({"error": "mint required"}))
                return self._send(200, json.dumps(why_engine.explain(q["mint"][0])))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/features"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                import feature_store
                ent = q.get("entity", [None])[0]
                if ent:
                    return self._send(200, json.dumps(feature_store.get(ent) or {}))
                return self._send(200, json.dumps({"count": feature_store.count()}))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.startswith("/api/trade"):
            # Proxy to PumpPortal trade-local: builds an UNSIGNED transaction for the user's own
            # wallet to sign in Phantom. The client tries this same-origin route first and only
            # falls back to hitting pumpportal.fun directly (which can be CORS-blocked) if absent.
            # Nothing is signed or sent here — raw serialized tx bytes are returned as-is.
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n) if n else b"{}"
                req = urllib.request.Request(
                    "https://pumpportal.fun/api/trade-local", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    return self._send(200, r.read(), "application/octet-stream")
            except urllib.error.HTTPError as e:
                try: detail = e.read().decode("utf-8", "replace")[:500]
                except Exception: detail = ""
                return self._send(e.code, json.dumps({"error": f"pumpportal {e.code}", "detail": detail}))
            except Exception as e:
                return self._send(502, json.dumps({"error": f"trade proxy failed: {e}"}))
        if self.path.startswith("/api/event"):
            # append a market-snapshot (or any) event — lets the client feed the time-series in
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                import event_log
                event_log.append(body.get("type", "market_snapshot"),
                                 mint=body.get("mint"), dev=body.get("dev"),
                                 payload=body.get("payload") or {})
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/report"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._send(400, json.dumps({"error": f"bad json: {e}"}))
            return self._send(200, json.dumps(_rug_report(payload)))
        if self.path.startswith("/api/investigate"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._send(400, json.dumps({"error": f"bad json: {e}"}))
            return self._send(200, json.dumps(_investigate(payload)))
        if self.path.startswith("/api/ansem"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._send(400, json.dumps({"error": f"bad json: {e}"}))
            text = (payload.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "need text"}))
            row = {"text": text, "ts": datetime.now().isoformat(),
                   "url": payload.get("url", f"https://x.com/{X_HANDLE}")}
            with _ansem_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            return self._send(200, json.dumps({"ok": True, "tweets": _read_ansem()}))

        if not self.path.startswith("/api/registry"):
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, json.dumps({"error": f"bad json: {e}"}))

        action = payload.get("action")
        wallet = (payload.get("wallet") or "").strip()
        reg = WalletRegistry()
        if action == "add" and len(wallet) >= 32:
            reg.add(wallet, kind=payload.get("kind", "whale"),
                    label=payload.get("label", ""), notes=payload.get("notes", ""))
        elif action == "rug" and len(wallet) >= 32:
            reg.record_rug(wallet, mint=payload.get("mint", ""), note=payload.get("note", ""))
        elif action == "remove" and wallet:
            reg._data.pop(wallet, None)
            reg._save()
        else:
            return self._send(400, json.dumps({"error": "need action add|rug|remove and a valid wallet"}))
        return self._send(200, json.dumps({"ok": True, "registry": reg._data}))


if __name__ == "__main__":
    print(f"  [anstrack] registry: {REGISTRY.path}")
    print(f"  [anstrack] serving  : http://localhost:{PORT}")
    print(f"  [anstrack] ansem    : auto-fetching @{X_HANDLE} (best-effort)")
    threading.Thread(target=_ansem_poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()