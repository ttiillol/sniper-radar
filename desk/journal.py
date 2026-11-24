"""Trade journal: per-trade results in R multiples (idea from the IBKR + Claude bot tutorial).

1R = the money we planned to risk on a trade = amount bought x stop-loss distance (now 30%).
A trade that hits the stop is about -1R; a trade that makes 3x the planned risk is +3R.
Built from the SQLite trades table, so trades made before this file existed are included.
"""
import json
import re
import sqlite3
import time

from . import config as C
from . import portfolio as P

RISK_PCT = abs(C.EXIT_PROFILE["sl"])          # 0.30 -> 1R = 30% of the amount bought


def _rows():
    try:
        con = sqlite3.connect(C.DB_FILE, timeout=10)
        try:
            return con.execute("select ts, mint, symbol, side, usd, pnl, reason from trades order by ts, rowid").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def trades(state):
    """Closed trades (oldest first). A trade = one BUY and all SELLs of that coin until it was fully sold."""
    open_ = {}
    closed = []
    since = state.get("book_start", 0)
    for ts, mint, sym, side, usd, pnl, reason in _rows():
        if ts < since:
            continue                                 # trades of an archived paper book
        if side == "BUY":
            if mint in open_:                       # re-bought: the previous one must have closed
                closed.append(open_.pop(mint))
            m = re.search(r"損切り-(\d+(?:\.\d+)?)%", reason or "")
            open_[mint] = {"mint": mint, "symbol": sym, "opened": ts, "closed": None, "cost": usd,
                           "pnl": 0.0, "sells": 0, "exit": "", "risk_pct": float(m.group(1)) / 100 if m else None}
        elif mint in open_:
            t = open_[mint]
            t["pnl"] += pnl or 0.0
            t["sells"] += 1
            t["closed"] = ts
            t["exit"] = reason or ""
    for mint, t in open_.items():
        if mint not in state.get("positions", {}) and t["sells"]:
            closed.append(t)                        # fully sold
    closed.sort(key=lambda t: t["closed"] or 0)
    for t in closed:
        risk = max(t["cost"] * (t.pop("risk_pct", None) or RISK_PCT), 1e-9)
        t["R"] = round(t["pnl"] / risk, 2)
        t["ret"] = round(t["pnl"] / t["cost"], 4) if t["cost"] else 0.0
        t["hold_min"] = round(((t["closed"] or t["opened"]) - t["opened"]) / 60)
        t["pnl"] = round(t["pnl"], 2)
    return closed


def stats(tr, start=None):
    """Win rate, average R (= expectancy per trade), profit factor, max drawdown, equity curve."""
    n = len(tr)
    eq, peak, mdd, curve = start or C.START_CASH, start or C.START_CASH, 0.0, []
    for t in tr:
        eq += t["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        curve.append([t["closed"], round(eq, 2)])
    if not n:
        return {"n": 0, "curve": []}
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    return {
        "n": n, "wins": len(wins), "win": round(len(wins) / n, 3),
        "avgR": round(sum(t["R"] for t in tr) / n, 2), "totalR": round(sum(t["R"] for t in tr), 2),
        "avgWinR": round(sum(t["R"] for t in wins) / len(wins), 2) if wins else None,
        "avgLossR": round(sum(t["R"] for t in losses) / len(losses), 2) if losses else None,
        "pf": round(gw / gl, 2) if gl > 0 else None, "pnl": round(gw - gl, 2),
        "mdd": round(mdd, 2), "mdd_pct": round(mdd / max(peak, 1e-9) * 100, 1),
        "best": max(t["R"] for t in tr), "worst": min(t["R"] for t in tr),
        "avg_ret": round(sum(t["ret"] for t in tr) / n, 4),
        "hold": round(sum(t["hold_min"] for t in tr) / n), "curve": curve[-200:],
    }


def backtest_vs_live(state, tr):
    """The tutorial's lesson: backtests looked great, live was 'meh'. Show both side by side.
    verification = what the SAME exit rules would have made on the coins we actually bought
    (price samples every 5 min, fees/slippage estimated) vs what the paper desk really made."""
    def agg(vals):
        return {"n": len(vals), "win": round(sum(1 for v in vals if v > 0) / len(vals), 3),
                "avg": round(sum(vals) / len(vals), 4)} if vals else None
    outs = state.get("outcomes", [])
    bought = [o["sim"] for o in outs if o.get("decision") == "BUY"]
    allc = [o["sim"] for o in outs]
    return {"sim_bought": agg(bought), "sim_all": agg(allc), "live": agg([t["ret"] for t in tr])}


def day_report(state, day):
    """Text for the end-of-day notification (Hawaii day)."""
    tr = [t for t in trades(state) if t["closed"] and P.hst_day(t["closed"]) == day]
    st = stats(tr)
    acts = {}
    try:
        for line in C.DECISIONS_LOG.read_text().splitlines()[-8000:]:
            d = json.loads(line)
            if P.hst_day(d["ts"]) == day and d["ts"] >= state.get("book_start", 0):
                acts[d["action"]] = acts.get(d["action"], 0) + 1
    except (FileNotFoundError, ValueError, KeyError):
        pass
    lines = [f"{day}（ハワイ時間）の練習売買まとめ"]
    if st["n"]:
        lines.append(f"終わったトレード {st['n']}件 ・ 勝ち {st['wins']} ・ 勝率 {st['win'] * 100:.0f}%")
        lines.append(f"損益 {st['pnl']:+.2f}ドル ・ 合計 {st['totalR']:+.2f}R ・ 1回平均 {st['avgR']:+.2f}R")
        b = max(tr, key=lambda t: t["R"])
        w = min(tr, key=lambda t: t["R"])
        lines.append(f"いちばん良い {b['symbol']} {b['R']:+.2f}R ／ 悪い {w['symbol']} {w['R']:+.2f}R")
    else:
        lines.append("終わったトレードはありません")
    lines.append(f"審査の判断: 買い {acts.get('BUY', 0)} ・ 見送り {acts.get('REJECT', 0)} ・ 様子見 {acts.get('WATCH', 0)}"
                 f" ・ エラー {acts.get('ERROR', 0)}")
    lines.append(f"残高 ${state['cash']:.2f} ・ 保有中 {len(state['positions'])}件 ・ 累計 {state['realized']:+.2f}ドル")
    return "\n".join(lines)


def maybe_day_report(state, send, now=None):
    """Call often. Sends yesterday's report once, right after the Hawaii date changes
    (or at the first start after it changed). Returns True when a report was sent."""
    today = P.hst_day(now or time.time())
    last = state.get("report_day")
    if last is None:
        state["report_day"] = today
        return False
    if last == today:
        return False
    state["report_day"] = today
    send(f"1日のまとめ {last}", day_report(state, last))
    return True
