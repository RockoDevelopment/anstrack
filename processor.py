"""
Pump.fun Brain -- Processing Pipeline
Adapted from the LAIS processor. Same skeleton: ingest -> batch -> analyse -> save ->
flag alerts, with a threading lock so cycles never stack. The one structural change for
on-chain data: the PumpFun source is a long-running websocket, so we start the listener
once at boot, then the scheduled cycle DRAINS the live buffer instead of polling HTTP.

Run modes:
  python processor.py once    -- start streams, wait a beat, run a single cycle
  python processor.py loop    -- start streams, run a cycle every CYCLE_SECONDS
"""
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
import memory
import ingestion
from ingestion import ALL_INGESTERS, start_streams, stop_streams
from intelligence import analyse_signals

BATCH_SIZE          = 5
BATCH_DELAY_S       = 70
MAX_RETRIES         = 1
RETRY_BACKOFF_S     = 90
MAX_SIGNALS_PER_RUN = 15     # launches arrive fast; drain more per cycle
CYCLE_SECONDS       = 60

_pipeline_lock = threading.Lock()


def _analyse_with_retry(batch: List[Dict]) -> Dict:
    last_error = None
    wait = RETRY_BACKOFF_S
    for attempt in range(MAX_RETRIES):
        try:
            return analyse_signals(batch)
        except Exception as e:
            last_error = e
            s = str(e)
            if "429" in s or "Too Many Requests" in s or "RESOURCE_EXHAUSTED" in s:
                print(f"  [pipeline] Rate limited ({attempt+1}/{MAX_RETRIES}). Waiting {wait}s...")
                time.sleep(wait)
                wait *= 2
            else:
                raise
    raise last_error


def run_ingestion() -> Dict:
    """Drain every ingester (PumpFun buffer + X dropfile) and save signals."""
    results, total = [], 0
    for make in ALL_INGESTERS:
        ingester = make()              # lambda returns the persistent instance
        result   = ingester.run()
        results.append(result)
        total   += result.get("count", 0)
    return {"ingesters": results, "total_signals": total,
            "timestamp": datetime.now().isoformat()}


def run_analysis_pipeline() -> Dict:
    if not _pipeline_lock.acquire(blocking=False):
        print("  [pipeline] Another cycle already running. Skipping.")
        return {"ok": True, "msg": "Skipped", "calls_found": 0}
    try:
        signals = memory.get_unprocessed_signals(limit=MAX_SIGNALS_PER_RUN)
        if not signals:
            return {"ok": True, "msg": "No unprocessed signals", "calls_found": 0}

        all_calls, processed_ids, errors = [], [], []
        total_batches = (len(signals) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  [pipeline] Analysing {len(signals)} signals in {total_batches} batches...")

        for i, start in enumerate(range(0, len(signals), BATCH_SIZE)):
            batch = signals[start:start + BATCH_SIZE]
            try:
                result = _analyse_with_retry(batch)
                for call in result.get("opportunities", []):
                    cid = memory.save_opportunity(call)
                    call["id"] = cid
                    all_calls.append(call)
                processed_ids += [s["id"] for s in batch]
                print(f"  [pipeline] Batch {i+1}: {len(result.get('opportunities', []))} calls")
            except Exception as e:
                errors.append({"batch_start": start, "error": str(e),
                               "traceback": traceback.format_exc()})
                print(f"  [pipeline] Batch {i+1} failed: {e}")
            if i < total_batches - 1:
                time.sleep(BATCH_DELAY_S)

        memory.mark_signals_processed(processed_ids)
        alerts = [c for c in all_calls if c.get("score", 0) >= config.ALPHA_ALERT_THRESHOLD]
        return {
            "ok":               True,
            "signals_processed": len(processed_ids),
            "calls_found":       len(all_calls),
            "alerts":            len(alerts),
            "alert_mints":       [c.get("mint") for c in alerts],
            "errors":            errors,
            "timestamp":         datetime.now().isoformat(),
        }
    finally:
        _pipeline_lock.release()


def run_full_cycle() -> Dict:
    t0 = datetime.now()
    ingest  = run_ingestion()
    analyse = run_analysis_pipeline()
    return {"ingestion": ingest, "analysis": analyse,
            "duration_s": round((datetime.now() - t0).total_seconds(), 2),
            "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    print("  [boot] starting PumpPortal listener...")
    # Activate the event-driven Brain: importing it subscribes handle() to the event log,
    # so every event ingestion appends now incrementally updates the Brain. Warm it from
    # the persisted log first so it continues from history rather than cold.
    try:
        import brain
        brain.rebuild()
        print(f"  [boot] brain online — {brain.snapshot()['outcome']}")
    except Exception as e:
        print(f"  [boot] brain init skipped: {e}")
    start_streams()
    time.sleep(8)  # let the buffer collect a few launches before first drain
    try:
        if mode == "loop":
            while True:
                print(f"\n  [cycle] {datetime.now().isoformat()}")
                print(run_full_cycle())
                time.sleep(CYCLE_SECONDS)
        else:
            print(run_full_cycle())
    except KeyboardInterrupt:
        print("\n  [boot] stopping...")
    finally:
        stop_streams()