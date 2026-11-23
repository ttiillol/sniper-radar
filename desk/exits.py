"""One exit engine for both the live (paper) desk and the verification engine.

A profile is a dict:
  sl         stop-loss before the first take-profit (e.g. -0.30)
  tps        [(gain, fraction_of_initial), ...] take-profit ladder
  be         after the first take-profit, never let the rest fall below entry*(1+be) (None = off)
  arm/trail  trailing stop: once the peak gain >= arm, sell the rest if price <= peak*(1-trail)
  arm0/trail0  optional early trailing stop used only BEFORE the first take-profit (locks in small wins)
  arm1/trail1  optional middle trailing stop used between the first and the last take-profit
  stale_min  before the first take-profit, exit if still below entry after this many minutes (None = off)
  max_min    hard time limit for the whole position
State (kept on the position): entry, peak, opened, sold (fraction of initial already sold), tp_done (list)
"""


def step(st, price, now, prof):
    """Advance one price tick. Returns (fraction_of_INITIAL_to_sell_now, reason or None)."""
    entry = st["entry"]
    st["peak"] = max(st.get("peak", entry), price)
    st.setdefault("sold", 0.0)
    st.setdefault("tp_done", [])
    left = 1.0 - st["sold"]
    if left <= 1e-9:
        return 0.0, None
    g = price / entry - 1
    pg = st["peak"] / entry - 1
    age_min = (now - st["opened"]) / 60
    first_tp_done = bool(st["tp_done"])

    def all_out(reason):
        st["sold"] = 1.0
        return left, reason

    # 1) protection before the first take-profit
    if not first_tp_done or prof.get("be") is None:
        if g <= prof["sl"]:
            return all_out(f"損切り（{g*100:+.1f}%）")
    if not first_tp_done and prof.get("stale_min") and age_min >= prof["stale_min"] and g < 0:
        return all_out(f"{int(prof['stale_min'])}分たっても伸びないので撤退（{g*100:+.1f}%）")

    # 2) take-profit ladder (fractions of the INITIAL position)
    sell, hit = 0.0, []
    for i, (lvl, frac) in enumerate(prof["tps"]):
        if i not in st["tp_done"] and g >= lvl:
            st["tp_done"].append(i)
            take = min(frac, left - sell)
            if take > 1e-9:
                sell += take
                hit.append(f"+{lvl*100:.0f}%")
    if sell:
        st["sold"] += sell
        rest = 1.0 - st["sold"]
        msg = f"利益確定 {'・'.join(hit)}で{sell*100:.0f}%売却（{g*100:+.1f}%）"
        if prof.get("be") is not None and rest > 1e-9:
            msg += f" → 残りの損切りを買値+{prof['be']*100:.0f}%に引き上げ"
        return sell, msg

    # 3) after the first take-profit: break-even floor and/or trailing stop
    floor = 0.0
    all_tp_done = len(st["tp_done"]) >= len(prof["tps"])
    if first_tp_done and prof.get("be") is not None:
        floor = entry * (1 + prof["be"])
    if not first_tp_done and prof.get("arm0") is not None and pg >= prof["arm0"]:
        floor = max(floor, st["peak"] * (1 - prof["trail0"]))
    if first_tp_done and not all_tp_done and prof.get("arm1") is not None and pg >= prof["arm1"]:
        floor = max(floor, st["peak"] * (1 - prof["trail1"]))
    if pg >= prof["arm"] and (all_tp_done or prof.get("arm1") is None):
        floor = max(floor, st["peak"] * (1 - prof["trail"]))
    if floor and price <= floor:
        why = "トレーリングストップ" if floor > entry * (1 + (prof.get("be") or 0)) + 1e-12 \
            else "建値ストップ（この取引は負けなし）"
        return all_out(f"{why}（高値+{pg*100:.0f}%から、損益{g*100:+.1f}%）")

    # 4) time limit
    if age_min >= prof["max_min"]:
        return all_out(f"保有時間の上限（{prof['max_min']//60}時間、損益{g*100:+.1f}%）")
    return 0.0, None


def replay(path, prof, friction=0.0):
    """Net return (fraction of stake) of one profile on a price path [[ts, price], ...]."""
    if not path or len(path) < 2:
        return None
    st = {"entry": path[0][1], "peak": path[0][1], "opened": path[0][0]}
    realized = 0.0
    for ts, p in path[1:]:
        frac, _ = step(st, p, ts, prof)
        if frac:
            realized += frac * (p / st["entry"] - 1)
        if st["sold"] >= 1 - 1e-9:
            break
    left = 1.0 - st.get("sold", 0.0)
    if left > 1e-9:
        realized += left * (path[-1][1] / st["entry"] - 1)
    return realized - friction
