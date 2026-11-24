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
    return {"book": C.BOOK, "book_start": int(time.time()), "start_cash": C.START_CASH,
            "cash": C.START_CASH, "realized": 0.0, "positions": {}, "daily": {}, "halted": None,
            "last_heartbeat": None, "gaps": [], "candidates": {}, "seen_symbols": {},
            "credits": {}, "stats": {"evaluated": 0, "rejected": {}, "bought": 0, "sold": 0}}


def load_state():
    try:
        s = json.loads(C.STATE_FILE.read_text())
    except Exception:
        return new_state()
    if s.get("book") != C.BOOK:
        # a new paper book was started: keep the old one as an archive file, begin from scratch
        try:
            old = C.STATE_FILE.with_name(f"state-archive-{s.get('book') or 'v1'}-{int(time.time())}.json")
            old.write_text(json.dumps(s, separators=(",", ":")))
        except Exception:
            pass
        fresh = new_state()
        fresh["credits"] = s.get("credits") or {}          # RPC usage counter is per month, keep it
        save_state(fresh)
        log_decision("book", "", "", "RESUME", f"新しい練習帳簿を開始: ${C.START_CASH:,.0f}（前の帳簿は保管）")
        return fresh
    base = new_state()
    base.update(s)
    return base


def save_state(s):
    C.DESK.mkdir(parents=True, exist_ok=True)
    tmp = C.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, separators=(",", ":")))
    tmp.replace(C.STATE_FILE)


# ---------------------------------------------------------------- logging
_db = None          # kept for test compatibility; connections are now per call (thread-safe)
_DB_LOCK = __import__("threading").Lock()


def _exec(sql, params):
    C.DESK.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        con = sqlite3.connect(C.DB_FILE, timeout=10)
        try:
            con.execute("create table if not exists decisions(ts integer, kind text, mint text, "
                        "symbol text, action text, reason text, data text)")
            con.execute("create table if not exists trades(ts integer, mint text, symbol text, side text, "
                        "usd real, price real, slip real, pnl real, reason text)")
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()


def log_decision(kind, mint, symbol, action, reason, **data):
    row = {"ts": int(time.time()), "kind": kind, "mint": mint, "symbol": symbol,
           "action": action, "reason": reason, **data}
    C.DESK.mkdir(parents=True, exist_ok=True)
    with open(C.DECISIONS_LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    _exec("insert into decisions values(?,?,?,?,?,?,?)",
          (row["ts"], kind, mint, symbol, action, reason, json.dumps(data, ensure_ascii=False)))
    return row


def log_trade(mint, symbol, side, usd, price, slip, pnl, reason):
    _exec("insert into trades values(?,?,?,?,?,?,?,?,?)",
          (int(time.time()), mint, symbol, side, usd, price, slip, pnl, reason))


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
    return sum(p["cost"] * p.get("gap", C.WORST_CASE_GAP) for p in s["positions"].values())


def paper_buy(s, mint, symbol, usd, price, liq, reason, ts=None, tier="normal", gap=None, sl=None):
    slip = slippage(usd, liq)
    fill = price * (1 + slip) * (1 + C.FEE_PCT)
    s["cash"] -= usd
    s["positions"][mint] = {"symbol": symbol, "cost": usd, "qty": usd / fill, "entry": fill,
                            "peak": fill, "opened": int(ts or time.time()), "half_sold": False,
                            "liq": liq, "tier": tier, "gap": gap if gap is not None else C.WORST_CASE_GAP}
    if sl:
        s["positions"][mint]["sl"] = sl
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
