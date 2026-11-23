"""Sniper Radar - collector (v2).

Plain-words summary:
1. Listens to PumpPortal for brand-new pump.fun coins.
2. A few minutes later, asks a Solana RPC for each coin's very first transactions.
3. Wallets that bought in the SAME block as creation (block 0) or within the next 3 blocks
   are recorded as early buyers. Repeat offenders across many coins = sniper bots.

Never trades, never touches a private key.

v2 additions: failure reasons + one retry for transient failures, request pacing,
monthly credit budget guard, incremental saves, hard stop before the job time limit,
evenly spread sampling across the listening window.
"""
import argparse
import asyncio
import json
import os
import random
import threading
import time
from pathlib import Path

import requests
import websockets

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
RPC_URL = os.environ.get("RPC_URL") or "https://api.mainnet-beta.solana.com"
RPC_KIND = "helius" if "helius" in RPC_URL else "public"
DATA = Path(__file__).parent / "data"
WALLETS_FILE = DATA / "wallets.json"
TOKENS_FILE = DATA / "tokens.json"
META_FILE = DATA / "meta.json"

WAIT_BEFORE_CHECK = 150      # seconds after creation before inspecting a coin
RETRY_DELAY = 60             # seconds before the single retry of a failed coin
SNIPE_WINDOW_SLOTS = 3       # block 0..3 after creation counts as "early"
MAX_EARLY_TX = 40            # inspect at most this many earliest transactions per coin
KEEP_TOKENS = 400            # recent coins kept in tokens.json
SAVE_EVERY = 10              # write files every N checked coins
# Helius free = 10 req/s; public RPC is stricter. Stay under both.
MIN_INTERVAL = 0.13 if RPC_KIND == "helius" else 0.35
MONTHLY_BUDGET = int(os.environ.get("MONTHLY_BUDGET") or 900_000)   # credits (Helius free = 1M)


class RpcError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


_lock = threading.Lock()
_last_call = [0.0]
CALLS = {"n": 0}


def _pace():
    with _lock:
        wait = _last_call[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def rpc(method, params, retries=3):
    last = "other"
    for attempt in range(retries):
        _pace()
        CALLS["n"] += 1
        try:
            r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                             "params": params}, timeout=20)
        except requests.Timeout:
            last = "timeout"
        except requests.RequestException:
            last = "network"
        else:
            if r.status_code == 429:
                last = "rate_limited"
            elif r.status_code in (401, 403):
                raise RpcError("auth", f"HTTP {r.status_code} (API key wrong or credits used up?)")
            elif r.status_code >= 500:
                last = "server_error"
            else:
                try:
                    j = r.json()
                except ValueError:
                    last = "bad_response"
                else:
                    if "error" not in j:
                        return j["result"]
                    code = (j["error"] or {}).get("code")
                    msg = str((j["error"] or {}).get("message", ""))[:80]
                    if code in (429, -32429) or "rate" in msg.lower():
                        last = "rate_limited"
                    elif "credit" in msg.lower() or "limit" in msg.lower():
                        raise RpcError("budget", msg)
                    else:
                        last = "rpc_error"
        time.sleep(1.5 * (attempt + 1) + random.random())
    raise RpcError(last)


def earliest_signatures(mint, rpc_call=rpc, max_pages=3):
    """Oldest transactions of a mint, oldest first. Raises RpcError('too_busy') if unreachable."""
    before, last_page = None, []
    for _ in range(max_pages):
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = rpc_call("getSignaturesForAddress", [mint, opts]) or []
        if not page:
            break
        last_page = page
        if len(page) < 1000:
            return list(reversed(last_page))
        before = page[-1]["signature"]
    if not last_page:
        raise RpcError("not_indexed_yet")
    raise RpcError("too_busy")


def tx_signer_and_delta(sig, mint, rpc_call=rpc):
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
    sigs = earliest_signatures(mint, rpc_call)
    create_slot = sigs[0]["slot"]
    best = {}
    for s in sigs[:MAX_EARLY_TX]:
        gap = s["slot"] - create_slot
        if gap > SNIPE_WINDOW_SLOTS:
            break
        if s.get("err"):
            continue
        signer, delta = tx_signer_and_delta(s["signature"], mint, rpc_call)
        if not signer or signer == creator or delta <= 0:
            continue
        best[signer] = min(best.get(signer, 99), gap)
    return {"create_slot": create_slot,
            "early": [{"wallet": w, "gap": g} for w, g in sorted(best.items(), key=lambda x: x[1])]}


