"""Paper portfolio: state that survives restarts, paper fills, and the decision log (JSONL + SQLite)."""
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config as C


class LiveTradingDisabled(RuntimeError):
    pass


def execute_live_order(*_a, **_k):
    """Real orders are intentionally impossible until the owner explicitly approves
    a dedicated wallet + a key WITHOUT withdrawal rights. This stub always refuses."""
    raise LiveTradingDisabled("Live trading is disabled. Paper mode only.")


def hst_day(ts=None):
    return datetime.fromtimestamp(ts or time.time(), ZoneInfo(C.TZ)).strftime("%Y-%m-%d")


def slippage(size_usd, liquidity_usd):
    """x*y=k price impact for buying `size_usd` into a pool whose quote side is ~liq/2."""
    reserve = max(liquidity_usd / 2.0, 1e-9)
    return size_usd / (reserve + size_usd)


def new_state():
    return {"cash": C.START_CASH, "realized": 0.0, "positions": {}, "daily": {}, "halted": None,
            "last_heartbeat": None, "gaps": [], "candidates": {}, "seen_symbols": {},
            "credits": {}, "stats": {"evaluated": 0, "rejected": {}, "bought": 0, "sold": 0}}


def load_state():
    try:
        s = json.loads(C.STATE_FILE.read_text())
        base = new_state()
        base.update(s)
        return base
    except Exception:
        return new_state()


def save_state(s):
    C.DESK.mkdir(parents=True, exist_ok=True)
    tmp = C.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, separators=(",", ":")))
    tmp.replace(C.STATE_FILE)


# ---------------------------------------------------------------- logging
_db = None


def _conn():
    global _db
    if _db is None:
        C.DESK.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(C.DB_FILE)
        _db.execute("create table if not exists decisions(ts integer, kind text, mint text, "
                    "symbol text, action text, reason text, data text)")
        _db.execute("create table if not exists trades(ts integer, mint text, symbol text, side text, "
                    "usd real, price real, slip real, pnl real, reason text)")
    return _db


def log_decision(kind, mint, symbol, action, reason, **data):
    row = {"ts": int(time.time()), "kind": kind, "mint": mint, "symbol": symbol,
           "action": action, "reason": reason, **data}
    C.DESK.mkdir(parents=True, exist_ok=True)
    with open(C.DECISIONS_LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    _conn().execute("insert into decisions values(?,?,?,?,?,?,?)",
                    (row["ts"], kind, mint, symbol, action, reason, json.dumps(data, ensure_ascii=False)))
    _conn().commit()
    return row


def log_trade(mint, symbol, side, usd, price, slip, pnl, reason):
    _conn().execute("insert into trades values(?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), mint, symbol, side, usd, price, slip, pnl, reason))
    _conn().commit()


def trim_log(max_lines=8000):
    try:
        lines = C.DECISIONS_LOG.read_text().splitlines()
        if len(lines) > max_lines:
            C.DECISIONS_LOG.write_text("\n".join(lines[-max_lines:]) + "\n")
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- money
def day_pnl(s, ts=None):
    return s["daily"].get(hst_day(ts), 0.0)


def worst_case_open(s):
    """Loss if every open position gaps down WORST_CASE_GAP (stop-loss can't save us)."""
    return sum(p["cost"] * C.WORST_CASE_GAP for p in s["positions"].values())


def paper_buy(s, mint, symbol, usd, price, liq, reason, ts=None):
    slip = slippage(usd, liq)
    fill = price * (1 + slip) * (1 + C.FEE_PCT)
    s["cash"] -= usd
    s["positions"][mint] = {"symbol": symbol, "cost": usd, "qty": usd / fill, "entry": fill,
                            "peak": fill, "opened": int(ts or time.time()), "half_sold": False,
                            "liq": liq}
    s["stats"]["bought"] += 1
    log_trade(mint, symbol, "BUY", usd, fill, slip, 0.0, reason)
    return fill, slip


def paper_sell(s, mint, fraction, price, reason, ts=None):
    p = s["positions"][mint]
    qty = p["qty"] * fraction
    gross = qty * price
    slip = slippage(gross, p.get("liq") or 1)
    proceeds = gross * (1 - slip) * (1 - C.FEE_PCT)
    cost_part = p["cost"] * fraction
    pnl = proceeds - cost_part
    s["cash"] += proceeds
    s["realized"] += pnl
    d = hst_day(ts)
    s["daily"][d] = s["daily"].get(d, 0.0) + pnl
    if fraction >= 0.999:
        del s["positions"][mint]
    else:
        p["qty"] -= qty
        p["cost"] -= cost_part
    s["stats"]["sold"] += 1
    log_trade(mint, p["symbol"], "SELL", proceeds, price, slip, pnl, reason)
    return pnl
