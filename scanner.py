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
import re
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
LOG_DIR = DATA / "log"     # one file per run: never conflicts, never overwritten

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
ERR_SAMPLES = {}
TX_VERSION = {"max": 0}   # raised automatically if the network starts using newer tx versions          # reason -> short sample message (shown on the page for diagnosis)


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
        CALLS["n"] += 10 if method == "getTransactionsForAddress" else 1   # credit cost
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
                ERR_SAMPLES.setdefault(last, f"{method}: HTTP 429")
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
                    msg = str((j["error"] or {}).get("message", ""))[:160]
                    if code == -32015:
                        raise RpcError("tx_version", msg)       # caller retries with a newer version
                    if code in (429, -32429) or "rate" in msg.lower():
                        last = "rate_limited"
                    elif "credit" in msg.lower() or "limit" in msg.lower():
                        raise RpcError("budget", msg)
                    else:
                        last = "rpc_error"
                    ERR_SAMPLES.setdefault(last, f"{method}: {code} {msg}")
        time.sleep(1.5 * (attempt + 1) + random.random())
    raise RpcError(last)


def oldest_first_helius(mint, rpc_call=rpc, limit=100):
    """Helius-only: oldest transactions in ONE call (10 credits), no paging needed."""
    res = rpc_call("getTransactionsForAddress", [mint, {"transactionDetails": "signatures",
                                                         "sortOrder": "asc", "limit": limit}])
    rows = (res or {}).get("data") or []
    return [{"signature": r["signature"], "slot": r["slot"], "err": r.get("err")} for r in rows]


def earliest_signatures(mint, rpc_call=rpc, max_pages=3):
    """Oldest transactions of a mint, oldest first. Raises RpcError('too_busy') if unreachable."""
    if RPC_KIND == "helius" and rpc_call is rpc:
        first = rpc_call("getSignaturesForAddress", [mint, {"limit": 1000, "commitment": "confirmed"}]) or []
        if not first:
            raise RpcError("not_indexed_yet")
        if len(first) < 1000:
            return list(reversed(first))           # small coin: 1 credit, done
        try:
            rows = oldest_first_helius(mint, rpc_call)
            if rows:
                return rows                        # busy coin: jump straight to the beginning
        except RpcError:
            pass                                   # fall back to paging below
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


def get_tx(sig, rpc_call=rpc):
    for _ in range(3):
        try:
            return rpc_call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                     "maxSupportedTransactionVersion": TX_VERSION["max"]}])
        except RpcError as e:
            if e.reason != "tx_version":
                raise
            m = re.search(r"version \((\d+)\)", str(e))
            need = int(m.group(1)) if m else TX_VERSION["max"] + 1
            if need <= TX_VERSION["max"]:
                raise
            TX_VERSION["max"] = need
    raise RpcError("tx_version")


def tx_signer_and_delta(sig, mint, rpc_call=rpc):
    tx = get_tx(sig, rpc_call)
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


def analyze_token(mint, creator, rpc_call=rpc, max_pages=3):
    sigs = earliest_signatures(mint, rpc_call, max_pages)
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


def migrate_legacy(data_dir=None):
    """One-time: turn the old wallets/tokens files into a log file so nothing is lost."""
    d = data_dir or DATA
    log = d / "log"
    if log.exists() and any(log.glob("*.jsonl")):
        return
    toks = load(d / "tokens.json", [])
    if not toks:
        return
    log.mkdir(parents=True, exist_ok=True)
    with open(log / "00000000-legacy.jsonl", "w") as f:
        for t in reversed(toks):
            f.write(json.dumps({k: t.get(k) for k in ("mint", "name", "symbol", "creator", "time", "early")},
                               separators=(",", ":")) + "\n")
    old = load(d / "meta.json", {})
    (log / "00000000-legacy.meta.json").write_text(json.dumps(
        {"start": min(t["time"] for t in toks), "end": old.get("last_run"), "runs": old.get("runs", 1),
         "stats": old.get("last_stats", {}), "credits": {}}))


