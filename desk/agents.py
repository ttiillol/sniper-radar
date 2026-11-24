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
        base = "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&order=DESC&includeNsfw=false&complete=true"
        rows = []
        # newest-created graduates are the likeliest fresh graduations; recently traded ones as a second net
        for sort in ("created_timestamp", "last_trade_timestamp"):
            data = S.get_json(base + "&sort=" + sort)
            rows += data if isinstance(data, list) else []
        n = 0
        for c in rows:
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
            if C.STRATEGY == "scalp":
                # any age is fine; a candidate lives while it keeps trending (scout refreshes it)
                if now - c.get("refreshed", c["seen"]) > C.SCALP_CAND_TTL_SEC:
                    c["status"] = "expired"
                elif now >= c.get("next_check", 0):
                    out.append(m)
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
        fails.append(f"流動性 ${liq:,.0f}（${C.MIN_LIQ_USD:,.0f}未満）")

    # --- impersonation
    sym = ((pair.get("baseToken") or {}).get("symbol") or "").upper().lstrip("$")
    facts["symbol"] = sym
    if sym in C.FAMOUS_TICKERS:
        fails.append(f"有名銘柄 {sym} の名前を使用（偽物の疑い）")
    prev = state["seen_symbols"].get(sym)
    if isinstance(prev, dict) and prev.get("mint") != mint and now - (prev.get("t") or 0) < 7 * 86400:
        fails.append(f"同じ名前 {sym} の別コインが7日以内に卒業済み（コピー銘柄の疑い）")

    rc = rc if rc is not None else S.rugcheck(mint)

    # --- on-chain: authorities, holders
    try:
        mi = minfo or S.mint_info(mint, rpc)
        facts["mint_authority"], facts["freeze_authority"] = mi["mint_authority"], mi["freeze_authority"]
        if mi["mint_authority"]:
            fails.append("mint権限が放棄されていない")
        if mi["freeze_authority"]:
            fails.append("freeze権限が放棄されていない")
        supply = mi["supply"]
        hs = holders if holders is not None else S.top_holders(mint, rpc, rc)
        wallets = [h for h in hs if h["is_wallet"]][:10]
        top10 = sum(h["amount"] for h in wallets) / supply if supply else 1
        facts["top10_pct"] = round(top10 * 100, 1)
        if top10 >= C.MAX_TOP10_PCT:
            fails.append(f"上位10ウォレットが {top10*100:.1f}%（30%以上）")
        top3 = sum(h["amount"] for h in wallets[:3]) / supply if supply else 1
        facts["top3_pct"] = round(top3 * 100, 1)
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
    creator = None
    if not rc:
        fails.append("データ不足: RugCheckが応答しない")
    else:
        creator = rc.get("creator")
        facts["creator"] = creator
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
        # holders, insiders, creator history (all from the same free RugCheck report)
        th = rc.get("totalHolders")
        facts["holders"] = th
        if th is not None and th < C.MIN_HOLDERS:
            fails.append(f"保有者 {th}人（{C.MIN_HOLDERS}人未満）")
        ins = sum(float(h.get("pct") or 0) for h in rc.get("topHolders") or [] if h.get("insider"))
        facts["insider_pct"] = round(ins, 2)
        facts["insider_graph"] = rc.get("graphInsidersDetected") or 0
        if ins / 100 >= C.MAX_INSIDER_PCT:
            fails.append(f"インサイダー財布が {ins:.1f}% 保有（RugCheck）")
        prev = [t for t in rc.get("creatorTokens") or [] if t.get("mint") != mint]
        dead = sum(1 for t in prev if float(t.get("marketCap") or 0) < C.DEAD_MCAP_USD)
        facts["creator_prev"], facts["creator_dead"] = len(prev), dead
        if len(prev) >= C.SERIAL_MIN_PREV and dead / len(prev) >= C.SERIAL_DEAD_RATIO:
            fails.append(f"作成者の過去コイン{len(prev)}個中{dead}個が死亡（使い捨ての常習）")

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
    if rt is None and early is None and (facts.get("age_min") or 0) > C.EARLY_CHECK_MAX_AGE_MIN:
        notes.append("作られてから6時間以上たったコインのため、初期の買い手チェックは省略")
        return V(not fails, reasons=fails or ["全項目合格"], notes=notes, facts=facts, creator=creator)
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
        early_list = (rt or {}).get("early") if rt is not None else early
        gaps = {e["wallet"]: e["gap"] for e in (early_list or [])}
        if gaps and supply:
            snip_amt = sum(h["amount"] for h in hs if h["owner"] in gaps and gaps[h["owner"]] > 0)
            bund_amt = sum(h["amount"] for h in hs if h["owner"] in gaps and gaps[h["owner"]] == 0)
            facts["sniper_hold_pct"] = round(snip_amt / supply * 100, 2)
            facts["bundle_hold_pct"] = round(bund_amt / supply * 100, 2)
            if snip_amt / supply >= C.MAX_SNIPER_HOLD_PCT:
                fails.append(f"狙撃ボットがまだ {snip_amt/supply*100:.1f}% 保有（売り浴びせ待ち）")
            if bund_amt / supply >= C.MAX_BUNDLE_HOLD_PCT:
                fails.append(f"まとめ買い財布がまだ {bund_amt/supply*100:.1f}% 保有")
    except Exception as e:
        notes.append(f"初期の買い手を確認できず（{getattr(e, 'reason', type(e).__name__)}）")

    return V(not fails, reasons=fails or ["全項目合格"], notes=notes, facts=facts, creator=creator)


