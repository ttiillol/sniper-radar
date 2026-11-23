"""The 8 desk roles. Rule-based (no LLM): every decision is explainable and logged.

Order of authority:  Rug (fail = no)  >  Risk (veto)  >  Sniper/Whale/Hype scores  >  Desk Manager.
Hype can never overturn Rug or Risk.
"""
import time

from . import config as C
from . import portfolio as P
from . import sources as S
import scanner


def V(ok, score=0, reasons=None, **data):
    return {"ok": ok, "score": score, "reasons": reasons or [], **data}


# ============================================================ 1. Scanner
class Scanner:
    """Collects freshly graduated (PumpSwap) coins. Graduation time = PumpSwap pair creation."""

    def on_migration(self, state, msg):
        m = msg["mint"]
        if m in state["candidates"] or m in state["positions"]:
            return
        state["candidates"][m] = {"seen": int(time.time()), "grad": None, "symbol": msg.get("symbol", ""),
                                  "status": "waiting", "checks": 0}

    def backfill(self, state):
        """Best effort: recently graduated coins from pump.fun (covers gaps between runs)."""
        url = ("https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=last_trade_timestamp"
               "&order=DESC&includeNsfw=false&complete=true")
        data = S.get_json(url)
        n = 0
        for c in data if isinstance(data, list) else []:
            if c.get("complete") and c.get("mint") and c["mint"] not in state["candidates"]:
                self.on_migration(state, {"mint": c["mint"], "symbol": c.get("symbol", "")})
                n += 1
        return n

    def due(self, state, now=None):
        """Candidates whose graduation is 20..180 min old and not yet decided."""
        now = now or time.time()
        out = []
        for m, c in list(state["candidates"].items()):
            if c["status"] != "waiting":
                if now - c["seen"] > 6 * 3600:
                    del state["candidates"][m]          # forget old decisions
                continue
            ref = c["grad"] or c["seen"]
            age_min = (now - ref) / 60
            if age_min > C.MAX_AGE_AFTER_GRAD_MIN:
                c["status"] = "expired"
            elif age_min >= C.MIN_AGE_AFTER_GRAD_MIN and now >= c.get("next_check", 0):
                out.append(m)
        return out


