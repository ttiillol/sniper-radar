"""Sniper Radar - collector.

What it does (plain words):
1. Listens to PumpPortal for brand-new pump.fun coins.
2. Waits a few minutes, then asks a Solana RPC for the coin's very first transactions.
3. Any wallet that bought in the SAME block as the coin's creation (block 0) or within the
   next few blocks is recorded as an "early buyer".
4. Wallets that keep showing up as early buyers across many coins = sniper bot candidates.

It never trades and never needs a private key.
"""
import argparse
import asyncio
import json
import os
import random
import time
from pathlib import Path

import requests
import websockets

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
RPC_URL = os.environ.get("RPC_URL") or "https://api.mainnet-beta.solana.com"
DATA = Path(__file__).parent / "data"
WALLETS_FILE = DATA / "wallets.json"
TOKENS_FILE = DATA / "tokens.json"

WAIT_BEFORE_CHECK = 150      # seconds to wait after a coin appears before inspecting it
SNIPE_WINDOW_SLOTS = 3       # block 0 .. block 3 after creation counts as "early"
MAX_EARLY_TX = 40            # inspect at most this many earliest transactions per coin
KEEP_TOKENS = 300            # recent coins kept in tokens.json


# ---------------------------------------------------------------- RPC helpers
def rpc(method, params, retries=4):
    for attempt in range(retries):
        try:
            r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                             "params": params}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j["result"]
        except Exception as e:  # network hiccup / rate limit
            if attempt == retries - 1:
                raise
            time.sleep(1.5 + attempt * 2 + random.random())


def earliest_signatures(mint, rpc_call=rpc, max_pages=3):
    """Return the oldest transactions of a mint (oldest first) as [{signature, slot, err}]."""
    before, pages, last_page = None, 0, []
    while pages < max_pages:
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = rpc_call("getSignaturesForAddress", [mint, opts]) or []
        pages += 1
        if not page:
            break
        last_page = page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return None  # too busy to reach the beginning -> skip
    return list(reversed(last_page))


def tx_signer_and_delta(sig, mint, rpc_call=rpc):
    """Who signed the tx, and how many tokens of `mint` their balance changed by."""
    tx = rpc_call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                           "maxSupportedTransactionVersion": 0}])
    if not tx:
        return None, 0.0
    keys = tx["transaction"]["message"]["accountKeys"]
    signer = keys[0] if isinstance(keys[0], str) else keys[0].get("pubkey")
    meta = tx.get("meta") or {}

    def bal(lst):
        for b in lst or []:
            if b.get("mint") == mint and b.get("owner") == signer:
                return float(b["uiTokenAmount"].get("uiAmount") or 0)
        return 0.0

    return signer, bal(meta.get("postTokenBalances")) - bal(meta.get("preTokenBalances"))


def analyze_token(mint, creator, rpc_call=rpc):
    """Find wallets that bought within SNIPE_WINDOW_SLOTS blocks of creation."""
    sigs = earliest_signatures(mint, rpc_call)
    if not sigs:
        return None
    create_slot = sigs[0]["slot"]
    early = []
    for s in sigs[:MAX_EARLY_TX]:
        gap = s["slot"] - create_slot
        if gap > SNIPE_WINDOW_SLOTS:
            break
        if s.get("err"):
            continue
        signer, delta = tx_signer_and_delta(s["signature"], mint, rpc_call)
        if not signer or signer == creator or delta <= 0:
            continue  # creator's own buy, failed tx, or not a buy
        early.append({"wallet": signer, "gap": gap})
    # one entry per wallet, keep its earliest gap
    best = {}
    for e in early:
        best[e["wallet"]] = min(best.get(e["wallet"], 99), e["gap"])
    return {"create_slot": create_slot,
            "early": [{"wallet": w, "gap": g} for w, g in sorted(best.items(), key=lambda x: x[1])]}


# ---------------------------------------------------------------- database
def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def record(wallets, tokens, token_info, result, now=None):
    now = int(now or time.time())
    early = result["early"]
    tokens.insert(0, {
        "mint": token_info["mint"], "name": token_info.get("name", ""),
        "symbol": token_info.get("symbol", ""), "creator": token_info.get("creator", ""),
        "time": now,
        "block0": sum(1 for e in early if e["gap"] == 0),
        "snipers": sum(1 for e in early if e["gap"] > 0),
        "early": early[:30],
    })
    del tokens[KEEP_TOKENS:]
    for e in early:
        w = wallets.setdefault(e["wallet"], {"count": 0, "block0": 0, "first": now,
                                             "last": now, "recent": []})
        w["count"] += 1
        if e["gap"] == 0:
            w["block0"] += 1
        w["last"] = now
        w["recent"] = ([token_info.get("symbol") or token_info["mint"][:6]] + w["recent"])[:8]


# ---------------------------------------------------------------- main loop
async def run(minutes, max_tokens):
    DATA.mkdir(exist_ok=True)
    wallets = load(WALLETS_FILE, {})
    tokens = load(TOKENS_FILE, [])
    queue = []                      # (ready_at, token_info)
    deadline = time.time() + minutes * 60
    stats = {"seen": 0, "checked": 0, "skipped": 0}

    async def listen():
        while time.time() < deadline:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("connected to PumpPortal")
                    while time.time() < deadline:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                        if msg.get("txType") != "create" or not msg.get("mint"):
                            continue
                        stats["seen"] += 1
                        # sample evenly so one run doesn't exceed the RPC budget
                        if len(queue) + stats["checked"] < max_tokens:
                            queue.append((time.time() + WAIT_BEFORE_CHECK, {
                                "mint": msg["mint"], "name": msg.get("name", ""),
                                "symbol": msg.get("symbol", ""),
                                "creator": msg.get("traderPublicKey", "")}))
            except Exception as e:
                print("ws error, reconnecting:", e)
                await asyncio.sleep(5)

    async def worker():
        # keep working until the queue is empty after the listening window
        while time.time() < deadline + WAIT_BEFORE_CHECK + 30 or queue:
            ready = [q for q in queue if q[0] <= time.time()]
            if not ready:
                if time.time() > deadline + WAIT_BEFORE_CHECK + 30:
                    break
                await asyncio.sleep(2)
                continue
            item = ready[0]
            queue.remove(item)
            info = item[1]
            try:
                res = await asyncio.to_thread(analyze_token, info["mint"], info["creator"])
            except Exception as e:
                print("rpc error", info["mint"], e)
                res = None
            if res is None:
                stats["skipped"] += 1
                continue
            record(wallets, tokens, info, res)
            stats["checked"] += 1
            print(f"{info.get('symbol','?'):>10} block0={sum(1 for e in res['early'] if e['gap']==0)}"
                  f" snipers={sum(1 for e in res['early'] if e['gap']>0)}")
            await asyncio.sleep(0.5)

    await asyncio.gather(listen(), worker())
    WALLETS_FILE.write_text(json.dumps(wallets, separators=(",", ":")))
    TOKENS_FILE.write_text(json.dumps(tokens, separators=(",", ":")))
    meta = load(DATA / "meta.json", {"runs": 0, "tokens_total": 0})
    meta.update(runs=meta["runs"] + 1, tokens_total=meta["tokens_total"] + stats["checked"],
                last_run=int(time.time()), last_stats=stats)
    (DATA / "meta.json").write_text(json.dumps(meta))
    print("done", stats)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20, help="how long to listen")
    ap.add_argument("--max-tokens", type=int, default=120, help="max coins to inspect per run")
    a = ap.parse_args()
    asyncio.run(run(a.minutes, a.max_tokens))