# ============================================================ 4b. Momentum (scalp gate)
def pullback(cand, pair, now):
    """PULLBACK agent: after a spike, wait for a dip and a bounce instead of chasing.
    Returns (state, reason): state None (no spike), 'start', 'watch', 'ready' or 'cancel'."""
    pc = pair.get("priceChange") or {}
    m5, h1 = float(pc.get("m5") or 0), float(pc.get("h1") or 0)
    try:
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    pb = cand.get("pb")
    if not price:
        return (None, "") if pb is None else ("watch", "価格不明")
    if pb is None:
        if m5 > C.SCALP_MAX_M5 and h1 >= C.PULLBACK_MIN_H1:
            cand["pb"] = {"peak": price, "low": price, "t": now}
            return "start", f"5分で+{m5:.0f}%の急騰 → 飛び乗らずに押し目を待つ"
        return None, ""
    if now - pb["t"] > C.PULLBACK_TTL_SEC:
        cand.pop("pb", None)
        return "cancel", f"{C.PULLBACK_TTL_SEC // 60}分待っても押し目買いの形にならない"
    if price > pb["peak"]:
        pb.update(peak=price, low=price)
        return "watch", "高値を更新中（まだ押していない）"
    pb["low"] = min(pb["low"], price)
    drop = 1 - pb["low"] / pb["peak"]
    if drop > C.PULLBACK_MAX:
        cand.pop("pb", None)
        return "cancel", f"高値から{drop * 100:.0f}%下落（崩れた）"
    if drop < C.PULLBACK_MIN:
        return "watch", f"高値から{drop * 100:.1f}%（押しが浅い）"
    bounce = price / pb["low"] - 1
    if bounce < C.PULLBACK_BOUNCE:
        return "watch", f"高値から{drop * 100:.0f}%押した → 下げ止まり待ち"
    return "ready", f"押し目買いの形（高値から{drop * 100:.0f}%押して{bounce * 100:.1f}%戻した）"