# ============================================================ 5. Rug
def rug_check(mint, pair, state, radar, now=None, rpc=None, rc=None, gp=None, holders=None,
              minfo=None, early=None):
    """Design-doc rules + radar rules. Missing data = FAIL. Returns V(ok, reasons, facts)."""
    now = now or time.time()
    rpc = rpc or scanner.rpc
    fails, notes, facts = [], [], {}

    # --- graduation age & liquidity (DexScreener)
    if not pair:
        return V(False, reasons=["データ不足: DexScreenerにペアが無い"])
    grad = (pair.get("pairCreatedAt") or 0) / 1000 or None
    facts["age_min"] = round((now - grad) / 60, 1) if grad else None
    if not grad:
        fails.append("データ不足: 卒業時刻が不明")
    elif facts["age_min"] < C.MIN_AGE_AFTER_GRAD_MIN:
        fails.append(f"卒業から{facts['age_min']}分（20分未満）")
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    facts["liq"] = liq
    if liq < C.MIN_LIQ_USD:
        fails.append(f"流動性 ${liq:,.0f}（$30,000未満）")

    # --- impersonation
    sym = ((pair.get("baseToken") or {}).get("symbol") or "").upper().lstrip("$")
    facts["symbol"] = sym
    if sym in C.FAMOUS_TICKERS:
        fails.append(f"有名銘柄 {sym} の名前を使用（偽物の疑い）")
    prev = state["seen_symbols"].get(sym)
    if prev and prev != mint:
        fails.append(f"同じ名前 {sym} の別コインが先に卒業済み（コピー銘柄の疑い）")

    # --- on-chain: authorities, holders
    try:
        mi = minfo or S.mint_info(mint, rpc)
        facts["mint_authority"], facts["freeze_authority"] = mi["mint_authority"], mi["freeze_authority"]
        if mi["mint_authority"]:
            fails.append("mint権限が放棄されていない")
        if mi["freeze_authority"]:
            fails.append("freeze権限が放棄されていない")
        supply = mi["supply"]
        hs = holders if holders is not None else S.top_holders(mint, rpc)
        wallets = [h for h in hs if h["is_wallet"]][:10]
        top10 = sum(h["amount"] for h in wallets) / supply if supply else 1
        facts["top10_pct"] = round(top10 * 100, 1)
        if top10 >= C.MAX_TOP10_PCT:
            fails.append(f"上位10ウォレットが {top10*100:.1f}%（30%以上）")
        bots = radar.get("bots", set())
        bot_amt = sum(h["amount"] for h in hs if h["is_wallet"] and h["owner"] in bots)
        facts["bot_hold_pct"] = round(bot_amt / supply * 100, 2) if supply else 0
        if supply and bot_amt / supply >= C.MAX_BOT_HOLD_PCT:
            fails.append(f"狙撃ボット財布が {bot_amt/supply*100:.1f}% 保有（レーダー）")
    except Exception as e:
        why = getattr(e, "reason", type(e).__name__)
        fails.append(f"データ不足: オンチェーン情報が取れない（{why}）")
        supply, hs = None, []

    # --- RugCheck: creator holding, LP lock, danger flags
    rc = rc if rc is not None else S.rugcheck(mint)
    creator = None
    if not rc:
        fails.append("データ不足: RugCheckが応答しない")
    else:
        creator = rc.get("creator")
        dev_pct = None
        if creator and supply:
            bal = rc.get("creatorBalance")
            if bal is None:
                bal = sum(h["amount"] for h in hs if h["owner"] == creator)
            dev_pct = float(bal) / supply
        facts["dev_pct"] = round(dev_pct * 100, 2) if dev_pct is not None else None
        if dev_pct is None:
            fails.append("データ不足: 開発者の保有率が不明")
        elif dev_pct >= C.MAX_DEV_PCT:
            fails.append(f"開発者の保有 {dev_pct*100:.1f}%（5%以上）")
        locks = [((m.get("lp") or {}).get("lpLockedPct")) for m in rc.get("markets") or []]
        locks = [float(x) for x in locks if x is not None]
        facts["lp_locked_pct"] = max(locks) if locks else None
        if not locks:
            fails.append("データ不足: LPロック率が不明")
        elif max(locks) < C.MIN_LP_LOCKED_PCT * 100:
            fails.append(f"LPロック/バーン {max(locks):.0f}%（95%未満）")
        danger = [r.get("name") for r in rc.get("risks") or [] if r.get("level") == "danger"]
        if danger:
            fails.append("RugCheck危険判定: " + "・".join(danger[:3]))

    # --- GoPlus (second opinion; if it answers and flags danger -> fail)
    gp = gp if gp is not None else S.goplus(mint)
    if gp:
        def flag(k):
            v = gp.get(k)
            return (v or {}).get("status") == "1" if isinstance(v, dict) else str(v) == "1"
        for k, label in (("mintable", "GoPlus: 追加発行できる"), ("freezable", "GoPlus: 凍結できる"),
                         ("closable", "GoPlus: 口座を閉じられる")):
            if flag(k):
                fails.append(label)
    else:
        notes.append("GoPlus応答なし（参考情報のみのため続行）")

    # --- radar: bundle buyers in the creation block
    rt = radar.get("tokens", {}).get(mint)
    try:
        if rt is None and early is None:
            early = scanner.analyze_token(mint, creator, rpc_call=rpc, max_pages=20)["early"]
        if rt is not None:
            b0, sn = rt["block0"], rt["snipers"]
        else:
            b0 = sum(1 for e in early if e["gap"] == 0)
            sn = sum(1 for e in early if e["gap"] > 0)
        facts["block0"], facts["snipers"] = b0, sn
        if b0 > C.MAX_BLOCK0_BUNDLERS:
            fails.append(f"作成と同じ瞬間に{b0}財布がまとめ買い（レーダー）")
    except Exception as e:
        notes.append(f"初期の買い手を確認できず（{getattr(e, 'reason', type(e).__name__)}）")

    return V(not fails, reasons=fails or ["全項目合格"], notes=notes, facts=facts, creator=creator)


# ============================================================ 4. Whale
def whale(facts, cand):
    score, why = 50, []
    t10 = facts.get("top10_pct")
    if t10 is not None:
        if t10 < 15:
            score += 20; why.append(f"保有が分散（上位10 {t10}%）")
        elif t10 > 25:
            score -= 15; why.append(f"上位に集中（上位10 {t10}%）")
        prev = cand.get("prev_top10")
        if prev is not None and t10 - prev > 3:
            score += 10; why.append("大口が買い増し中")
        elif prev is not None and prev - t10 > 3:
            score -= 15; why.append("大口が売り抜け中")
        cand["prev_top10"] = t10
    if (facts.get("bot_hold_pct") or 0) > 1:
        score -= 20; why.append(f"ボット保有 {facts['bot_hold_pct']}%")
    if (facts.get("snipers") or 0) >= 8:
        score -= 10; why.append(f"初期の狙撃 {facts['snipers']}件")
    return V(True, max(0, min(100, score)), why)


# ============================================================ 7. Hype (X substitute)
def hype(pair):
    """X data is paid-only, so this scores on-chain/market attention instead.
    It can lower or raise the entry score but can never overturn Rug or Risk."""
    score, why = 50, []
    tx = (pair.get("txns") or {}).get("h1") or {}
    b, s = tx.get("buys") or 0, tx.get("sells") or 0
    if b + s >= 300:
        score += 15; why.append(f"取引が活発（1時間 {b+s}件）")
    elif b + s < 60:
        score -= 20; why.append(f"取引が少ない（1時間 {b+s}件）")
    socials = (pair.get("info") or {}).get("socials") or []
    if socials:
        score += 10; why.append("SNSリンクあり")
    else:
        score -= 10; why.append("SNSリンクなし")
    if (pair.get("boosts") or {}).get("active"):
        score -= 5; why.append("有料ブースト中（宣伝は割り引いて評価）")
    return V(True, max(0, min(100, score)), why)


