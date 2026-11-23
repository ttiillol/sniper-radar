"""Start-up self check: call every data source once with a REAL coin that is trending right now
and report the result to the phone and the dashboard. Runs at start and every 6 hours.

Nothing here can stop the desk; a failing source only shows up as "NG" in the report.
"""
import time

from . import flow as F
from . import sources as S


def run():
    res, coin = {}, None
    try:
        tr = F.trending_mints(limit=10)
        res["GeckoTerminal注目"] = (bool(tr), f"{len(tr)}件")
        coin = tr[0] if tr else None
    except Exception as e:
        res["GeckoTerminal注目"] = (False, type(e).__name__)
    mint = coin["mint"] if coin else None
    pool = None
    if mint:
        try:
            pair = S.dex_pairs([mint]).get(mint)
            pool = (pair or {}).get("pairAddress")
            res["DexScreener"] = (bool(pair), f"{coin['symbol']} 流動性 ${((pair or {}).get('liquidity') or {}).get('usd') or 0:,.0f}")
        except Exception as e:
            res["DexScreener"] = (False, type(e).__name__)
        try:
            t = F.pool_trades(pool)
            res["GeckoTerminal取引"] = (bool(t), f"{len(t or [])}件")
        except Exception as e:
            res["GeckoTerminal取引"] = (False, type(e).__name__)
        try:
            sc = F.sell_check(mint, 10)
            res["Jupiter売却テスト"] = (sc["ok"] is True, sc["reason"])
        except Exception as e:
            res["Jupiter売却テスト"] = (False, type(e).__name__)
        try:
            rc = S.rugcheck(mint)
            res["RugCheck"] = (bool(rc), "応答あり" if rc else "応答なし")
        except Exception as e:
            res["RugCheck"] = (False, type(e).__name__)
    return {"ts": int(time.time()), "coin": coin["symbol"] if coin else None,
            "results": {k: {"ok": ok, "note": note} for k, (ok, note) in res.items()}}


def text(r):
    lines = [f"試したコイン: {r['coin'] or '（取得できず）'}"]
    for k, v in r["results"].items():
        lines.append(f"{'OK' if v['ok'] else 'NG'}  {k} — {v['note']}")
    return "\n".join(lines)