def momentum(pair, flow=None, pre=False, pullback=False):
    """MOMENTUM agent for the scalp strategy. ok=False means 'not now' (look again shortly),
    not 'bad coin'. Uses DexScreener price changes/volume + the last-10-minute order flow."""
    pc = pair.get("priceChange") or {}
    m5, h1 = float(pc.get("m5") or 0), float(pc.get("h1") or 0)
    vol5 = float((pair.get("volume") or {}).get("m5") or 0)
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    b5, s5 = tx5.get("buys") or 0, tx5.get("sells") or 0
    why, score = [], 50
    if pullback:                                   # dip entry: the 5-minute change is negative by design
        p = (flow or {}).get("pressure")
        if h1 < C.SCALP_MIN_H1:
            return V(False, 0, [f"1時間の流れが下向き（{h1:+.1f}%）"])
        if p is None or p < C.PULLBACK_PRESSURE:
            return V(False, 0, [f"押し目で買いが戻っていない（直近2分の買い{(p or 0) * 100:.0f}%）"])
        return V(True, 70, [f"押し目買い（1時間 +{h1:.0f}%・直近2分の買い{p * 100:.0f}%）"])
    if m5 > C.SCALP_MAX_M5:
        return V(False, 0, [f"5分で+{m5:.0f}%の急騰（飛び乗りになる）"])
    if m5 < C.SCALP_MIN_M5:
        return V(False, 0, [f"5分の勢いが弱い（{m5:+.1f}%）"])
    if h1 < C.SCALP_MIN_H1:
        return V(False, 0, [f"1時間の流れが下向き（{h1:+.1f}%）"])
    if vol5 and vol5 < C.SCALP_MIN_VOL_M5:
        return V(False, 0, [f"5分の売買額が少ない（${vol5:,.0f}）"])
    if pre:                                        # free pre-check from DexScreener data only
        return V(True, 50, [f"5分 +{m5:.1f}%"])
    p = (flow or {}).get("pressure")
    if p is not None and p < C.SCALP_MIN_PRESSURE:
        return V(False, 0, [f"買いの勢いが足りない（買い{p * 100:.0f}%）"])
    if p is None and b5 <= s5:
        return V(False, 0, [f"5分の買い{b5}件 ≦ 売り{s5}件"])
    score += min(20, m5 * 2); why.append(f"5分 +{m5:.1f}%")
    if h1 >= 20:
        score += 10; why.append(f"1時間 +{h1:.0f}%（上昇トレンド）")
    if p is not None and p >= 0.65:
        score += 10; why.append(f"買い{p * 100:.0f}%")
    return V(True, round(min(100, score)), why)


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
    t3 = facts.get("top3_pct")
    if t3 is not None and t3 > 30:
        score -= 20 if t3 > 50 else 10; why.append(f"上位3人に集中（{t3}%）")
    if (facts.get("sniper_hold_pct") or 0) > 10:
        score -= 10; why.append(f"狙撃ボット保有 {facts['sniper_hold_pct']}%")
    h, ph = facts.get("holders"), cand.get("prev_holders")
    if h and ph:
        growth = h / ph - 1
        if growth >= 0.10:
            score += 10; why.append(f"保有者が増加中（+{growth*100:.0f}%）")
        elif growth <= -0.05:
            score -= 10; why.append("保有者が減少中")
    if h:
        cand["prev_holders"] = h
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
    info = pair.get("info") or {}
    kinds = {(x.get("type") or "").lower() for x in info.get("socials") or []}
    if kinds & {"twitter", "x", "telegram"}:
        score += 10; why.append("X/テレグラムあり")
    elif info.get("websites"):
        score -= 5; why.append("サイトのみ（実質匿名）")
    else:
        score -= 15; why.append("SNSなし")
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
def conviction(pair, facts, flow, score, kol_a=0):
    """How sure are we? 'max' only when EVERY item of the perfect-momentum checklist holds."""
    pc = pair.get("priceChange") or {}
    m5, h1 = float(pc.get("m5") or 0), float(pc.get("h1") or 0)
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    p = (flow or {}).get("pressure")
    checks = [
        (C.PERFECT_M5[0] <= m5 <= C.PERFECT_M5[1], f"5分 {m5:+.1f}%"),
        (h1 >= C.PERFECT_H1, f"1時間 {h1:+.0f}%"),
        (p is not None and p >= C.PERFECT_PRESSURE, f"買い{(p or 0) * 100:.0f}%"),
        ((flow or {}).get("burst_wallets", 0) >= C.PERFECT_WHALES or kol_a > 0,
         f"大口{(flow or {}).get('burst_wallets', 0)}人/KOL-A {kol_a}人"),
        (liq >= C.PERFECT_MIN_LIQ, f"流動性 ${liq:,.0f}"),
        ((facts.get("holders") or 0) >= C.PERFECT_MIN_HOLDERS, f"保有者 {facts.get('holders')}人"),
    ]
    missing = [label for ok, label in checks if not ok]
    if not missing:
        return "max", ["完璧な勢い（" + "・".join(l for _, l in checks[:4]) + "）"]
    if score >= C.STRONG_SCORE and p is not None and p >= C.STRONG_PRESSURE:
        return "strong", [f"強い勢い（点数{score}・買い{p * 100:.0f}%）"]
    return "normal", []