# ---------------------------------------------------------------- storage
def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def record(wallets, tokens, info, result, now=None):
    now = int(now or time.time())
    early = result["early"]
    tokens.insert(0, {
        "mint": info["mint"], "name": info.get("name", ""), "symbol": info.get("symbol", ""),
        "creator": info.get("creator", ""), "time": now,
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
        w["recent"] = ([info.get("symbol") or info["mint"][:6]] + w["recent"])[:8]


def month_key(t=None):
    return time.strftime("%Y-%m", time.gmtime(t or time.time()))


# ---------------------------------------------------------------- main loop
async def run(minutes, max_tokens):
    DATA.mkdir(exist_ok=True)
    wallets = load(WALLETS_FILE, {})
    tokens = load(TOKENS_FILE, [])
    meta = load(META_FILE, {})
    meta.setdefault("runs", 0)
    meta.setdefault("tokens_total", 0)
    credits = meta.setdefault("credits", {})
    mk = month_key()
    used_before = credits.get(mk, 0) if RPC_KIND == "helius" else 0

    start = time.time()
    deadline = start + minutes * 60
    hard_stop = deadline + WAIT_BEFORE_CHECK + 6 * 60     # never run past this
    queue = []                                            # [ready_at, info, attempt]
    stats = {"seen": 0, "queued": 0, "checked": 0, "skipped": 0, "retried": 0,
             "reasons": {}, "rpc": RPC_KIND}
    stop = {"budget": False}

    def save():
        WALLETS_FILE.write_text(json.dumps(wallets, separators=(",", ":")))
        TOKENS_FILE.write_text(json.dumps(tokens, separators=(",", ":")))
        if RPC_KIND == "helius":
            credits[mk] = used_before + CALLS["n"]
        for k in list(credits):            # keep 3 months
            if k < month_key(time.time() - 95 * 86400):
                credits.pop(k)
        meta.update(last_run=int(time.time()), last_stats=dict(stats, rpc_calls=CALLS["n"]))
        META_FILE.write_text(json.dumps(meta, separators=(",", ":")))

    async def listen():
        while time.time() < deadline:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("connected to PumpPortal", flush=True)
                    while time.time() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if msg.get("txType") != "create" or not msg.get("mint"):
                            continue
                        stats["seen"] += 1
                        # spread the per-run quota evenly over the listening window
                        frac = (time.time() - start) / (minutes * 60)
                        allowed = min(max_tokens, int(max_tokens * frac) + 5)
                        if stats["queued"] < allowed and not stop["budget"]:
                            stats["queued"] += 1
                            queue.append([time.time() + WAIT_BEFORE_CHECK, {
                                "mint": msg["mint"], "name": msg.get("name", ""),
                                "symbol": msg.get("symbol", ""),
                                "creator": msg.get("traderPublicKey", "")}, 0])
            except Exception as e:
                print("ws error, reconnecting:", repr(e)[:120], flush=True)
                await asyncio.sleep(5)

    def fail(reason):
        stats["skipped"] += 1
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1

    async def worker():
        since_save = 0
        while time.time() < hard_stop:
            if time.time() > deadline and not queue:
                break
            ready = [q for q in queue if q[0] <= time.time()]
            if not ready:
                await asyncio.sleep(1)
                continue
            item = min(ready, key=lambda q: q[0])
            queue.remove(item)
            _, info, attempt = item
            if RPC_KIND == "helius" and used_before + CALLS["n"] > MONTHLY_BUDGET:
                stop["budget"] = True
                fail("monthly_budget_reached")
                continue
            try:
                res = await asyncio.to_thread(analyze_token, info["mint"], info["creator"])
            except RpcError as e:
                if e.reason in ("auth", "budget"):
                    stop["budget"] = True
                    fail(e.reason)
                    print("STOP:", e, flush=True)
                    continue
                if attempt == 0 and e.reason not in ("too_busy",):
                    stats["retried"] += 1
                    queue.append([time.time() + RETRY_DELAY, info, 1])
                    continue
                fail(e.reason)
                continue
            except Exception as e:
                fail("unexpected")
                print("unexpected", info["mint"], repr(e)[:160], flush=True)
                continue
            record(wallets, tokens, info, res)
            stats["checked"] += 1
            meta["tokens_total"] += 1
            since_save += 1
            if since_save >= SAVE_EVERY:
                save()
                since_save = 0
        # anything still waiting when we hit the hard stop
        for _ in queue:
            fail("out_of_time")

    meta["runs"] += 1
    await asyncio.gather(listen(), worker())
    save()
    print("done", json.dumps(meta["last_stats"]), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("LISTEN_MINUTES") or 40))
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("MAX_TOKENS") or 150))
    a = ap.parse_args()
    asyncio.run(run(a.minutes, a.max_tokens))