def rebuild(data_dir=None):
    """Aggregate every run log -> (wallets, tokens, meta). Order-independent and merge-safe."""
    d = data_dir or DATA
    migrate_legacy(d)
    log = d / "log"
    recs, metas = [], []
    for f in sorted(log.glob("*.jsonl")) if log.exists() else []:
        for line in f.read_text().splitlines():
            if line.strip():
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
    for f in sorted(log.glob("*.meta.json")) if log.exists() else []:
        m = load(f, None)
        if m:
            metas.append(m)
    seen, uniq = set(), []
    for r in sorted(recs, key=lambda r: r.get("time") or 0):
        if r["mint"] not in seen:            # same coin checked twice -> count once
            seen.add(r["mint"])
            uniq.append(r)
    wallets, tokens = {}, []
    for r in uniq:
        record(wallets, tokens, r, {"early": r.get("early") or []}, now=r.get("time"))
    credits = {}
    for m in metas:
        for k, v in (m.get("credits") or {}).items():
            credits[k] = credits.get(k, 0) + v
    newest = max(metas, key=lambda m: m.get("end") or 0) if metas else {}
    meta = {"runs": sum(m.get("runs", 1) for m in metas), "tokens_total": len(uniq), "credits": credits,
            "last_run": newest.get("end"), "last_stats": newest.get("stats", {})}
    return wallets, tokens, meta


def wallet_groups(tokens_or_recs, min_together=3):
    """Wallets that bought early TOGETHER on min_together+ coins -> likely one operator."""
    from collections import Counter
    from itertools import combinations
    pairs = Counter()
    for t in tokens_or_recs:
        ws = sorted({e["wallet"] for e in (t.get("early") or [])})
        if 2 <= len(ws) <= 25:
            pairs.update(combinations(ws, 2))
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), n in pairs.items():
        if n >= min_together:
            parent[find(a)] = find(b)
    groups = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    out = []
    for members in groups.values():
        if len(members) >= 2:
            together = max(n for (a, b), n in pairs.items() if a in members and b in members)
            out.append({"wallets": sorted(members), "together": together})
    return sorted(out, key=lambda g: (-len(g["wallets"]), -g["together"]))


def radar_credits(month, data_dir=None):
    log = (data_dir or DATA) / "log"
    return sum((load(f, {}).get("credits") or {}).get(month, 0) for f in log.glob("*.meta.json")) \
        if log.exists() else 0


def write_derived(wallets, tokens, meta, data_dir=None):
    d = data_dir or DATA
    (d / "wallets.json").write_text(json.dumps(wallets, separators=(",", ":")))
    (d / "tokens.json").write_text(json.dumps(tokens, separators=(",", ":")))
    (d / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))


def month_key(t=None):
    return time.strftime("%Y-%m", time.gmtime(t or time.time()))


# ---------------------------------------------------------------- main loop
async def run(minutes, max_tokens):
    DATA.mkdir(exist_ok=True)
    wallets, tokens, meta = rebuild(DATA)
    mk = month_key()
    used_before = radar_credits(mk, DATA) if RPC_KIND == "helius" else 0
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + os.urandom(2).hex()
    log_dir = DATA / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / f"{run_id}.jsonl"
    run_meta = log_dir / f"{run_id}.meta.json"
    run_records = []

    start = time.time()
    deadline = start + minutes * 60
    hard_stop = deadline + WAIT_BEFORE_CHECK + 6 * 60     # never run past this
    queue = []                                            # [ready_at, info, attempt]
    stats = {"seen": 0, "queued": 0, "checked": 0, "skipped": 0, "retried": 0,
             "reasons": {}, "rpc": RPC_KIND}
    stop = {"budget": False}

    def save():
        with open(run_log, "w") as f:
            for r in run_records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        st = dict(stats, rpc_calls=CALLS["n"], samples=dict(ERR_SAMPLES))
        run_meta.write_text(json.dumps({"start": int(start), "end": int(time.time()), "runs": 1, "stats": st,
                                        "credits": {mk: CALLS["n"]} if RPC_KIND == "helius" else {}}))
        meta.update(last_run=int(time.time()), last_stats=st,
                    credits=dict(meta.get("credits", {}), **({mk: used_before + CALLS["n"]}
                                                           if RPC_KIND == "helius" else {})))
        write_derived(wallets, tokens, meta, DATA)

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
            run_records.append({**{k: info.get(k) for k in ("mint", "name", "symbol", "creator")},
                                "time": int(time.time()), "early": res["early"]})
            stats["checked"] += 1
            meta["tokens_total"] += 1
            since_save += 1
            if since_save >= SAVE_EVERY:
                save()
                since_save = 0
        # anything still waiting when we hit the hard stop
        for _ in queue:
            fail("out_of_time")

    meta["runs"] = meta.get("runs", 0) + 1
    await asyncio.gather(listen(), worker())
    save()
    print("done", json.dumps(meta["last_stats"]), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("LISTEN_MINUTES") or 40))
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("MAX_TOKENS") or 150))
    a = ap.parse_args()
    asyncio.run(run(a.minutes, a.max_tokens))