TIER_PCT = {"normal": None, "strong": "SIZE_STRONG_PCT", "max": "SIZE_MAX_PCT"}
TIER_JA = {"normal": "通常", "strong": "強い", "max": "完璧"}


def risk(state, pair, now=None, tier="normal"):
    now = now or time.time()
    if state.get("halted"):
        return V(False, reasons=[f"停止中: {state['halted']}"])
    if C.KILL_FILE.exists():
        return V(False, reasons=["停止ファイル data/KILL があるため新規購入なし"])
    if C.QUALITY_ON:
        paused, why = loss_streak(state, now)
        if paused:
            return V(False, reasons=[why])
    if len(state["positions"]) >= C.MAX_OPEN_POSITIONS:
        return V(False, reasons=[f"保有が上限の{C.MAX_OPEN_POSITIONS}件"])
    dpl = P.day_pnl(state, now)
    if dpl <= -C.DAILY_LOSS_LIMIT:
        return V(False, reasons=[f"今日の損失 ${-dpl:.2f}（上限${C.DAILY_LOSS_LIMIT:.0f}）— ハワイ時間の明日まで新規購入なし"])
    if state["realized"] <= -C.TOTAL_LOSS_LIMIT:
        state["halted"] = f"累計損失 ${-state['realized']:.2f}（上限${C.TOTAL_LOSS_LIMIT:.0f}）"
        return V(False, reasons=[state["halted"]])
    equity = state["cash"] + sum(p["cost"] for p in state["positions"].values())
    pct = getattr(C, TIER_PCT[tier]) if TIER_PCT.get(tier) else C.MAX_POSITION_PCT
    size = equity * pct                                  # SIZER: a share of the whole book, by conviction
    liq0 = (pair.get("liquidity") or {}).get("usd") or 0
    if liq0 > 0:                                         # don't move the price more than 1% ourselves
        reserve = liq0 / 2.0
        size = min(size, C.MAX_ENTRY_IMPACT * reserve / (1 - C.MAX_ENTRY_IMPACT)) if tier != "normal" else size
    if C.SMALL_MODE:
        size = min(max(size, C.SMALL_MIN), C.SMALL_MAX)
    size = min(size, state["cash"])
    if size < 1:
        return V(False, reasons=["現金不足"])
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    slip = P.slippage(size, liq)
    if slip > C.MAX_SLIPPAGE:
        return V(False, reasons=[f"スリッページ見込み {slip*100:.2f}%（2%超）"])
    gap = C.WORST_CASE_GAP_DEEP if liq >= C.PERFECT_MIN_LIQ else C.WORST_CASE_GAP
    worst_new = size * gap
    room_day = C.DAILY_LOSS_LIMIT + dpl - P.worst_case_open(state)
    room_total = C.TOTAL_LOSS_LIMIT + state["realized"] - P.worst_case_open(state)
    room = min(room_day, room_total)
    shrunk = ""
    if worst_new > room:
        # shrink the trade so its worst case still fits the loss room (the daily/total limits still hold)
        fit = room / gap if room > 0 else 0.0
        if tier != "normal" or fit < C.MIN_TRADE_USD:          # big tiers fall back to normal size instead
            return V(False, reasons=[f"急落時の最大損失 ${worst_new:.2f} が残りの損失枠 ${max(room, 0):.2f} を超える"
                                     f"（${C.MIN_TRADE_USD:.0f}未満に縮めないと収まらない）"])
        size = fit
        slip = P.slippage(size, liq)
        shrunk = f"・損失枠の残り${room:.2f}に合わせて縮小"
    return V(True, reasons=[f"${size:.2f}で可（{TIER_JA[tier]}・スリッページ {slip*100:.2f}%{shrunk}）"], size=size,
             slip=slip, tier=tier, gap=gap)


