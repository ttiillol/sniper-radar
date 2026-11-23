"""Order-flow agents (ideas from the 12-agent Grok memecoin desk, rebuilt on free data).

WHALE WATCH  - how many big buys (and from how many different wallets) in the last minutes
HOLDER FLOW  - how many different wallets are buying, and buy vs sell pressure in USD
KOL WATCH    - buys from wallets listed in data/kol_wallets.txt (one address per line, optional)
DEV WATCH    - did the coin's creator sell? (hard fail before entry, instant exit while holding)
SELL CHECK   - can the coin actually be sold? (Jupiter quote token -> SOL; no route = honeypot)
TRENDING     - coins currently trending on GeckoTerminal become extra candidates

Data: GeckoTerminal public API (no key, ~30 calls/min) and Jupiter's free quote API.
A quote is only a price question - nothing is signed or sent. If a source is down the
check is skipped (noted), except DEV WATCH which only acts on positive evidence.
"""
import time
from datetime import datetime

from . import config as C
from . import sources as S

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"

_last_call = {"t": 0.0}


def _gt(path):
    """GeckoTerminal allows ~30 calls/min: keep at least 2.1 s between calls."""
    wait = _last_call["t"] + 2.1 - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call["t"] = time.time()
    return S.get_json(GT + path, timeout=12, retries=1)


