"""Offline test of the whole run loop: fake PumpPortal + fake RPC failures.
Checks: retry of transient failures, failure reasons, even sampling, incremental save, hard stop."""
import asyncio
import json
import random
import shutil
import tempfile
from pathlib import Path

import scanner

random.seed(3)
tmp = Path(tempfile.mkdtemp())
scanner.DATA = tmp
scanner.WALLETS_FILE, scanner.TOKENS_FILE, scanner.META_FILE = tmp / "w.json", tmp / "t.json", tmp / "m.json"
scanner.WAIT_BEFORE_CHECK = 0.3
scanner.RETRY_DELAY = 0.3
BOT = "BOTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"
attempts = {}


class FakeWS:
    def __init__(self):
        self.n = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send(self, m): pass
    async def recv(self):
        await asyncio.sleep(0.02)
        self.n += 1
        return json.dumps({"txType": "create", "mint": f"MINT{self.n}pump", "symbol": f"C{self.n}",
                           "traderPublicKey": "CREATOR"})


def fake_analyze(mint, creator):
    attempts[mint] = attempts.get(mint, 0) + 1
    k = int(mint[4:-4])
    if k % 5 == 0 and attempts[mint] == 1:
        raise scanner.RpcError("rate_limited")          # transient: should succeed on retry
    if k % 7 == 0:
        raise scanner.RpcError("too_busy")              # permanent: no retry
    return {"create_slot": 1, "early": [{"wallet": BOT, "gap": 1}, {"wallet": f"H{k}", "gap": 2}]}


scanner.websockets.connect = lambda *a, **k: FakeWS()
scanner.analyze_token = fake_analyze
asyncio.run(scanner.run(minutes=0.05, max_tokens=40))   # 3 seconds of listening

meta = json.loads(scanner.META_FILE.read_text())
st = meta["last_stats"]
w = json.loads(scanner.WALLETS_FILE.read_text())
print(json.dumps(st))
assert st["queued"] <= 40, "per-run cap respected"
assert st["seen"] > st["queued"], "sampling spreads quota instead of grabbing the first N"
assert st["retried"] > 0 and st["reasons"].get("too_busy", 0) > 0
assert "rate_limited" not in st["reasons"], "transient failures recovered on retry"
assert st["checked"] + st["skipped"] == st["queued"], "every queued coin accounted for"
assert w[BOT]["count"] == st["checked"], "bot recorded on every checked coin"
assert meta["tokens_total"] == st["checked"] and meta["runs"] == 1
assert "credits" in meta and "RPC_URL" not in json.dumps(meta), "never store the key"
print("run loop OK")
shutil.rmtree(tmp)
