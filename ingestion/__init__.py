"""
Pump.fun Brain -- Ingestion package
Wires the live sources. The PumpFun stream and X ingester share one WalletRegistry.

The processor calls run() on each of ALL_INGESTERS. For the streaming PumpFun source,
run() drains the live buffer; start_background() must be called once at boot to start
the websocket listener (the runner does this).
"""
from ingestion.wallets import WalletRegistry
from ingestion.pumpfun import PumpFunIngester
from ingestion.x_ingester import XIngester

# Shared registry so launches, whale trades, and the analyst all see the same wallets.
REGISTRY = WalletRegistry()

# Streaming source -- instantiate once so the buffer persists across cycles.
PUMPFUN = PumpFunIngester(registry=REGISTRY)
X_ANSEM = XIngester(handle="blknoiz06")

ALL_INGESTERS_INSTANCES = [PUMPFUN, X_ANSEM]

# Compatibility with the LAIS processor, which iterates classes and instantiates.
# Here we hand it pre-built instances via a tiny shim so streaming state survives.
ALL_INGESTERS = [lambda inst=inst: inst for inst in ALL_INGESTERS_INSTANCES]


def start_streams() -> None:
    """Call once at boot to start long-running listeners."""
    PUMPFUN.start_background()


def stop_streams() -> None:
    PUMPFUN.stop()
