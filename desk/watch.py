"""WALLET WATCH: follow a few named wallets closely (e.g. the wallet widely attributed to Ansem).

Every minute: ask the Solana RPC for the wallet's newest transactions, read the token balance
changes of that wallet, and report buys/sells. A bought coin is added to the desk's candidates
(it still has to pass every safety and momentum check - nothing is bought blindly).

Wallets come from desk/kol_wallets.txt lines with tier W (watch). The label says how sure we are.
"""
import time

STABLE = {"So11111111111111111111111111111111111111112",      # wSOL
          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",      # USDC
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}      # USDT
MAX_NEW_TX = 6                                                # per wallet per poll (keeps RPC use small)


def wallets(kols):
    return {a: v for a, v in kols.items() if v.get("tier") == "W"}


def _changes(tx, owner):
    """{mint: delta} of token balances owned by `owner` in one transaction (stablecoins/SOL ignored)."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return {}

    def bal(rows):
        out = {}
        for r in rows or []:
            if r.get("owner") != owner or r.get("mint") in STABLE:
                continue
            amt = (r.get("uiTokenAmount") or {}).get("uiAmount") or 0
            out[r["mint"]] = out.get(r["mint"], 0) + float(amt)
        return out
    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    ch = {}
    for m in set(pre) | set(post):
        d = post.get(m, 0) - pre.get(m, 0)
        if abs(d) > 1e-9:
            ch[m] = d
    return ch


def poll(state, kols, rpc, now=None):
    """Returns a list of events {wallet, name, mint, side, amount, sig, ts}. Never raises."""
    now = now or time.time()
    seen = state.setdefault("wwatch", {})
    events = []
    for addr, info in wallets(kols).items():
        try:
            w = seen.setdefault(addr, {"last": None})
            sigs = rpc("getSignaturesForAddress", [addr, {"limit": 20}]) or []
            if w["last"] is None:                  # first look: remember where we are, report nothing old
                w["last"] = sigs[0]["signature"] if sigs else ""
                continue
            new = []
            for s in sigs:
                if s.get("signature") == w["last"]:
                    break
                if not s.get("err"):
                    new.append(s)
            if sigs:
                w["last"] = sigs[0]["signature"]
            for s in reversed(new[:MAX_NEW_TX]):   # oldest first
                tx = rpc("getTransaction", [s["signature"], {"maxSupportedTransactionVersion": 0,
                                                             "encoding": "json"}])
                for mint, d in _changes(tx, addr).items():
                    events.append({"wallet": addr, "name": info.get("name", addr[:6]), "mint": mint,
                                   "side": "buy" if d > 0 else "sell", "amount": abs(d),
                                   "sig": s["signature"], "ts": int(s.get("blockTime") or now)})
        except Exception as e:                     # a watcher must never stop the desk
            seen.setdefault(addr, {})["error"] = f"{type(e).__name__}"
    return events