# ============================================================ 4c. Quality (win-rate gate)
def quality(candles, trades, now, price):
    """QUALITY agent: is this a good moment to buy, not just a moving coin?
    Returns V(ok, reasons, facts). Missing candles/trades skip those checks (noted, never a fail)."""
    fails, notes, facts = [], [], {}
    rows = [r for r in (candles or []) if r[4] and r[2] >= r[3] > 0]
    swing = None
    if len(rows) >= 5:
        last = rows[-C.ATR_CANDLES:]
        swing = sum((h - l) / c for _, _, h, l, c in last) / len(last)
        # 1) stretch vs the volume-weighted average of the last 15 minutes (volume ~ range when no volume column)
        win = [r for r in rows if r[0] >= now - C.Q_VWAP_MIN * 60] or rows[-C.Q_VWAP_MIN:]
        typ = [(h + l + c) / 3 for _, _, h, l, c in win]
        vwap = sum(typ) / len(typ)
        ext = (price or rows[-1][4]) / vwap - 1
        lim = min(C.Q_MAX_EXT_CAP, max(C.Q_MAX_EXT, C.Q_EXT_SWING_MULT * swing))
        facts["ext"], facts["swing"] = round(ext, 4), round(swing, 4)
        if ext > lim:
            fails.append(f"伸びすぎ（15分平均より+{ext * 100:.1f}%、上限+{lim * 100:.0f}%）")
        # 2) uptrend shape
        st = rows[-C.Q_STRUCT_CANDLES:]
        ups = sum(1 for a, b in zip(st, st[1:]) if b[3] >= a[3])
        facts["higher_lows"] = ups
        if ups < len(st) - 2:
            fails.append(f"上昇の形になっていない（直近{len(st) - 1}本で安値の切り上げ{ups}回）")
        _, o, h, l, c = rows[-1]
        rng = h - l
        wick = (h - max(o, c)) / rng if rng > 0 else 0
        facts["wick"] = round(wick, 2)
        if wick >= C.Q_MAX_WICK and rng / c > 0.01:
            fails.append(f"最新の1分足に長い上ヒゲ（上で売られている・{wick * 100:.0f}%）")
    else:
        notes.append("1分足なし → 伸びすぎ・形のチェックは省略")
    if trades:
        recent = [x for x in trades if now - x["t"] <= C.FLOW_WINDOW_MIN * 60]
        buys = [x for x in recent if x["kind"] == "buy"]
        bw = {}
        for x in buys:
            bw[x["wallet"]] = bw.get(x["wallet"], 0.0) + x["usd"]
        sw = {x["wallet"] for x in recent if x["kind"] == "sell"}
        bu = sum(bw.values())
        top = max(bw.values()) / bu if bu else 0
        facts.update(q_buyers=len(bw), q_sellers=len(sw), top_buyer=round(top, 2))
        # 3) real buyers
        if len(bw) < C.Q_MIN_BUYERS:
            fails.append(f"買い手が少ない（10分で{len(bw)}人、{C.Q_MIN_BUYERS}人以上が必要）")
        elif len(bw) < C.Q_BUYER_SELLER_RATIO * len(sw):
            fails.append(f"売り手のほうが多い（買い手{len(bw)}人・売り手{len(sw)}人）")
        if bu and top > C.Q_MAX_TOP_BUYER:
            fails.append(f"1人の財布が買いの{top * 100:.0f}%（なれ合い・1人頼みの上げ）")
        # 4) acceleration
        b2 = sum(x["usd"] for x in buys if now - x["t"] <= 120)
        pace = bu / (C.FLOW_WINDOW_MIN / 2) if bu else 0
        facts["accel"] = round(b2 / pace, 2) if pace else None
        if pace and b2 < C.Q_ACCEL * pace:
            fails.append(f"勢いが衰えている（直近2分の買い${b2:,.0f}・10分の平均ペース${pace:,.0f}）")
    else:
        notes.append("取引データなし → 買い手・加速のチェックは省略")
    ok = not fails
    return V(ok, 70 if ok else 0, fails or ["品質チェック合格（伸びすぎなし・上昇の形・本物の買い手・加速中）"],
             notes=notes, facts=facts, swing=swing)


