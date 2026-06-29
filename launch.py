#!/usr/bin/env python3
"""
ANSTRACK — token launcher
Creates the $ANSTRACK token on Pump.fun using PumpPortal's Local Transaction API.
Your keys never leave this machine: PumpPortal builds the transaction, you sign it
here with solders and broadcast it through your own RPC.

FLOW
  1) Upload image + metadata to pump.fun IPFS.
  2) Generate the mint keypair (this is your token's contract address / CA).
  3) Ask PumpPortal to build the `create` transaction (optionally with a dev buy).
  4) Sign locally with the creator + mint keypairs and send to Solana.
  5) Print the CA, then the exact next step to turn on 60% fee-sharing.

Run:  python3 launch.py
Config comes from .env (copy .env.example -> .env first).
"""
import os, json, sys, pathlib
import requests

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.commitment_config import CommitmentLevel
    from solders.rpc.requests import SendVersionedTransaction
    from solders.rpc.config import RpcSendTransactionConfig
except ImportError:
    sys.exit("Missing deps. Run:  pip install -r requirements.txt")

HERE = pathlib.Path(__file__).parent

def load_env():
    env = {}
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # environment overrides file
    for k in ("CREATOR_PRIVATE_KEY", "RPC_URL", "TOKEN_NAME", "TOKEN_SYMBOL",
              "TOKEN_DESCRIPTION", "TOKEN_IMAGE", "TWITTER", "TELEGRAM", "WEBSITE",
              "DEV_BUY_SOL", "SLIPPAGE", "PRIORITY_FEE"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env

def main():
    env = load_env()
    priv = env.get("CREATOR_PRIVATE_KEY", "")
    rpc = env.get("RPC_URL", "https://api.mainnet-beta.solana.com")
    if not priv or priv.startswith("PASTE"):
        sys.exit("Set CREATOR_PRIVATE_KEY in .env (base58 secret key of the launching wallet).")

    name = env.get("TOKEN_NAME", "ANSTRACK")
    symbol = env.get("TOKEN_SYMBOL", "ANSTRACK")
    desc = env.get("TOKEN_DESCRIPTION", "The Ansem Promise: 60% of creator fees routed straight into liquidity, provable on-chain.")
    image = env.get("TOKEN_IMAGE", str(HERE.parent / "anstrack-logo.png"))
    dev_buy = float(env.get("DEV_BUY_SOL", "0") or 0)
    slippage = int(float(env.get("SLIPPAGE", "10")))
    prio = float(env.get("PRIORITY_FEE", "0.0005"))

    creator = Keypair.from_base58_string(priv)
    mint = Keypair()  # the token's CA
    print("Creator wallet :", creator.pubkey())
    print("New token CA   :", mint.pubkey())

    # 1) IPFS metadata ----------------------------------------------------------
    if not pathlib.Path(image).exists():
        sys.exit(f"Image not found: {image}")
    print("Uploading metadata to pump.fun IPFS ...")
    form = {
        "name": name, "symbol": symbol, "description": desc,
        "twitter": env.get("TWITTER", ""), "telegram": env.get("TELEGRAM", ""),
        "website": env.get("WEBSITE", ""), "showName": "true",
    }
    with open(image, "rb") as fh:
        files = {"file": (pathlib.Path(image).name, fh.read(), "image/png")}
    meta = requests.post("https://pump.fun/api/ipfs", data=form, files=files, timeout=30)
    meta.raise_for_status()
    meta_json = meta.json()
    metadata_uri = meta_json.get("metadataUri") or meta_json.get("uri")
    if not metadata_uri:
        sys.exit("IPFS upload did not return a metadataUri: " + meta.text[:300])
    print("Metadata URI   :", metadata_uri)

    # 2) Build create tx via PumpPortal ----------------------------------------
    print("Requesting create transaction from PumpPortal ...")
    resp = requests.post("https://pumpportal.fun/api/trade-local", data={
        "publicKey": str(creator.pubkey()),
        "action": "create",
        "tokenMetadata": json.dumps({"name": name, "symbol": symbol, "uri": metadata_uri}),
        "mint": str(mint.pubkey()),
        "denominatedInSol": "true",
        "amount": dev_buy,            # dev buy in SOL (0 = none)
        "slippage": slippage,
        "priorityFee": prio,
        "pool": "pump",
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit("PumpPortal create failed: " + resp.text[:400])

    # 3) Sign locally (creator + mint) and send --------------------------------
    tx = VersionedTransaction(VersionedTransaction.from_bytes(resp.content).message, [mint, creator])
    cfg = RpcSendTransactionConfig(preflight_commitment=CommitmentLevel.Confirmed)
    payload = SendVersionedTransaction(tx, cfg)
    send = requests.post(rpc, headers={"Content-Type": "application/json"}, data=payload.to_json(), timeout=30)
    out = send.json()
    if out.get("error"):
        sys.exit("Send failed: " + json.dumps(out["error"]))
    sig = out.get("result")
    print("\n  TOKEN LIVE")
    print("  CA       :", mint.pubkey())
    print("  Tx       : https://solscan.io/tx/" + str(sig))

    # persist the CA so the bot + landing page can pick it up
    (HERE / "launched.json").write_text(json.dumps({
        "mint": str(mint.pubkey()), "creator": str(creator.pubkey()),
        "tx": str(sig), "name": name, "symbol": symbol,
    }, indent=2))
    print("  Saved CA -> launch/launched.json")

    print("""
  NEXT — turn on the Ansem Promise (60% creator fees -> liquidity wallet):
    1. Open pump.fun, connect the CREATOR wallet, go to your token's page.
    2. Open "Fees" / "Fee sharing" and add your liquidity wallet at 60% (6000 bps),
       and the remaining 40% to your project wallet (for DEX updates etc).
    3. Paste that liquidity wallet into launch/.env as RECIPIENT_PRIVATE_KEY and
       set TOKEN_MINT, then run:  python3 auto_lp_bot.py
    4. Put the CA into index.html (var CA = "...") and set LP.treasury to the
       liquidity wallet so the landing page shows injections live.
  Fee-sharing config is an on-chain pump.fun step done AFTER creation — there is no
  one-click interval native to it; auto_lp_bot.py provides the scheduled injections.
""")

if __name__ == "__main__":
    main()