"""
Pump.fun Brain -- PumpFun Ingester (PumpPortal websocket)
Streams real-time Pump.fun data and emits Signals into the buffer that the processor
drains on its normal cycle.

PumpPortal data API (verified against pumpportal.fun/data-api/real-time):
  endpoint : wss://pumpportal.fun/api/data            (free, no key)
             wss://pumpportal.fun/api/data?api-key=... (needed for metered streams)
  methods  : subscribeNewToken    -> new launches            (FREE)
             subscribeMigration   -> bonding-curve graduations (FREE)
             subscribeTokenTrade  -> trades on given mints     (METERED 0.01 SOL / 10k msgs)
             subscribeAccountTrade-> trades by given wallets    (METERED 0.01 SOL / 10k msgs)

CRITICAL operational rule from PumpPortal: use ONE websocket connection only. Send all
subscribe messages on the same socket. Opening a connection per token/wallet gets you
blacklisted. This class holds a single connection and multiplexes every subscription.

New-token event fields (observed): mint, name, symbol, traderPublicKey (the dev),
marketCapSol, vSolInBondingCurve, initialBuy, bondingCurveKey, signature, txType.

Requires: pip install websocket-client
"""
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from ingestion.base import Signal, StreamingIngester
from ingestion.wallets import WalletRegistry, flag

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

WS_URL_FREE = "wss://pumpportal.fun/api/data"
WS_URL_KEYED = "wss://pumpportal.fun/api/data?api-key={key}"
PUMP_FUN_URL = "https://pump.fun/{mint}"


def _heuristic_score(evt: Dict, dev_flag: Dict = None) -> int:
    """Cheap pre-score so the UI and processor can rank before the LLM ever runs.
    NOT a safety judgment -- purely 'is this worth a closer look'. 0-100.
    Devs who have LAUNCHED AND MIGRATED before (wins>=1) get a boost scaled by wins."""
    score = 0
    mc   = float(evt.get("marketCapSol", 0) or 0)
    vsol = float(evt.get("vSolInBondingCurve", 0) or 0)
    init = float(evt.get("initialBuy", 0) or 0)

    if vsol >= 30:   score += 15
    if vsol >= 60:   score += 10
    if init > 0:     score += 10
    if 25 <= mc <= 120: score += 15

    # Tracked wallet bump. A graduated dev (>=1 prior migration) is the strongest free
    # repeat signal we have, so scale it: +30 for one graduation, up to +45.
    if dev_flag:
        wins = int(dev_flag.get("wins", 0) or 0)
        if wins >= 1:
            score += min(20 + wins * 10, 45)
        elif dev_flag.get("kind"):
            score += 15
    return min(score, 100)


