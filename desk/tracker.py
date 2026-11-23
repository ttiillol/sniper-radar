"""Verification engine: "what if we had bought it?"

Every coin the desk evaluates (bought, rejected or watched) is followed for 4 hours:
its price is sampled about every 5 minutes, then the SAME exit rules as the real desk
(stop -6%, half at +25%, trailing stop, time stop) are replayed on that price path.

The result per coin is a simulated return. Grouped by reject reason, liquidity band and
entry score, it shows which rules protect money and which rules throw away winners.
This is the evidence used to tune the desk - not opinions.
"""
import re
import time

from . import config as C

TRACK_HOURS = 4
SAMPLE_SEC = 300
MAX_TRACKED = 400
MAX_OUTCOMES = 3000
FRICTION = 2 * C.FEE_PCT + 0.01          # fees both ways + ~1% slippage


def reason_key(text):
    t = re.sub(r"^\[\w+\]\s*", "", text or "")
    return re.sub(r"\$?\d[\d,\.]*", "N", t.split("（")[0]).replace("上位N", "上位10")[:32]


def liq_band(liq):
    for lo, label in ((30_000, "$30k以上"), (10_000, "$10k〜30k"), (5_000, "$5k〜10k"), (0, "$5k未満")):
        if (liq or 0) >= lo:
            return label
    return "不明"


def start(state, mint, symbol, price, liq, decision, reasons=None, score=None, now=None):
    """Begin following a coin right after the desk decided on it."""
    if not price:
        return
    tr = state.setdefault("tracking", {})
    if mint in tr or len(tr) >= MAX_TRACKED:
        return
    now = int(now or time.time())
    tr[mint] = {"t0": now, "p0": price, "symbol": symbol, "decision": decision, "score": score,
                "liq": liq, "reasons": [reason_key(r) for r in (reasons or [])][:4],
                "path": [[now, price]], "next": now + SAMPLE_SEC}


def due(state, now=None):
    now = now or time.time()
    return [m for m, t in state.get("tracking", {}).items() if t["next"] <= now]


def update(state, prices, now=None):
    """prices: {mint: usd price}. Adds samples; finishes coins older than TRACK_HOURS."""
    now = int(now or time.time())
    tr = state.get("tracking", {})
    for m, p in prices.items():
        t = tr.get(m)
        if t is None:
            continue
        if p:
            t["path"].append([now, p])
            t["path"] = t["path"][-60:]
        t["next"] = now + SAMPLE_SEC
    for m in list(tr):
        if now - tr[m]["t0"] >= TRACK_HOURS * 3600:
            finish(state, m)


def simulate(path, entry=None):
    """Replay the desk's exit rules on a price path. Returns net return (fraction)."""
    from .agents import exit_decision
    if not path:
        return None
    entry = entry or path[0][1]
    pos = {"entry": entry, "peak": entry, "opened": path[0][0], "half_sold": False}
    realized, left = 0.0, 1.0
    for ts, p in path[1:]:
        frac, _ = exit_decision(pos, p, None, ts)
        if frac:
            sold = left * frac if frac < 1 else left
            realized += sold * (p / entry - 1)
            left -= sold
            if left <= 1e-9:
                break
    if left > 1e-9:                      # still holding at the end: mark to last price
        realized += left * (path[-1][1] / entry - 1)
    return realized - FRICTION


def finish(state, mint):
    t = state["tracking"].pop(mint, None)
    if not t or len(t["path"]) < 3:
        return
    ps = [p for _, p in t["path"]]
    out = {"mint": mint, "symbol": t["symbol"], "t0": t["t0"], "decision": t["decision"],
           "score": t["score"], "liq": t["liq"], "reasons": t["reasons"],
           "sim": round(simulate(t["path"]), 4),
           "max_up": round(max(ps) / ps[0] - 1, 4), "max_down": round(min(ps) / ps[0] - 1, 4),
           "end": round(ps[-1] / ps[0] - 1, 4), "samples": len(ps)}
    outs = state.setdefault("outcomes", [])
    outs.append(out)
    del outs[:-MAX_OUTCOMES]


def summarize(outcomes):
    """Groups -> {n, win_rate, avg} so the page (and tuning) can compare rules."""
    def agg(rows):
        if not rows:
            return None
        sims = [r["sim"] for r in rows]
        return {"n": len(rows), "win": round(sum(1 for x in sims if x > 0) / len(rows), 3),
                "avg": round(sum(sims) / len(rows), 4),
                "big": sum(1 for r in rows if r["max_up"] >= 1.0)}      # doubled at some point
    groups = {"decision": {}, "reason": {}, "liq": {}, "score": {}}
    for r in outcomes:
        groups["decision"].setdefault(r["decision"], []).append(r)
        if r["decision"] == "REJECT" and r["reasons"]:
            groups["reason"].setdefault(r["reasons"][0], []).append(r)
        groups["liq"].setdefault(liq_band(r["liq"]), []).append(r)
        if r["score"] is not None:
            b = "60以上" if r["score"] >= 60 else "40〜59" if r["score"] >= 40 else "40未満"
            groups["score"].setdefault(b, []).append(r)
    return {g: {k: agg(v) for k, v in d.items()} for g, d in groups.items()}
