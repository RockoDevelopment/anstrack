"""
Pump.fun Brain -- Wallet Registry
Tracks known dev and whale wallets so the analyst can flag "repeat dev" and
"smart-money buy" the moment a launch or trade involves a watched address.

This is a local JSON-backed registry, not a paid service. You seed it from:
  - public Dune dashboards of profitable Pump.fun wallets
  - your own observed winners (the brain appends devs whose past launches ran)
  - X/Telegram alpha callers' wallets if you have them

Honest note on "repeat dev = alpha": repeat-dev tracking is a real edge AND a known
honeypot. Sophisticated actors deliberately seed a wallet with a few wins so trackers
pile into the next launch -- which is the rug. So a repeat-dev hit raises attention,
it never auto-confirms safety. The analyst treats it as one input, weighed against
mint/freeze authority, holder spread, and sell behavior.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

REGISTRY_PATH = Path(config.VAULT_PATH) / "wallets" / "registry.json"


class WalletRegistry:
    """label -> {wallet, kind, notes, wins, last_seen}. kind in {dev, whale, caller}."""

    def __init__(self, path: Path = REGISTRY_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict] = self._load()
        self._known: set = set(self._data.keys())  # for detecting removals to sync to Postgres

    def _load(self) -> Dict[str, Dict]:
        # Postgres is the durable source of truth in production (survives Render redeploys).
        # If it's configured and reachable, load from it; the JSON file stays as a local mirror.
        try:
            import db
            if db.enabled():
                reg = db.load_registry()
                if reg is not None:
                    return reg
        except Exception:
            pass
        # No Postgres (local dev) or it's unreachable -> read the file vault.
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        # Always mirror to the disk file (safety net + local dev). When Postgres is available,
        # also upsert there so the registry persists across redeploys. Postgres failure never
        # loses a write because the file mirror above already succeeded.
        try:
            self.path.write_text(json.dumps(self._data, indent=2))
        except Exception:
            pass
        try:
            import db
            if db.enabled():
                db.save_registry(self._data)
                # sync deletions: any wallet we knew about but that's now gone must be removed
                # from Postgres too, otherwise it would resurrect on the next _load().
                for gone in (self._known - set(self._data.keys())):
                    try:
                        db.delete_wallet(gone)
                    except Exception:
                        pass
        except Exception:
            pass
        self._known = set(self._data.keys())

    def add(self, wallet: str, kind: str = "whale",
            label: str = "", notes: str = "", wins: int = 0) -> None:
        self._data[wallet] = {
            "wallet":    wallet,
            "kind":      kind,
            "label":     label or wallet[:6],
            "notes":     notes,
            "wins":      wins,
            "added_at":  self._data.get(wallet, {}).get("added_at", datetime.now().isoformat()),
            "last_seen": None,
        }
        self._save()

    def record_win(self, wallet: str, note: str = "") -> None:
        """Append a confirmed winner -- the brain calls this when a tracked dev's
        launch runs, so the registry compounds the way the vault philosophy intends."""
        entry = self._data.get(wallet) or {
            "wallet": wallet, "kind": "dev", "label": wallet[:6],
            "notes": "", "wins": 0, "launches": 0, "added_at": datetime.now().isoformat(), "last_seen": None,
        }
        entry["wins"] = entry.get("wins", 0) + 1
        entry["last_win"] = datetime.now().isoformat()
        entry["reputation"] = self._reputation(entry)
        if note:
            entry["notes"] = (entry.get("notes", "") + f" | {note}").strip(" |")
        self._data[wallet] = entry
        self._save()

    def record_launch(self, wallet: str) -> None:
        """Count a dev's launches so the brain can learn a hit-rate (wins / launches).
        Only tracks devs already known (tracked or prior winners) to stay bounded."""
        entry = self._data.get(wallet)
        if not entry:
            return
        entry["launches"] = entry.get("launches", 0) + 1
        entry["reputation"] = self._reputation(entry)
        self._data[wallet] = entry
        self._save()

    @staticmethod
    def _reputation(entry: Dict) -> Dict:
        """A compounding reputation the brain learns over time: graduation count is the
        spine; hit-rate (grads / launches) and recency refine it. Tiers advance with wins."""
        wins = entry.get("wins", 0)
        launches = max(entry.get("launches", 0), wins)
        hit_rate = round(wins / launches, 2) if launches else 0.0
        tier = ("elite" if wins >= 5 else "proven" if wins >= 2 else
                "winner" if wins >= 1 else "tracked")
        # score blends volume of graduations with how reliably they happen
        score = min(100, wins * 14 + int(hit_rate * 30))
        return {"tier": tier, "hit_rate": hit_rate, "score": score,
                "wins": wins, "launches": launches}

    def lookup(self, wallet: str) -> Optional[Dict]:
        entry = self._data.get(wallet)
        if entry:
            entry["last_seen"] = datetime.now().isoformat()
            self._save()
        return entry

    def all_wallets(self) -> List[str]:
        return list(self._data.keys())

    def watched_for_trades(self) -> List[str]:
        """Wallets worth paying the metered trade-stream cost to follow.
        Default: devs/whales with at least one recorded win, to keep the bill down."""
        return [w for w, e in self._data.items() if e.get("wins", 0) >= 1]


def flag(wallet: str, registry: WalletRegistry) -> Dict:
    """Return a flag dict for a wallet involved in an event, or empty if unknown."""
    entry = registry.lookup(wallet)
    if not entry:
        return {}
    return {
        "wallet":      wallet,
        "kind":        entry["kind"],
        "label":       entry["label"],
        "wins":        entry.get("wins", 0),
        "is_repeat":   entry.get("wins", 0) >= 1,
        "notes":       entry.get("notes", ""),
    }