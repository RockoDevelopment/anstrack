"""
Pump.fun Brain -- Base Ingester
Reused verbatim from the LAIS project (Signal + BaseIngester), with one addition:
StreamingIngester, which lets a long-running websocket fill a buffer that the normal
poll-based processor can still drain via fetch(). This keeps the clean contract --
every source is a class with one fetch() returning Signals -- while supporting the
push-shaped, time-sensitive nature of on-chain data.
"""
import sys
import threading
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
# NOTE: `memory` (the LAIS vault layer) is imported lazily inside Signal.save() rather
# than at module load, so this package imports cleanly in contexts that never save
# signals (the local server, tests) even before you've dropped memory.py in.


class Signal:
    """A single piece of raw intelligence. Same shape as LAIS so memory.save_signal
    and the analyst flow work unchanged. `meta` carries structured on-chain fields
    (mint, dev wallet, market cap, etc.) that the analyst and UI read."""
    def __init__(self, source: str, title: str, url: str = "",
                 content: str = "", score_raw: int = 0, meta: Dict = None):
        self.source    = source
        self.title     = title
        self.url       = url
        self.content   = content
        self.score_raw = score_raw      # native engagement metric (volume, sol, votes)
        self.meta      = meta or {}      # structured fields for downstream scoring

    def save(self) -> str:
        """Save via the vault layer. Falls back gracefully if the existing LAIS
        memory.save_signal doesn't yet accept a `meta` argument -- so this drops in
        without forcing a memory-layer rewrite (meta is then folded into content)."""
        import memory  # lazy: only needed when actually saving
        try:
            return memory.save_signal(
                source=self.source, title=self.title,
                url=self.url, content=self.content, score_raw=self.score_raw,
                meta=self.meta,
            )
        except TypeError:
            import json as _json
            blob = self.content
            if self.meta:
                blob += "\n\n<meta>" + _json.dumps(self.meta) + "</meta>"
            return memory.save_signal(
                source=self.source, title=self.title,
                url=self.url, content=blob, score_raw=self.score_raw,
            )


class BaseIngester(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self) -> List[Signal]:
        """Fetch new signals from the source. Must be implemented."""
        ...

    def run(self) -> Dict:
        """Fetch signals and save them to the vault."""
        t0 = datetime.now()
        signals: List[Signal] = []
        error = None
        try:
            signals = self.fetch()
            for s in signals:
                s.save()
        except Exception as e:
            error = str(e)
        duration = round((datetime.now() - t0).total_seconds(), 2)
        return {
            "ingester":   self.name,
            "count":      len(signals),
            "duration_s": duration,
            "ok":         error is None,
            "error":      error,
            "timestamp":  datetime.now().isoformat(),
        }


class StreamingIngester(BaseIngester):
    """
    Bridges a push-based source (websocket) into the pull-based ingester contract.

    A background thread runs start_stream() and calls self._emit(signal) on every
    event. fetch() simply drains whatever has accumulated since the last call. The
    processor loop therefore doesn't change: it still calls run() on a schedule, but
    instead of hitting an HTTP endpoint, run() collects what the live stream buffered.

    The buffer is bounded (maxlen) so a quiet consumer can never balloon memory during
    a launch storm -- oldest events drop first.
    """
    buffer_size: int = 2000

    def __init__(self):
        self._buffer: Deque[Signal] = deque(maxlen=self.buffer_size)
        self._lock = threading.Lock()
        self._thread: threading.Thread = None
        self._running = False

    def _emit(self, signal: Signal) -> None:
        with self._lock:
            self._buffer.append(signal)

    def fetch(self) -> List[Signal]:
        with self._lock:
            drained = list(self._buffer)
            self._buffer.clear()
        return drained

    @abstractmethod
    def start_stream(self) -> None:
        """Long-running loop: connect, subscribe, call self._emit() per event.
        Should reconnect on drop. Runs inside the background thread."""
        ...

    def start_background(self) -> None:
        """Spin up the listener thread once. Idempotent."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_stream_safe, daemon=True)
        self._thread.start()

    def _run_stream_safe(self) -> None:
        while self._running:
            try:
                self.start_stream()
            except Exception as e:
                print(f"  [{self.name}] stream crashed: {e} -- restarting in 5s")
                import time
                time.sleep(5)

    def stop(self) -> None:
        self._running = False