def _ts(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pool_trades(pool):
    """Last ~300 trades of a pool as [{t, kind, usd, wallet}], newest first. None if unavailable."""
    if not pool:
        return None
    d = _gt(f"/pools/{pool}/trades")
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        return None
    out = []
    for row in d["data"]:
        a = row.get("attributes") or {}
        t = _ts(a.get("block_timestamp"))
        try:
            usd = float(a.get("volume_in_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if t is None or a.get("kind") not in ("buy", "sell"):
            continue
        out.append({"t": t, "kind": a["kind"], "usd": usd, "wallet": a.get("tx_from_address") or ""})
    return out


def load_kols():
    """desk/kol_wallets.txt (kept in GitHub) + data/kol_wallets.txt (local only). One address per line."""
    out = set()
    for f in (C.ROOT / "desk" / "kol_wallets.txt", C.DATA / "kol_wallets.txt"):
        try:
            out |= {x.split("#")[0].strip() for x in f.read_text().splitlines()
                    if x.split("#")[0].strip()}
        except FileNotFoundError:
            pass
    return out


def analyze(trades, now=None, creator=None, kols=None, window_min=None):
    """Numbers only (no opinion): what happened in the last `window_min` minutes."""
    now = now or time.time()
    win = (window_min or C.FLOW_WINDOW_MIN) * 60
    recent = [x for x in trades if now - x["t"] <= win]
    buys = [x for x in recent if x["kind"] == "buy"]
    sells = [x for x in recent if x["kind"] == "sell"]
    bu, su = sum(x["usd"] for x in buys), sum(x["usd"] for x in sells)
    whales = [x for x in buys if x["usd"] >= C.WHALE_BUY_USD]
    # whale burst: the most whale USD bought inside any 2-minute stretch
    burst_usd, burst_w = 0.0, 0
    for w in whales:
        grp = [y for y in whales if 0 <= w["t"] - y["t"] <= 120]
        usd = sum(y["usd"] for y in grp)
        if usd > burst_usd:
            burst_usd, burst_w = usd, len({y["wallet"] for y in grp})
    kols = kols or set()
    return {
        "window_min": round(win / 60), "buys": len(buys), "sells": len(sells),
        "buy_usd": round(bu), "sell_usd": round(su),
        "pressure": round(bu / (bu + su), 3) if bu + su else None,
        "buyers": len({x["wallet"] for x in buys if x["wallet"]}),
        "whale_buys": len(whales), "whale_wallets": len({x["wallet"] for x in whales}),
        "whale_usd": round(sum(x["usd"] for x in whales)),
        "burst_usd": round(burst_usd), "burst_wallets": burst_w,
        "kol_buys": sorted({x["wallet"] for x in buys if x["wallet"] in kols}),
        "dev_sold_usd": round(sum(x["usd"] for x in trades if creator and x["wallet"] == creator
                                  and x["kind"] == "sell")),
    }


def score(f):
    """FLOW agent verdict. ok=False only for a selling creator. Score 0..100, 50 = neutral."""
    if f is None:
        return {"ok": True, "score": 50, "reasons": ["取引の流れ: データなし（判定を飛ばした）"]}
    sc, why = 50, []
    if f["dev_sold_usd"] > 0:
        return {"ok": False, "score": 0, "reasons": [f"作成者が売っている（${f['dev_sold_usd']:,}）"]}
    p = f["pressure"]
    if p is not None:
        if p >= 0.62:
            sc += 15; why.append(f"買いの勢いが強い（買い{p * 100:.0f}%）")
        elif p <= 0.40:
            sc -= 20; why.append(f"売りが優勢（買い{p * 100:.0f}%）")
    if f["burst_wallets"] >= 3:
        sc += 20; why.append(f"大口{f['burst_wallets']}人が2分で${f['burst_usd']:,}買い")
    elif f["whale_wallets"] >= 2:
        sc += 10; why.append(f"大口の買い{f['whale_wallets']}人（${f['whale_usd']:,}）")
    if f["buyers"] >= 30:
        sc += 10; why.append(f"新しい買い手{f['buyers']}人（{f['window_min']}分）")
    elif f["buyers"] < 8:
        sc -= 10; why.append(f"買い手が少ない（{f['buyers']}人）")
    if f["kol_buys"]:
        sc += min(20, 10 * len(f["kol_buys"])); why.append(f"登録した有名人の財布が{len(f['kol_buys'])}つ購入")
    return {"ok": True, "score": max(0, min(100, sc)), "reasons": why}


def sell_check(mint, usd, sol_price=None):
    """SELL SIMULATION: ask Jupiter for a quote selling ~our size of the token for SOL.
    Returns {"ok": True/False/None, "reason": str}. None = could not check (not a fail)."""
    try:
        price = None
        pair = S.dex_pairs([mint]).get(mint)
        price = S.pair_price(pair) if pair else None
        dec = 6                                 # pump.fun tokens use 6 decimals
        if not price:
            return {"ok": None, "reason": "売却テスト: 価格不明で未確認"}
        amount = int(usd / price * 10 ** dec)
        d = S.get_json(f"{JUP_QUOTE}?inputMint={mint}&outputMint={SOL}&amount={amount}&slippageBps=300",
                       timeout=10, retries=1)
        if d is None:
            return {"ok": None, "reason": "売却テスト: Jupiterが応答せず未確認"}
        if not d.get("routePlan") or not int(d.get("outAmount") or 0):
            return {"ok": False, "reason": "売却テスト失敗: 売る経路がない（ハニーポットの疑い）"}
        impact = abs(float(d.get("priceImpactPct") or 0))
        if impact > C.MAX_SELL_IMPACT:
            return {"ok": False, "reason": f"売却テスト失敗: 売ると価格が{impact * 100:.1f}%動く"}
        return {"ok": True, "reason": f"売却テスト合格（価格への影響{impact * 100:.2f}%）"}
    except Exception as e:                       # never let a helper stop the desk
        return {"ok": None, "reason": f"売却テスト: 未確認（{type(e).__name__}）"}


def trending_mints(limit=20):
    """TRENDING / FOMO SCOUT: PumpSwap coins trending on GeckoTerminal right now."""
    d = _gt("/trending_pools?duration=1h")
    out = []
    if not isinstance(d, dict):
        return out
    for i, row in enumerate(d.get("data") or []):
        rel = row.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
        base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        if "pump" in dex and base.startswith("solana_"):
            out.append({"mint": base[len("solana_"):], "rank": i + 1,
                        "symbol": ((row.get("attributes") or {}).get("name") or "").split(" /")[0]})
    return out[:limit]
