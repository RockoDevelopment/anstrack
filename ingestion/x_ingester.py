"""
Pump.fun Brain -- X / Ansem Ingester
A BaseIngester for one (or a few) X accounts, framed honestly.

THE HONEST PART, up front:
There is no robust *free, programmatic, real-time* way to read one X account anymore.
The free X API tier does not grant tweet read/search. So this ingester does NOT scrape
X (scraping breaks constantly, violates ToS, and gets IP-banned). Instead it reads from
a pluggable `fetcher` so you choose the source and own the tradeoffs:

  1. DROPFILE (default, genuinely free, reliable):
     Turn on post notifications for @blknoiz06 (bell icon). Use an iOS Shortcut /
     Tasker / a Telegram-to-file bot to append each notification's text as one JSON
     line to  <vault>/x_inbox/ansem.jsonl . This ingester tails that file. Zero scraping,
     near-real-time, survives because it rides X's own notification system.

  2. PAID API (drop-in later): set a fetcher that calls TwitterAPI.io / the official
     paid tier for `from:blknoiz06`. Same interface, you just pay for reliability.

  3. MANUAL: paste a tweet into the dropfile yourself. Crude but works for testing.

The point: the ingester is real and drops straight into the pattern. The data source is
a choice you make, not a fragile scraper I hid inside it.

Tweet -> Signal, with CA (contract address) extraction so a launch mention can be linked
to a live pumpfun signal by mint.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from ingestion.base import BaseIngester, Signal

# Solana addresses are base58, 32-44 chars. Pump.fun mints commonly end in "pump".
CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# Words that make a tweet trading-relevant (used only to tag, not to drop).
ALPHA_HINTS = ["pump", "ca", "launch", "send", "ape", "buy", "alpha", "100x", "10x",
               "sending", "live", "mint", "new coin", "this one"]


def dropfile_fetcher(path: Path) -> Callable[[], List[Dict]]:
    """Returns a fetcher that reads new JSONL lines from a notification dropfile.
    Each line: {"text": "...", "ts": "...", "url": "..."} (url optional).
    Tracks a byte offset sidecar so each tweet is ingested once."""
    path = Path(path)
    offset_file = path.with_suffix(".offset")

    def _fetch() -> List[Dict]:
        if not path.exists():
            return []
        start = 0
        if offset_file.exists():
            try:
                start = int(offset_file.read_text().strip() or "0")
            except Exception:
                start = 0
        rows: List[Dict] = []
        with path.open("r", encoding="utf-8") as f:
            f.seek(start)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"text": line})  # tolerate raw-text lines
            end = f.tell()
        offset_file.write_text(str(end))
        return rows

    return _fetch


class XIngester(BaseIngester):
    name = "x"

    def __init__(self, handle: str = "blknoiz06", fetcher: Callable[[], List[Dict]] = None):
        self.handle = handle.lstrip("@")
        if fetcher is not None:
            self.fetcher = fetcher
        else:
            dropfile = Path(config.VAULT_PATH) / "x_inbox" / f"{self.handle}.jsonl"
            dropfile.parent.mkdir(parents=True, exist_ok=True)
            self.fetcher = dropfile_fetcher(dropfile)

    def _extract_cas(self, text: str) -> List[str]:
        # Filter out obvious false positives (URLs, @handles handled by word boundary).
        return [m for m in CA_RE.findall(text or "") if not m.startswith("http")]

    def fetch(self) -> List[Signal]:
        rows = self.fetcher() or []
        signals: List[Signal] = []
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            cas  = self._extract_cas(text)
            low  = text.lower()
            hits = [h for h in ALPHA_HINTS if h in low]
            url  = row.get("url", "") or f"https://x.com/{self.handle}"

            title = f"[X @{self.handle}] {text[:80]}"
            if cas:
                title = f"[X @{self.handle}] CA MENTIONED: {cas[0][:10]}..."

            signals.append(Signal(
                source    = f"x/{self.handle}",
                title     = title,
                url       = url,
                content   = f"@{self.handle} posted:\n{text}\n\nContract addresses: {cas or 'none'}\nHints: {hits}",
                score_raw = 90 if cas else (60 if hits else 20),
                meta      = {
                    "event":   "tweet",
                    "handle":  self.handle,
                    "cas":     cas,
                    "hints":   hits,
                    "ts":      row.get("ts") or datetime.now().isoformat(),
                },
            ))
        return signals