# ============================================================ 3. Sniper (entry)
def sniper(pair):
    pc = pair.get("priceChange") or {}
    m5, h1 = pc.get("m5") or 0, pc.get("h1") or 0
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    b, s = tx5.get("buys") or 0, tx5.get("sells") or 0
    score, why = 50, []
    if h1 > 300:
        return V(False, 0, [f"1時間で+{h1:.0f}%（追いかけ買いになる）"])
    if h1 < -40:
        return V(False, 0, [f"1時間で{h1:.0f}%（下落トレンド）"])
    if m5 > 0:
        score += 15; why.append(f"5分 +{m5:.1f}%")
    else:
        score -= 10; why.append(f"5分 {m5:.1f}%")
    ratio = b / max(s, 1)
    if ratio >= 1.3:
        score += 15; why.append(f"買いが優勢（{b}:{s}）")
    elif ratio < 0.8:
        score -= 20; why.append(f"売りが優勢（{b}:{s}）")
    if 0 < h1 <= 100:
        score += 10; why.append(f"1時間 +{h1:.0f}%（過熱していない上昇）")
    return V(score >= 50, max(0, min(100, score)), why)


# ============================================================ 2. Risk (veto)
def risk(state, pair, now=None):
    now = now or time.time()
    if state.get("halted"):
        return V(False, reasons=[f"停止中: {state['halted']}"])
    if C.KILL_FILE.exists():
        return V(False, reasons=["停止ファイル data/KILL があるため新規購入なし"])
    if len(state["positions"]) >= C.MAX_OPEN_POSITIONS:
        return V(False, reasons=[f"保有が上限の{C.MAX_OPEN_POSITIONS}件"])
    dpl = P.day_pnl(state, now)
    if dpl <= -C.DAILY_LOSS_LIMIT:
        return V(False, reasons=[f"今日の損失 ${-dpl:.2f}（上限$10）— ハワイ時間の明日まで新規購入なし"])
    if state["realized"] <= -C.TOTAL_LOSS_LIMIT:
        state["halted"] = f"累計損失 ${-state['realized']:.2f}（上限$30）"
        return V(False, reasons=[state["halted"]])
    size = state["cash"] * C.MAX_POSITION_PCT
    if C.SMALL_MODE:
        size = min(max(size, C.SMALL_MIN), C.SMALL_MAX)
    size = min(size, state["cash"])
    if size < 1:
        return V(False, reasons=["現金不足"])
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    slip = P.slippage(size, liq)
    if slip > C.MAX_SLIPPAGE:
        return V(False, reasons=[f"スリッページ見込み {slip*100:.2f}%（2%超）"])
    worst_new = size * C.WORST_CASE_GAP
    room_day = C.DAILY_LOSS_LIMIT + dpl - P.worst_case_open(state)
    room_total = C.TOTAL_LOSS_LIMIT + state["realized"] - P.worst_case_open(state)
    if worst_new > room_day or worst_new > room_total:
        return V(False, reasons=[f"急落時の最大損失 ${worst_new:.2f} が残りの損失枠を超える"])
    return V(True, reasons=[f"${size:.2f}で可（スリッページ {slip*100:.2f}%）"], size=size, slip=slip)


# ============================================================ 6. Exit
def exit_decision(pos, price, liq, now=None):
    """Returns (fraction_to_sell, reason) or (0, None)."""
    now = now or time.time()
    chg = price / pos["entry"] - 1
    pos["peak"] = max(pos["peak"], price)
    if chg <= C.STOP_LOSS:
        return 1.0, f"損切り（{chg*100:+.1f}%）"
    if liq is not None and liq < 5000:
        return 1.0, f"流動性が急減（${liq:,.0f}）"
    if not pos["half_sold"] and chg >= C.TAKE_PROFIT_1:
        pos["half_sold"] = True
        return 0.5, f"利益確定で半分売却（{chg*100:+.1f}%）"
    if pos["peak"] / pos["entry"] - 1 >= C.TRAIL_ARM and price <= pos["peak"] * (1 - C.TRAIL_DIST):
        return 1.0, f"トレーリングストップ（高値から-{C.TRAIL_DIST*100:.0f}%、損益{chg*100:+.1f}%）"
    if (now - pos["opened"]) / 60 >= C.MAX_HOLD_MIN:
        return 1.0, f"保有時間の上限（{C.MAX_HOLD_MIN//60}時間、損益{chg*100:+.1f}%）"
    return 0, None


# ============================================================ 8. Desk Manager
def entry_score(sn, wh, hy):
    return round(0.5 * sn["score"] + 0.25 * wh["score"] + 0.25 * hy["score"])
