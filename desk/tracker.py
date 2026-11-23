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

# Exit profiles compared on every tracked coin (same price path, different selling rules).
#  A: the desk's current rules (design doc: stop -6%)
#  B: common terminal/bot advice: stop -35%, sell 50% at 2x, 25% at 5x, trail the rest 25% from the peak
#  C: open-source pumpbot style: stop -35%, thirds at +50% / +150% / +400%, trail 12% after +25%
PROFILES = {
    "A_今のルール(-6%)": {"sl": -0.06, "tps": [(0.25, 0.5)], "arm": 0.15, "trail": 0.12, "max_min": 240},
    "B_業界標準(-35%,2倍で半分)": {"sl": -0.35, "tps": [(1.0, 0.5), (4.0, 0.25)], "arm": 1.0, "trail": 0.25,
                                 "max_min": 240},
    "C_段階利確(+50/150/400%)": {"sl": -0.35, "tps": [(0.5, 1 / 3), (1.5, 1 / 3), (4.0, 1 / 3)], "arm": 0.25,
                                "trail": 0.12, "max_min": 240},
}
FEATURES = ("holders", "sniper_hold_pct", "bundle_hold_pct", "insider_pct", "top3_pct", "top10_pct",
            "creator_prev", "creator_dead", "dev_pct", "liq")


def reason_key(text):
    t = re.sub(r"^\[\w+\]\s*", "", text or "")
    return re.sub(r"\$?\d[\d,\.]*", "N", t.split("（")[0]).replace("上位N", "上位10")[:32]


def liq_band(liq):
    for lo, label in ((30_000, "$30k以上"), (10_000, "$10k〜30k"), (5_000, "$5k〜10k"), (0, "$5k未満")):
        if (liq or 0) >= lo:
            return label
    return "不明"


def start(state, mint, symbol, price, liq, decision, reasons=None, score=None, now=None, facts=None):
    """Begin following a coin right after the desk decided on it."""
    if not price:
        return
    tr = state.setdefault("tracking", {})
    if mint in tr or len(tr) >= MAX_TRACKED:
        return
    now = int(now or time.time())
    tr[mint] = {"t0": now, "p0": price, "symbol": symbol, "decision": decision, "score": score,
                "liq": liq, "reasons": [reason_key(r) for r in (reasons or [])][:4],
                "path": [[now, price]], "next": now + SAMPLE_SEC,
                "facts": {k: (facts or {}).get(k) for k in FEATURES if (facts or {}).get(k) is not None}}


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


def simulate_profile(path, prof):
    """Replay one exit profile on a price path. Returns net return (fraction of the stake)."""
    if not path or len(path) < 2:
        return None
    entry, t0 = path[0][1], path[0][0]
    left, realized, peak, done = 1.0, 0.0, entry, set()
    for ts, p in path[1:]:
        g = p / entry - 1
        peak = max(peak, p)
        if g <= prof["sl"]:
            realized += left * g; left = 0; break               # honest: filled at the real (gapped) price
        for i, (lvl, frac) in enumerate(prof["tps"]):
            if i not in done and g >= lvl:
                done.add(i)
                sold = min(frac, left)
                realized += sold * g; left -= sold
        if left <= 1e-9:
            break
        if peak / entry - 1 >= prof["arm"] and p <= peak * (1 - prof["trail"]):
            realized += left * g; left = 0; break
        if (ts - t0) / 60 >= prof["max_min"]:
            realized += left * g; left = 0; break
    if left > 1e-9:
        realized += left * (path[-1][1] / entry - 1)
    return realized - FRICTION


def simulate(path, entry=None):
    """Desk's own exit rules (profile A, via the real exit_decision for exactness)."""
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
    if left > 1e-9:
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
           "end": round(ps[-1] / ps[0] - 1, 4), "samples": len(ps),
           "sims": {k: round(simulate_profile(t["path"], v), 4) for k, v in PROFILES.items()},
           "facts": t.get("facts", {})}
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
    groups = {"decision": {}, "reason": {}, "liq": {}, "score": {}, "holders": {}, "snipers": {},
              "creator": {}}
    for r in outcomes:
        groups["decision"].setdefault(r["decision"], []).append(r)
        if r["decision"] == "REJECT" and r["reasons"]:
            groups["reason"].setdefault(r["reasons"][0], []).append(r)
        groups["liq"].setdefault(liq_band(r["liq"]), []).append(r)
        f = r.get("facts") or {}
        if f.get("holders") is not None:
            h = f["holders"]
            groups["holders"].setdefault("75人未満" if h < 75 else "75〜300人" if h < 300 else "300人以上", []).append(r)
        if f.get("sniper_hold_pct") is not None:
            v = f["sniper_hold_pct"]
            groups["snipers"].setdefault("5%未満" if v < 5 else "5〜20%" if v < 20 else "20%以上", []).append(r)
        if f.get("creator_prev") is not None:
            serial = f["creator_prev"] >= 3 and f.get("creator_dead", 0) / max(f["creator_prev"], 1) >= 0.67
            groups["creator"].setdefault("常習犯" if serial else "それ以外", []).append(r)
        if r["score"] is not None:
            b = "60以上" if r["score"] >= 60 else "40〜59" if r["score"] >= 40 else "40未満"
            groups["score"].setdefault(b, []).append(r)
    out = {g: {k: agg(v) for k, v in d.items()} for g, d in groups.items()}
    # exit profiles: same coins, different selling rules (only coins the desk would consider buying
    # are meaningful, but all coins are shown too so the sample is large early on)
    prof = {}
    for name in PROFILES:
        vals = [r["sims"][name] for r in outcomes if r.get("sims", {}).get(name) is not None]
        if vals:
            prof[name] = {"n": len(vals), "win": round(sum(1 for x in vals if x > 0) / len(vals), 3),
                          "avg": round(sum(vals) / len(vals), 4)}
    out["profiles"] = prof
    return out
