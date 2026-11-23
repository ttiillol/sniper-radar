"""Offline test: fake blockchain data -> checks detection logic and builds a sample page."""
import json
import random
import shutil
import time
from pathlib import Path

import scanner

random.seed(7)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
addr = lambda: "".join(random.choice(B58) for _ in range(44))

BOTS = [addr() for _ in range(6)]          # always snipe within 1-3 blocks
BUNDLERS = [addr() for _ in range(3)]      # buy in block 0 together with creator
TXS = {}


def fake_chain(mint, creator):
    slot0 = random.randint(300_000_000, 310_000_000)
    rows = [("create", creator, 0, 1000.0)]
    for b in random.sample(BUNDLERS, random.randint(0, 2)):
        rows.append(("b", b, 0, 500.0))
    for b in random.sample(BOTS, random.randint(1, 4)):
        rows.append(("s", b, random.randint(1, 3), 200.0))
    rows.append(("s", addr(), 2, 50.0))     # random early human
    rows.append(("x", addr(), 2, -10.0))    # early SELL -> must be ignored
    for _ in range(15):
        rows.append(("late", addr(), random.randint(5, 200), 30.0))
    rows.sort(key=lambda r: r[2])
    sigs = []
    for kind, w, gap, delta in rows:
        sig = addr() + addr()
        TXS[sig] = (w, delta, mint)
        sigs.append({"signature": sig, "slot": slot0 + gap, "err": None})
    return list(reversed(sigs))  # RPC returns newest first


CHAINS = {}


def fake_rpc(method, params):
    if method == "getSignaturesForAddress":
        return CHAINS[params[0]]
    if method == "getTransaction":
        w, delta, mint = TXS[params[0]]
        pre = 0.0 if delta > 0 else 100.0
        return {"transaction": {"message": {"accountKeys": [w, addr()]}},
                "meta": {"preTokenBalances": [{"mint": mint, "owner": w, "uiTokenAmount": {"uiAmount": pre}}],
                         "postTokenBalances": [{"mint": mint, "owner": w, "uiTokenAmount": {"uiAmount": pre + delta}}]}}


# use a temp data dir
tmp = Path("/tmp/sr_test")
shutil.rmtree(tmp, ignore_errors=True)
(tmp / "data").mkdir(parents=True)
wallets, tokens = {}, []
for i in range(40):
    mint, creator = addr() + "pump", addr()
    CHAINS[mint] = fake_chain(mint, creator)
    res = scanner.analyze_token(mint, creator, rpc_call=fake_rpc)
    # checks
    ws = {e["wallet"] for e in res["early"]}
    assert creator not in ws, "creator must be excluded"
    assert all(e["gap"] <= 3 for e in res["early"]), "late buys must be excluded"
    assert len(ws) == len(res["early"]), "one row per wallet"
    sym = random.choice(["MOON", "CAT", "BONK2", "PEPE9", "ALOHA", "TACO", "GOAT", "DOGE3"])
    scanner.record(wallets, tokens, {"mint": mint, "symbol": sym, "name": sym + " coin", "creator": creator},
                   res, now=time.time() - random.randint(0, 7200))

for b in BOTS:
    assert wallets[b]["count"] >= 3, "bots should be caught repeatedly"
for b in BUNDLERS:
    assert wallets[b]["block0"] == wallets[b]["count"], "bundlers only in block 0"
sells = [w for w, v in wallets.items() if v["count"] and w not in BOTS + BUNDLERS and v["count"] > 1]
assert not sells, "random one-off wallets should appear once"
print("logic OK — bots:", [wallets[b]["count"] for b in BOTS], "bundlers:", [wallets[b]["block0"] for b in BUNDLERS])

(tmp / "data" / "wallets.json").write_text(json.dumps(wallets))
(tmp / "data" / "tokens.json").write_text(json.dumps(tokens))
(tmp / "data" / "meta.json").write_text(json.dumps({"runs": 3, "tokens_total": 40, "last_run": int(time.time()) - 600}))
print("sample bot wallet:", BOTS[0])