def loss_streak(state, now):
    """(paused?, reason). After LOSS_STREAK losing trades in a row, pause new buys for a while."""
    res = state.get("results") or []
    last = res[-C.LOSS_STREAK:]
    if len(last) < C.LOSS_STREAK or any(p > 0 for _, p in last):
        return False, ""
    until = last[-1][0] + C.LOSS_STREAK_PAUSE_MIN * 60
    if now < until:
        return True, f"{C.LOSS_STREAK}連敗中 — 相場が合っていないので{int((until - now) // 60) + 1}分休む"
    return False, ""


# ============================================================ 6b. Adaptive stop
def adaptive_stop(candles):
    """candles [[ts, o, h, l, c], ...] -> (stop as a positive fraction, avg 1-minute swing or None, reason)."""
    rows = [r for r in (candles or []) if r[4] and r[2] >= r[3] > 0][-C.ATR_CANDLES:]
    if len(rows) < 5:
        return C.STOP_MIN, None, f"揺れ幅が測れない → 標準の損切り−{C.STOP_MIN * 100:.0f}%"
    swing = sum((h - l) / c for _, _, h, l, c in rows) / len(rows)
    sl = min(C.STOP_MAX, max(C.STOP_MIN, C.ATR_MULT * swing))
    return sl, swing, f"1分の平均の揺れ{swing * 100:.1f}% → 損切り−{sl * 100:.0f}%"


def stop_size(size, sl):
    """A wider stop buys less, so the dollars lost at the stop stay about the same."""
    return size * min(1.0, C.STOP_MIN / sl) if sl else size


# ============================================================ 6. Exit
def exit_decision(pos, price, liq, now=None):
    """Returns (fraction_of_CURRENT_position_to_sell, reason) or (0, None). Uses C.EXIT_PROFILE."""
    from .exits import step
    now = now or time.time()
    before = pos.get("sold", 0.0)
    if liq is not None and liq < 5000:
        pos["sold"] = 1.0
        return 1.0, f"流動性が急減（${liq:,.0f}）"
    prof = C.EXIT_PROFILE
    if pos.get("sl"):                                   # adaptive stop chosen for this coin at entry
        prof = dict(prof, sl=-pos["sl"])
    frac_init, why = step(pos, price, now, prof)
    if not frac_init:
        return 0, None
    left_before = 1.0 - before
    return (1.0 if pos["sold"] >= 1 - 1e-9 else min(1.0, frac_init / left_before)), why


# ============================================================ 8. Desk Manager
def entry_score(sn, wh, hy):
    return round(0.5 * sn["score"] + 0.25 * wh["score"] + 0.25 * hy["score"])