class PumpFunIngester(StreamingIngester):
    name = "pumpfun"
    buffer_size = 3000

    def __init__(self, registry: WalletRegistry = None):
        super().__init__()
        self.registry = registry or WalletRegistry()
        self.api_key  = getattr(config, "PUMPPORTAL_API_KEY", "")
        # Only follow wallet trades if we have a key AND there are funded watch targets.
        self.follow_wallets = bool(self.api_key) and getattr(config, "ENABLE_WALLET_TRADES", False)
        # Remember which dev launched each mint so a later migration can credit them.
        # Bounded so a long-running listener never grows unbounded (oldest mints drop).
        self._launch_devs: "OrderedDict[str, str]" = OrderedDict()
        self._launch_devs_max = 20000

    # ---- event handlers -------------------------------------------------

    def _on_new_token(self, evt: Dict) -> None:
        dev = evt.get("traderPublicKey", "")
        mint = evt.get("mint", "")
        # Remember who launched this mint, for win-crediting on a future migration.
        if mint and dev:
            self._launch_devs[mint] = dev
            self._launch_devs.move_to_end(mint)
            while len(self._launch_devs) > self._launch_devs_max:
                self._launch_devs.popitem(last=False)
        dev_flag = flag(dev, self.registry) if dev else {}
        # Learn the dev's launch cadence (only for devs we already know) so reputation
        # can weigh graduations against total launches over time.
        if dev and dev_flag:
            try:
                self.registry.record_launch(dev)
            except Exception:
                pass
        name = evt.get("name", "") or "(unnamed)"
        sym  = evt.get("symbol", "")

        # EVENT LOG: immutable record of the launch (dev<->mint association is implicit here).
        try:
            import event_log
            event_log.append("token_create", mint=mint, dev=(dev or None), payload={
                "name": name, "symbol": sym,
                "market_cap_sol": evt.get("marketCapSol"),
                "vsol": evt.get("vSolInBondingCurve"),
                "initial_buy": evt.get("initialBuy"),
                "uri": evt.get("uri", ""),
                "signature": evt.get("signature"),
            })
        except Exception:
            pass

        title = f"[LAUNCH] {name} (${sym})"
        if dev_flag.get("is_repeat"):
            title += f"  REPEAT DEV: {dev_flag['label']} ({dev_flag['wins']} prior)"

        content = (
            f"New Pump.fun launch.\n"
            f"Name: {name} (${sym})\n"
            f"Mint: {mint}\n"
            f"Dev wallet: {dev}\n"
            f"Market cap (SOL): {evt.get('marketCapSol')}\n"
            f"Virtual SOL in curve: {evt.get('vSolInBondingCurve')}\n"
            f"Dev initial buy: {evt.get('initialBuy')}\n"
        )
        if dev_flag:
            content += f"Dev flag: {dev_flag['kind']} '{dev_flag['label']}', wins={dev_flag['wins']}. {dev_flag.get('notes','')}\n"

        self._emit(Signal(
            source    = "pumpfun/launch",
            title     = title,
            url       = PUMP_FUN_URL.format(mint=mint),
            content   = content,
            score_raw = int(float(evt.get("marketCapSol", 0) or 0)),
            meta      = {
                "event":        "launch",
                "mint":         mint,
                "name":         name,
                "symbol":       sym,
                "dev":          dev,
                "dev_flag":     dev_flag,
                "market_cap_sol": evt.get("marketCapSol"),
                "vsol":         evt.get("vSolInBondingCurve"),
                "initial_buy":  evt.get("initialBuy"),
                "bonding_curve_key": evt.get("bondingCurveKey"),
                "signature":    evt.get("signature"),
                "uri":          evt.get("uri", ""),
                "heuristic":    _heuristic_score(evt, dev_flag),
                "ts":           datetime.now().isoformat(),
            },
        ))

    def _on_migration(self, evt: Dict) -> None:
        mint = evt.get("mint", "")

        # Self-building watchlist: a migration means this token survived the bonding
        # curve -- the strongest free on-chain signal that the dev can launch a runner.
        # Credit the dev who launched it. If they're new, this auto-adds them as a
        # tracked dev; if they're already tracked, their win count climbs (and the
        # heuristic gives their next launch a bigger attention bump). This is exactly
        # the compounding the vault philosophy describes: the brain learns who cooks.
        dev = self._launch_devs.get(mint, "")
        promoted = ""
        win_line = ""
        if dev:
            existing = self.registry.lookup(dev)  # also refreshes last_seen
            note = f"token {mint[:8]} graduated {datetime.now().date()}"
            self.registry.record_win(dev, note=note)
            updated = self.registry.lookup(dev) or {}
            promoted = (f"  DEV CREDITED: {updated.get('label', dev[:6])} "
                        f"now {updated.get('wins', 1)}\u2605" + ("" if existing else " (NEW tracked dev)"))
            win_line = (f"  [pumpfun] migration win -> {dev[:8]} ({updated.get('wins',1)}\u2605)"
                        + (" [new]" if not existing else ""))

        # EVENT LOG FIRST: write the immutable migration record BEFORE any console output. A graduation must never
        # be lost to a Windows console that can't encode a star/emoji on a cosmetic log line (that exact crash was
        # aborting the stream every few seconds and dropping wins from the log the brain/devmetrics rebuild from).
        try:
            import event_log
            event_log.append("migration", mint=mint, dev=(dev or None), payload={"dev_credited": bool(dev)})
        except Exception:
            pass
        if win_line:
            try:
                print(win_line)
            except Exception:
                pass
        title = f"[MIGRATION] {mint[:8]} graduated to PumpSwap/Raydium" + promoted
        self._emit(Signal(
            source    = "pumpfun/migration",
            title     = title,
            url       = PUMP_FUN_URL.format(mint=mint),
            content   = (f"Token {mint} migrated off the bonding curve. Survived to graduation.\n"
                         f"Launching dev: {dev or 'unknown (launched before listener start)'}\n"
                         f"{promoted.strip()}\nRaw: {json.dumps(evt)[:400]}"),
            score_raw = 100,  # graduating is a strong survival signal
            meta      = {"event": "migration", "mint": mint, "dev": dev,
                         "dev_credited": bool(dev), "ts": datetime.now().isoformat()},
        ))

    def _on_account_trade(self, evt: Dict) -> None:
        wallet = evt.get("traderPublicKey", "")
        wflag  = flag(wallet, self.registry)
        if not wflag:
            return  # only care about watched wallets here
        mint = evt.get("mint", "")
        side = evt.get("txType", "")
        # EVENT LOG: an observed trade by a tracked wallet (carries an mc point for time-series).
        try:
            import event_log
            event_log.append("trade", mint=mint, dev=wallet, payload={
                "side": side, "sol": evt.get("solAmount"),
                "market_cap_sol": evt.get("marketCapSol"), "token_amount": evt.get("tokenAmount"),
            })
        except Exception:
            pass
        self._emit(Signal(
            source    = "pumpfun/whale",
            title     = f"[WHALE {side.upper()}] {wflag['label']} on {mint[:8]}",
            url       = PUMP_FUN_URL.format(mint=mint),
            content   = (f"Tracked {wflag['kind']} '{wflag['label']}' ({wflag['wins']} wins) "
                         f"{side} on {mint}.\nRaw: {json.dumps(evt)[:400]}"),
            score_raw = 80,
            meta      = {"event": "whale_trade", "mint": mint, "wallet": wallet,
                         "side": side, "wallet_flag": wflag, "ts": datetime.now().isoformat()},
        ))

    def _dispatch(self, msg: Dict) -> None:
        tx = msg.get("txType")
        if tx == "create" or msg.get("method") == "newToken" or "bondingCurveKey" in msg:
            self._on_new_token(msg)
        elif msg.get("pool") or msg.get("event") == "migration" or tx == "migrate":
            self._on_migration(msg)
        elif tx in ("buy", "sell"):
            self._on_account_trade(msg)
        # subscription confirmations and unknowns are ignored

    # ---- stream loop ----------------------------------------------------

    def start_stream(self) -> None:
        if websocket is None:
            raise RuntimeError("websocket-client not installed. Run: pip install websocket-client")

        url = WS_URL_KEYED.format(key=self.api_key) if self.follow_wallets else WS_URL_FREE
        ws = websocket.create_connection(url, timeout=30)
        print(f"  [pumpfun] connected to {url.split('?')[0]}")

        # All subscriptions go on this ONE connection (PumpPortal rule).
        ws.send(json.dumps({"method": "subscribeNewToken"}))
        ws.send(json.dumps({"method": "subscribeMigration"}))
        if self.follow_wallets:
            watch = self.registry.watched_for_trades()
            if watch:
                ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": watch}))
                print(f"  [pumpfun] following {len(watch)} watched wallets (metered)")

        while self._running:
            try:
                raw = ws.recv()
            except Exception as e:
                print(f"  [pumpfun] recv error: {e}")
                break
            if not raw:
                continue
            try:
                self._dispatch(json.loads(raw))
            except json.JSONDecodeError:
                continue
        try:
            ws.close()
        except Exception:
            pass


# Convenience for a standalone smoke test: prints launches for ~30s.
if __name__ == "__main__":
    ing = PumpFunIngester()
    ing.start_background()
    print("Listening for launches for 30s...")
    for _ in range(6):
        time.sleep(5)
        batch = ing.fetch()
        for s in batch:
            print(f"  {s.title}  [heuristic={s.meta.get('heuristic')}]")
    ing.stop()