"""Desk Manager loop + CLI.  PAPER TRADING ONLY.

  python -m desk.main run [--minutes 50]   loop (used by GitHub Actions)
  python -m desk.main --once               one decision cycle
  python -m desk.main status               positions / cash / limits
  python -m desk.main summary              performance summary
  python -m desk.main rug <mint>           run the Rug check on one coin
Create data/KILL to stop new buys and make the loop exit safely.
"""
import argparse
import re
import asyncio
import json
import os
import time

from . import agents as A
from . import config as C
from . import portfolio as P
from . import sources as S
from . import tracker as T
from . import notify as N
from . import live as L
from . import journal as J
from . import flow as F
from . import selfcheck as SC
import scanner


_radar_cache = {"t": 0, "v": None}


def load_radar():
    """Sniper-radar output (rebuilt from run logs; cached 10 min)."""
    if _radar_cache["v"] is None or time.time() - _radar_cache["t"] > 600:
        try:
            wallets, tokens, _ = scanner.rebuild(C.DATA)
        except Exception:
            wallets, tokens = {}, []
        grouped = {w for g in scanner.wallet_groups(tokens) for w in g["wallets"]}
        _radar_cache.update(t=time.time(), v={
            "bots": {w for w, v in wallets.items() if v.get("count", 0) >= 5} | grouped,
            "tokens": {t["mint"]: t for t in tokens}})
    v = _radar_cache["v"]
    return {"bots": set(v["bots"]), "tokens": dict(v["tokens"])}


def credits_ok(state):
    """Shared Helius budget with the radar."""
    if scanner.RPC_KIND != "helius":
        return True
    mk = scanner.month_key()
    try:
        radar_used = scanner.radar_credits(mk, C.DATA)
    except Exception:
        radar_used = 0
    return radar_used + state["credits"].get(mk, 0) + scanner.CALLS["n"] < scanner.MONTHLY_BUDGET


STAGE_AGENT = {"Momentum": "trend", "Rug": "rug", "Sniper": "picker", "Risk": "sizer", "Flow": "flow", "Sell": "exec", "Reentry": "whale"}
STAGE_JA = {"Rug": "安全チェック", "Sniper": "点数", "Risk": "資金ルール", "Flow": "取引の流れ",
            "Sell": "売却テスト", "Reentry": "再エントリー", "Momentum": "勢い"}


class Desk:
    def __init__(self, state=None, fetch_pairs=S.dex_pairs, rug=A.rug_check,
                 trades=F.pool_trades, sell_check=F.sell_check):
        self.s = state or P.load_state()
        self.scanner = A.Scanner()
        self.fetch_pairs = fetch_pairs
        self.rug = rug
        self.trades = trades
        self.sell_check = sell_check
        self._dev_checked = {}
        self._jcache = (0.0, {"n": 0})
        self._last_refine = 0.0
        self._calls0 = scanner.CALLS["n"]

    # ------------------------------------------------ restart / gap handling
    def resume(self, now=None):
        now = now or time.time()
        hb = self.s.get("last_heartbeat")
        if hb and now - hb > C.GAP_WARN_SEC:
            gap = int(now - hb)
            self.s["gaps"] = (self.s["gaps"] + [{"ts": int(now), "sec": gap,
                                                 "open": len(self.s["positions"])}])[-50:]
            P.log_decision("gap", "", "", "RESUME", f"{gap//60}分止まっていた → すぐに損切り条件を確認",
                           open_positions=len(self.s["positions"]))
        self.check_exits(now)              # always check stops first thing after (re)start

    # ------------------------------------------------ exits (Exit agent)
    def check_exits(self, now=None):
        now = now or time.time()
        if not self.s["positions"]:
            return
        L.step("check", f"保有中 {len(self.s['positions'])}件の値段を確認（損切り・利確のタイミングか判定）", log=False,
               agent="stop", note=f"保有{len(self.s['positions'])}件を監視中")
        pairs = self.fetch_pairs(list(self.s["positions"]))
        for mint in list(self.s["positions"]):
            try:
                self._exit_one(mint, pairs, now)
            except Exception as e:
                self._error("exit", mint, e)
        for mint in list(self.s["positions"]):
            try:
                self._dev_watch(mint, now)
            except Exception as e:
                self._error("dev_watch", mint, e)

    def _dev_watch(self, mint, now):
        """STOP GUARD extra: if the creator sells after we bought, get out at once."""
        pos = self.s["positions"][mint]
        if not (C.FLOW_ON and pos.get("creator") and pos.get("pool")):
            return
        if now - self._dev_checked.get(mint, 0) < C.DEV_CHECK_SEC:
            return
        self._dev_checked[mint] = now
        tr = self.trades(pos["pool"])
        if not tr:
            return
        sold = sum(x["usd"] for x in tr if x["kind"] == "sell" and x["wallet"] == pos["creator"]
                   and x["t"] >= pos["opened"])
        if sold <= 0:
            return
        price = pos.get("last_price") or pos["entry"]
        why = f"作成者が売った（${sold:,.0f}）→ 全部売却"
        pnl = P.paper_sell(self.s, mint, 1.0, price, why, now)
        P.log_decision("exit", mint, pos["symbol"], "SELL", why, fraction=1.0,
                       change_pct=round((price / pos["entry"] - 1) * 100, 2), pnl=round(pnl, 2))
        c = self.s["candidates"].get(mint)
        if c:
            c["reentry_used"] = True                 # never re-enter a coin whose dev dumped
        L.step("stop", f"練習売り 100% ・ {pnl:+.2f}ドル ・ {why}", pos["symbol"], mint, "STOP", agent="stop",
               note=f"{pos['symbol']} 作成者の売りで撤退 {pnl:+.2f}ドル")
        N.send_async(f"STOP {pos['symbol']}  {pnl:+.2f}ドル", f"{why}\n残高 ${self.s['cash']:.2f}（練習）",
                     click=f"https://dexscreener.com/solana/{mint}")

    def _exit_one(self, mint, pairs, now):
        if True:
            pos = self.s["positions"][mint]
            pair = pairs.get(mint)
            price = S.pair_price(pair) if pair else None
            if not price:
                P.log_decision("exit", mint, pos["symbol"], "NO_PRICE", "価格を取得できず — 次の確認で再試行")
                L.step("error", "値段が取れず — 次の確認でもう一度", pos["symbol"], mint, "NO_PRICE", agent="stop")
                return
            liq = (pair.get("liquidity") or {}).get("usd")
            pos["liq"] = liq or pos.get("liq")
            pos["last_price"] = price
            frac, why = A.exit_decision(pos, price, liq, now)
            if frac:
                chg = price / pos["entry"] - 1
                pnl = P.paper_sell(self.s, mint, frac, price, why, now)
                P.log_decision("exit", mint, pos["symbol"], "SELL", why, fraction=frac,
                               change_pct=round(chg * 100, 2), pnl=round(pnl, 2))
                stop = why.startswith("損切り")
                c = self.s["candidates"].get(mint)
                if C.STRATEGY == "scalp":
                    if mint not in self.s["positions"] and c and not c.get("reentry_used"):
                        td = c.get("trades_day") or ["", 0]
                        if not (td[0] == P.hst_day(now) and td[1] >= C.SCALP_MAX_TRADES_PER_COIN):
                            c.update(status="waiting", next_check=now + C.SCALP_COOLDOWN_SEC, checks=0,
                                     refreshed=int(now))   # same coin, next trade if momentum is still there
                elif stop and mint not in self.s["positions"] and c and not c.get("reentry_used"):
                    # same coin, second answer if the facts change (whales pile in after our stop)
                    c.update(status="waiting", next_check=now + C.REENTRY_WAIT_SEC, reentry_used=True,
                             reentry=True)
                L.step("stop" if stop else "sell", f"練習売り {frac * 100:.0f}% ・ {pnl:+.2f}ドル ・ {why}",
                       pos["symbol"], mint, "STOP" if stop else "SELL", agent="stop" if stop or "ストップ" in why else "tp",
                       note=f"{pos['symbol']} {frac * 100:.0f}%売却 {pnl:+.2f}ドル")
                N.send_async(f"{'STOP' if stop else 'SELL'} {pos['symbol']}  {pnl:+.2f}ドル",
                             f"{why}\n残高 ${self.s['cash']:.2f} ・ 今日 {P.day_pnl(self.s, now):+.2f}ドル（練習）",
                             tags=["red_circle" if stop else "large_blue_circle"],
                             click=f"https://dexscreener.com/solana/{mint}")
            else:
                g = (price / pos['entry'] - 1) * 100
                L.step("hold", f"買値から {g:+.1f}% — まだ持つ", pos["symbol"], mint, "HOLD", log=False,
                       agent="tp" if g > 0 else "stop", note=f"{pos['symbol']} {g:+.1f}% 保有中")

    def _error(self, where, mint, e):
        import traceback
        st = self.s["stats"]
        st["errors"] = st.get("errors", 0) + 1
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        P.log_decision("error", mint, "", "ERROR", f"[{where}] {msg}")
        L.step("error", f"エラー（{where}）— 止まらずに次へ進みます: {msg[:60]}", "", mint, "ERROR")
        print("ERROR", where, mint, msg, flush=True)
        traceback.print_exc()

    # ------------------------------------------------ entries
    def prefilter(self, mint, pair, now):
        """Free checks before spending any RPC credits."""
        if not pair:
            return "データ不足: DexScreenerにペアが無い"
        if C.STRATEGY == "scalp":
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            if liq < C.MIN_LIQ_USD:
                return f"流動性 ${liq:,.0f}（${C.MIN_LIQ_USD:,.0f}未満）"
            return None
        if pair.get("dexId") != "pumpswap":
            return f"PumpSwap以外の取引所（{pair.get('dexId')}）"
        created = (pair.get("pairCreatedAt") or 0) / 1000
        if created and (now - created) / 60 > C.MAX_AGE_AFTER_GRAD_MIN:
            return f"卒業から{(now - created) / 3600:.0f}時間たっている（新しい卒業ではない）"
        return None

    def evaluate(self, mint, pair, radar, now=None):
        now = now or time.time()
        bad = self.prefilter(mint, pair, now)
        if bad:
            c = self.s["candidates"].setdefault(mint, {"seen": int(now), "grad": None, "symbol": "",
                                                       "status": "waiting", "checks": 0})
            c["status"] = "skipped"
            c["last_eval"] = int(now)          # the scout won't re-open it for 15 minutes
            st = self.s["stats"].setdefault("prefiltered", {})
            key = re.sub(r"\$?\d[\d,\.]*", "N", bad.split("（")[0])
            st[key] = st.get(key, 0) + 1
            L.step("skip", f"下見で除外: {bad}", ((pair or {}).get("baseToken") or {}).get("symbol", ""), mint, "SKIP",
                   agent="scout", note=f"下見で除外 {bad[:30]}")
            return "skipped"
        cand = self.s["candidates"].setdefault(mint, {"seen": int(now), "grad": None, "symbol": "",
                                                      "status": "waiting", "checks": 0})
        cand["checks"] += 1
        cand["last_eval"] = int(now)
        self.s["stats"]["evaluated"] += 1
        if pair and pair.get("pairCreatedAt"):
            cand["grad"] = pair["pairCreatedAt"] / 1000
            if (now - cand["grad"]) / 60 < C.MIN_AGE_AFTER_GRAD_MIN:
                cand["next_check"] = cand["grad"] + C.MIN_AGE_AFTER_GRAD_MIN * 60
                L.step("wait", f"卒業から{(now - cand['grad']) / 60:.0f}分 — {C.MIN_AGE_AFTER_GRAD_MIN}分たつまで様子見",
                       ((pair or {}).get("baseToken") or {}).get("symbol", ""), mint, "WAIT", agent="scout")
                return "wait"
        sym = ((pair or {}).get("baseToken") or {}).get("symbol", cand.get("symbol", ""))
        cand["symbol"] = sym
        if C.STRATEGY == "scalp":
            # free momentum pre-check first: the expensive safety checks only run on coins that are moving
            pre = A.momentum(pair, pre=True)
            if not pre["ok"]:
                cand["checks"] -= 1                          # a free pre-check is not a real evaluation
                cand["pre_checks"] = cand.get("pre_checks", 0) + 1
                self.s["stats"]["evaluated"] -= 1           # not a real evaluation
                if cand["pre_checks"] <= C.SCALP_MAX_CHECKS:
                    cand["next_check"] = now + C.SCALP_RECHECK_SEC
                    L.step("wait", f"勢い待ち: {pre['reasons'][0]}", sym, mint, "WAIT", log=False,
                           agent="trend", note=f"{sym} {pre['reasons'][0][:30]}")
                    return "wait"
                cand["status"] = "rejected"
                L.step("reject", f"見送り（勢い）: {pre['reasons'][0]}", sym, mint, "REJECT", agent="trend",
                       note=f"{sym} 勢いが出ないので見送り")
                return "skipped"

        liq0 = ((pair or {}).get("liquidity") or {}).get("usd")
        price0 = S.pair_price(pair) if pair else None

        def reject(stage, reasons, score=None, **extra):
            cand["status"] = "rejected"
            if stage in ("Rug", "Flow", "Sell"):
                cand["hard_until"] = int(now) + 6 * 3600     # don't re-examine a dangerous coin for 6 hours
            T.start(self.s, mint, sym, price0, liq0, "REJECT", reasons, score, now, facts=extra.get("facts"),
                    pool=(pair or {}).get("pairAddress"))
            key = re.sub(r"\$?\d[\d,\.]*", "N", reasons[0].split("（")[0]).replace("上位N", "上位10")[:32]
            self.s["stats"]["rejected"][key] = self.s["stats"]["rejected"].get(key, 0) + 1
            P.log_decision("entry", mint, sym, "REJECT", f"[{stage}] " + " / ".join(reasons), **extra)
            ag = STAGE_AGENT.get(stage, "picker")
            if stage == "Rug" and any(k in reasons[0] for k in ("まとめ買い", "狙撃", "バンドル", "同ブロック")):
                ag = "bundle"
            L.step("reject", f"見送り（{STAGE_JA.get(stage, stage)}）: {reasons[0]}", sym, mint, "REJECT", agent=ag,
                   note=f"{sym} 見送り {reasons[0][:34]}")
            return "rejected"

        if cand.get("radar") and mint not in radar["tokens"]:
            radar["tokens"][mint] = cand["radar"]   # reuse the expensive early-buyer lookup
        L.step("work", f"審査中: 流動性 ${liq0 or 0:,.0f} ・ 保有者 ・ 狙撃ボット ・ まとめ買い ・ インサイダー ・ ロック状況を確認…",
               sym, mint, agent="rug", note=f"{sym} を審査中…")
        cached = cand.get("rug_cache") if C.STRATEGY == "scalp" and cand.get("rug_until", 0) > now else None
        if cached:
            rg = {"ok": True, "reasons": ["全項目合格（30分以内の結果を再利用）"], "notes": [],
                  "facts": dict(cached["facts"]), "creator": cached.get("creator")}
        else:
            rg = self.rug(mint, pair, self.s, radar, now)
            if rg["ok"] and C.STRATEGY == "scalp":
                cand["rug_cache"] = {"facts": {k: v for k, v in (rg.get("facts") or {}).items() if k != "flow"},
                                     "creator": rg.get("creator")}
                cand["rug_until"] = int(now) + C.SCALP_RUG_CACHE_SEC
        f = rg.get("facts") or {}
        if "block0" in f:
            cand["radar"] = {"block0": f["block0"], "snipers": f.get("snipers", 0)}
        u = (sym or "").upper().lstrip("$")
        cur = self.s["seen_symbols"].get(u)
        if u and not (isinstance(cur, dict) and now - cur.get("t", 0) < 7 * 86400):
            self.s["seen_symbols"][u] = {"mint": mint, "t": int(cand.get("grad") or now)}
        if not rg["ok"]:
            return reject("Rug", rg["reasons"], facts=rg.get("facts"), notes=rg.get("notes"))
        fl, fv = None, F.score(None)
        if C.FLOW_ON:
            f0 = rg["facts"]
            L.note("rug", f"{sym} 合格・作成者{f0.get('dev_pct') if f0.get('dev_pct') is not None else '?'}%", sym, "OK")
            L.note("bundle", f"{sym} まとめ買い{f0.get('bundle_hold_pct', '?')}%・狙撃{f0.get('sniper_hold_pct', '?')}%", sym, "OK")
            L.step("work", "安全チェック合格 → 直近10分の取引を確認中（大口の買い・買い手の数・作成者の売り）…", sym, mint,
                   agent="flow", note=f"{sym} の取引を確認中…")
            tr = self.trades((pair or {}).get("pairAddress"))
            fl = F.analyze(tr, now, rg["facts"].get("creator"), F.load_kols()) if tr else None
            fv = F.score(fl)
            if fl:
                rg["facts"]["flow"] = fl
                L.note("whale", f"{sym} 大口{fl['whale_wallets']}人 ${fl['whale_usd']:,}", sym)
                L.note("kol", f"{sym} KOL {len(fl['kol_buys'])}人" + (f"（{'・'.join(fl['kol_names'][:2])}）" if fl["kol_names"] else ""), sym)
                L.note("flow", f"{sym} 買い手{fl['buyers']}人・買い{(fl['pressure'] or 0) * 100:.0f}%", sym)
                L.status(flow={"symbol": sym, "mint": mint, "ladder": fl["ladder"], "whales": fl["whale_list"],
                               "net": fl["buy_usd"] - fl["sell_usd"], "wb": fl["whale_wallets"], "ws": fl["whale_sellers"],
                               "t": int(now)})
                rg["facts"]["kol_n"] = len(fl["kol_buys"])
                if fl["kol_buys"]:
                    ks = self.s.setdefault("kol_seen", [])
                    ks.append({"ts": int(now), "mint": mint, "symbol": sym, "names": fl["kol_names"][:5],
                               "n": len(fl["kol_buys"]), "a": fl.get("kol_a", 0)})
                    del ks[:-60]
            if not fv["ok"]:
                cand["reentry_used"] = True
                return reject("Flow", fv["reasons"], facts=rg["facts"])
        mom = None
        if C.STRATEGY == "scalp":
            mom = A.momentum(pair, fl)
            if not mom["ok"]:
                if cand["checks"] < C.SCALP_MAX_CHECKS:
                    cand["next_check"] = now + C.SCALP_RECHECK_SEC
                    L.step("wait", f"勢い待ち: {mom['reasons'][0]} — {C.SCALP_RECHECK_SEC}秒後にもう一度", sym, mint, "WAIT",
                           log=False, agent="trend", note=f"{sym} {mom['reasons'][0][:30]}")
                    return "watch"
                return reject("Momentum", mom["reasons"], facts=rg["facts"])
            L.note("trend", f"{sym} 勢いあり（{'・'.join(mom['reasons'][:2])}）", sym, "OK")
        if cand.get("reentry") and C.STRATEGY != "scalp":
            if not fl or fl["burst_wallets"] < C.REENTRY_MIN_WHALES:
                return reject("Reentry", [f"損切り後の再エントリー条件（大口{C.REENTRY_MIN_WHALES}人以上が2分で買う）を満たさず"],
                              facts=rg["facts"])
        L.step("work", "勢い・大口・話題性・取引の流れから点数を計算中…", sym, mint, agent="picker", note=f"{sym} の点数を計算中…")
        sn, wh, hy = A.sniper(pair), A.whale(rg["facts"], cand), A.hype(pair)
        score = A.entry_score(sn, wh, hy) + round((fv["score"] - 50) * C.FLOW_WEIGHT)
        if mom:
            score += round((mom["score"] - 50) * 0.5)
            hy["reasons"] = mom["reasons"] + hy["reasons"]
        if cand.get("trending"):
            score += C.TRENDING_BONUS
            hy["reasons"].append(f"GeckoTerminalで注目{cand['trending']}位")
        why = {"sniper": sn["reasons"], "whale": wh["reasons"], "hype": hy["reasons"], "flow": fv["reasons"],
               "score": score}
        why_x = {k: v for k, v in why.items() if k != "score"}      # score is passed on its own
        if not sn["ok"] or score < C.MIN_ENTRY_SCORE:
            if cand["checks"] < 4 and sn["ok"] and score >= 40:
                cand["next_check"] = now + (C.SCALP_RECHECK_SEC if C.STRATEGY == "scalp" else 300)
                cand["score"] = score
                T.start(self.s, mint, sym, price0, liq0, "WATCH", [], score, now, facts=rg["facts"],
                        pool=(pair or {}).get("pairAddress"))
                P.log_decision("entry", mint, sym, "WATCH", f"点数{score}（{C.MIN_ENTRY_SCORE}未満）— 5分後にまた確認", **why)
                L.step("watch", f"点数{score}（合格は{C.MIN_ENTRY_SCORE}点）— 5分後にもう一度見る", sym, mint, "WATCH",
                       agent="picker", note=f"{sym} {score}点 → 5分後に再審査")
                return "watch"
            return reject("Sniper", sn["reasons"] if not sn["ok"] else [f"点数{score}（{C.MIN_ENTRY_SCORE}未満）"],
                          score=score, facts=rg["facts"], **why_x)
        tier, tier_why = ("normal", [])
        if C.STRATEGY == "scalp":
            tier, tier_why = A.conviction(pair, rg["facts"], fl, score, (fl or {}).get("kol_a", 0))
        rk = A.risk(self.s, pair, now, tier=tier)
        if not rk["ok"] and tier != "normal":               # too big for today's loss room -> try normal size
            tier, tier_why = "normal", []
            rk = A.risk(self.s, pair, now)
        if not rk["ok"]:
            return reject("Risk", rk["reasons"], score=score, facts=rg["facts"], **why_x)
        L.note("picker", f"{sym} {score}点（合格{C.MIN_ENTRY_SCORE}）", sym, "OK")
        L.note("sizer", f"{A.TIER_JA[tier]} ${rk['size']:.0f}（{'・'.join(tier_why) or '資産の10%'}）", sym, "OK")
        L.step("work", "売却テスト中（本当に売れるか見積もりを取る・注文は出さない）…", sym, mint, agent="exec",
               note=f"{sym} 売却テスト中…")
        sc = self.sell_check(mint, rk["size"]) if C.FLOW_ON else {"ok": None, "reason": "売却テスト: オフ"}
        if sc["ok"] is False:
            return reject("Sell", [sc["reason"]], score=score, facts=rg["facts"], **why_x)
        why["sell"] = sc["reason"]
        price = S.pair_price(pair)
        fill, slip = P.paper_buy(self.s, mint, sym, rk["size"], price, rg["facts"]["liq"],
                                 f"点数{score}・{A.TIER_JA[tier]}", now, tier=tier, gap=rk.get("gap"))
        cand["status"] = "bought"
        cand.pop("reentry", None)
        day = P.hst_day(now)
        cnt = cand.get("trades_day") or [day, 0]
        cand["trades_day"] = [day, (cnt[1] if cnt[0] == day else 0) + 1]
        pos = self.s["positions"].get(mint)
        if pos is not None:
            pos["creator"] = rg["facts"].get("creator")
            pos["pool"] = (pair or {}).get("pairAddress")
        T.start(self.s, mint, sym, price, rg["facts"]["liq"], "BUY", [], score, now, facts=rg["facts"],
                pool=(pair or {}).get("pairAddress"))
        f = rg["facts"]
        N.send_async(f"BUY {sym}  ${rk['size']:.2f}" + ("" if tier == "normal" else f"  [{A.TIER_JA[tier]}]"),
                     ("".join(t + "\n" for t in tier_why)) +
                     f"点数{score} ・ 流動性 ${f.get('liq', 0):,.0f} ・ 上位10 {f.get('top10_pct')}%\n"
                     f"{' / '.join((sn['reasons'] + hy['reasons'])[:3])}\n（練習・本物のお金は動きません）",
                     tags=["green_circle"], click=f"https://dexscreener.com/solana/{mint}")
        P.log_decision("entry", mint, sym, "BUY", f"点数{score}・{rk['reasons'][0]}", price=fill,
                       facts=rg["facts"], **why)
        L.step("buy", f"練習買い ${rk['size']:.2f}（{A.TIER_JA[tier]}） ・ 点数{score} ・ 全審査に合格", sym, mint, "BUY", agent="exec",
               note=f"{sym} 練習買い ${rk['size']:.2f}・すべり{slip * 100:.1f}%")
        return "bought"

    def cycle(self, now=None):
        now = now or time.time()
        if C.KILL_FILE.exists():
            self.check_exits(now)
            return "killed"
        self.check_exits(now)
        try:
            self.follow_outcomes(now)
        except Exception as e:
            self._error("tracker", "", e)
        if not credits_ok(self.s):
            P.log_decision("risk", "", "", "PAUSE", "今月のHelius無料枠の上限に近いため新規の審査を停止")
            return "budget"
        due = self.scanner.due(self.s, now)
        if due:
            L.step("work", f"審査の順番が来たコイン {len(due)}件 → 1件ずつ調べます", log=False, agent="scout",
                   note=f"審査待ち{len(due)}件を順番に")
            radar = load_radar()
            pairs = self.fetch_pairs(due[:30])
            spent = 0
            for m in due[:30]:
                if spent >= C.MAX_EVALS_PER_CYCLE:
                    break                      # keep cycles short so exits are checked often
                try:
                    r = self.evaluate(m, pairs.get(m), radar, now)
                    if r not in ("skipped", "wait"):
                        spent += 1
                except Exception as e:
                    self.s["candidates"].get(m, {})["status"] = "error"
                    self._error("evaluate", m, e)
        self.alerts(now)
        self.heartbeat(now)
        L.idle(f"ひと回り完了 — {C.TICK_SEC:.0f}秒後に次の巡回（新しい卒業コインはずっと受信中）")
        return "ok"

    def follow_outcomes(self, now=None):
        """Verification engine: sample prices of every evaluated coin (free DexScreener calls)."""
        now = now or time.time()
        want = T.due(self.s, now)[:60]
        prices = {}
        if want:
            L.step("work", f"検証エンジン: 過去に審査した {len(want)}件の今の値段を記録中", log=False)
            pairs = self.fetch_pairs(want)
            prices = {m: S.pair_price(pairs.get(m)) if pairs.get(m) else None for m in want}
        T.update(self.s, prices, now)
        if C.FLOW_ON and now - self._last_refine >= C.REFINE_EVERY_SEC:
            self._last_refine = now
            try:
                T.refine(self.s, F.ohlcv, limit=1, now=now)      # 1-minute candles for finished coins
            except Exception as e:
                self._error("refine", "", e)

    def alerts(self, now=None):
        now = now or time.time()
        day = P.hst_day(now)
        sent = self.s.setdefault("alerted", {})
        if self.s.get("halted") and not sent.get("halted"):
            sent["halted"] = 1
            N.send_async("デスク完全停止", self.s["halted"], tags=["no_entry"], priority=5)
        if P.day_pnl(self.s, now) <= -C.DAILY_LOSS_LIMIT and sent.get("daily") != day:
            sent["daily"] = day
            N.send_async("今日の損失上限に到達", "ハワイ時間の明日まで新規購入を止めます（練習）",
                         tags=["pause_button"], priority=4)

    def summary_text(self):
        s = self.s
        lines = [f"残高 ${s['cash']:.2f} ・ 確定 {s['realized']:+.2f} ・ 今日 {P.day_pnl(s):+.2f}ドル"]
        for m, p in s["positions"].items():
            lp = p.get("last_price") or p["entry"]
            lines.append(f"  保有 {p['symbol']} {(lp / p['entry'] - 1) * 100:+.1f}%")
        st = s["stats"]
        rej = sorted(st.get("rejected", {}).items(), key=lambda x: -x[1])[:2]
        lines.append(f"審査 {st.get('evaluated', 0)} ・ 買い {st.get('bought', 0)} ・ 売り {st.get('sold', 0)}"
                     f" ・ エラー {st.get('errors', 0)}")
        if rej:
            lines.append("見送り多い理由: " + " / ".join(f"{k} {v}" for k, v in rej))
        try:
            js = J.stats(J.trades(s))
            if js["n"]:
                lines.append(f"成績 {js['n']}件 ・ 勝率 {js['win'] * 100:.0f}% ・ 1回平均 {js['avgR']:+.2f}R ・ 合計 {js['totalR']:+.2f}R")
        except Exception:
            pass
        lines += self.shortlist()
        outs = s.get("outcomes", [])
        if outs:
            win = sum(1 for o in outs if o["sim"] > 0) / len(outs)
            lines.append(f"検証 {len(outs)}件 ・ もし全部買っていたら勝率 {win * 100:.0f}%")
        try:
            meta = json.loads((C.DATA / "meta.json").read_text())
            ls = meta.get("last_stats", {})
            lines.append(f"レーダー: 累計 {meta.get('tokens_total', 0)} コイン調査 ・ 直近 {ls.get('checked', 0)}"
                         f"/{(ls.get('checked', 0) + ls.get('skipped', 0)) or 0} 成功")
        except Exception:
            pass
        return "\n".join(lines)

    def cockpit(self, now=None):
        """Numbers for the phone cockpit: balance header and up to 5 coin charts (5-minute samples)."""
        now = now or time.time()
        s = self.s
        val = s["cash"] + sum(p["cost"] * (p.get("last_price") or p["entry"]) / p["entry"]
                              for p in s["positions"].values())
        if now - self._jcache[0] > 60:
            try:
                self._jcache = (now, J.stats(J.trades(s), s.get("start_cash")))
            except Exception:
                self._jcache = (now, {"n": 0})
        js = self._jcache[1]
        coins, tr = [], s.get("tracking", {})
        for m, p in s["positions"].items():
            path = [x[1] for x in (tr.get(m) or {}).get("path", [])][-40:]
            lp = p.get("last_price") or p["entry"]
            coins.append({"s": p["symbol"], "m": m, "st": "保有中", "chg": round(lp / p["entry"] - 1, 4),
                          "p": lp, "path": (path + [lp])[-40:], "liq": p.get("liq")})
        names = {"BUY": "買った", "WATCH": "様子見", "REJECT": "見送り"}
        for m, t in sorted(tr.items(), key=lambda kv: -kv[1]["t0"]):
            if len(coins) >= 5:
                break
            if m in s["positions"] or len(t["path"]) < 2:
                continue
            path = [x[1] for x in t["path"]][-40:]
            coins.append({"s": t["symbol"], "m": m, "st": names.get(t["decision"], t["decision"]),
                          "chg": round(path[-1] / path[0] - 1, 4), "p": path[-1], "path": path, "liq": t.get("liq")})
        return {"head": {"balance": round(val, 2), "start": s.get("start_cash") or C.START_CASH, "n": js.get("n", 0),
                         "w": js.get("wins", 0), "l": js.get("n", 0) - js.get("wins", 0),
                         "open": len(s["positions"]), "max": C.MAX_OPEN_POSITIONS},
                "charts": coins[:5]}

    def shortlist(self, k=3):
        """Pre-filtered watchlist for the 30-min message: coins still in play, best first."""
        wait = [c for c in self.s["candidates"].values() if c.get("status") == "waiting"]
        scored = sorted((c for c in wait if c.get("score") is not None), key=lambda c: -c["score"])[:k]
        out = []
        if scored:
            out.append("有力候補: " + " / ".join(f"{c.get('symbol') or '?'} {c['score']}点" for c in scored)
                       + f"（合格 {C.MIN_ENTRY_SCORE}点）")
        fresh = len(wait) - len([c for c in wait if c.get("score") is not None])
        if fresh:
            out.append(f"まだ審査前のコイン {fresh}件")
        return out

    def heartbeat(self, now=None):
        self.s["last_heartbeat"] = int(now or time.time())
        mk = scanner.month_key()
        used = scanner.CALLS["n"] - self._calls0
        self._calls0 = scanner.CALLS["n"]
        self.s["credits"][mk] = self.s["credits"].get(mk, 0) + used
        self.s["rpc"] = scanner.RPC_KIND
        self.s["rpc_samples"] = dict(scanner.ERR_SAMPLES)
        P.save_state(self.s)
        try:
            L.status(**self.cockpit(now))
        except Exception:
            pass
        st = self.s["stats"]
        L.status(cash=round(self.s["cash"], 2), open=len(self.s["positions"]), evaluated=st.get("evaluated", 0),
                 bought=st.get("bought", 0), waiting=sum(1 for c in self.s["candidates"].values()
                                                         if c.get("status") == "waiting"))


# ------------------------------------------------------------ runners
async def run_loop(minutes):
    desk = Desk()
    until = time.time() + minutes * 60
    desk.resume()
    n = desk.scanner.backfill(desk.s)
    print(f"desk started; backfilled {n} graduated coins; open={len(desk.s['positions'])}", flush=True)

    desk.s["stats"]["backfilled"] = desk.s["stats"].get("backfilled", 0) + n
    url_file = C.DATA / "dashboard_url.txt"

    def dash():
        try:
            return url_file.read_text().strip() or None
        except Exception:
            return None

    gaps = desk.s.get("gaps") or []
    gap_txt = f"（{gaps[-1]['sec'] // 60}分止まっていました）" if gaps and time.time() - gaps[-1]["ts"] < 120 else ""
    N.send_async("デスク起動" + gap_txt, desk.summary_text(), tags=["arrow_forward"], click=dash())

    def day_report():
        try:
            J.maybe_day_report(desk.s, lambda t, b: N.send_async(t, b, tags=["crescent_moon"], click=dash()))
        except Exception as e:
            desk._error("day_report", "", e)

    def scout():
        n, now = 0, time.time()
        seen = {}
        for dur in ("5m", "1h"):
            for t in F.trending_mints(duration=dur):
                seen.setdefault(t["mint"], t)
        for t in seen.values():
            c = desk.s["candidates"].get(t["mint"])
            if c is not None:
                c["trending"] = t["rank"]
                c["refreshed"] = int(now)
                td = c.get("trades_day") or ["", 0]
                if (C.STRATEGY == "scalp" and c.get("status") in ("rejected", "expired", "skipped", "error")
                        and now - c.get("last_eval", c["seen"]) > C.SCALP_RECHECK_REJECTED_MIN * 60
                        and now >= c.get("hard_until", 0) and not c.get("reentry_used")
                        and not (td[0] == P.hst_day(now) and td[1] >= C.SCALP_MAX_TRADES_PER_COIN)):
                    c.update(status="waiting", checks=0, next_check=0)     # it trends again: look again
                    n += 1
            elif t["mint"] not in desk.s["positions"]:
                desk.s["candidates"][t["mint"]] = {"seen": int(time.time()), "grad": None, "symbol": t["symbol"],
                                                   "status": "waiting", "checks": 0, "trending": t["rank"]}
                n += 1
        if n:
            L.step("new", f"GeckoTerminalの注目ランキング（5分・1時間）から{n}件を審査待ちに追加", agent="scout",
                   note=f"注目ランキングから{n}件")
        else:
            L.note("scout", "注目ランキングを確認（新しい候補なし）")

    def check_sources():
        r = SC.run()
        desk.s["selfcheck"] = r
        bad = [k for k, v in r["results"].items() if not v["ok"]]
        N.send_async("データ窓口チェック  " + ("全部OK" if not bad else f"NG {len(bad)}件"), SC.text(r), click=dash())
        L.step("check" if not bad else "error", "データ窓口の自己点検: " + ("全部OK" if not bad else "NG " + "・".join(bad)))

    async def ticker():
        last_fill = time.time()
        last_sum = time.time()
        last_scout = 0
        last_check = time.time() - 6 * 3600 + 60          # first self check one minute after start
        while time.time() < until:
            day_report()
            if C.FLOW_ON and time.time() - last_check > 6 * 3600:
                last_check = time.time()
                try:
                    await asyncio.to_thread(check_sources)
                except Exception as e:
                    desk._error("selfcheck", "", e)
            if C.FLOW_ON and time.time() - last_scout > C.TRENDING_EVERY_SEC:
                last_scout = time.time()
                try:
                    await asyncio.to_thread(scout)
                except Exception as e:
                    desk._error("trending", "", e)
            if time.time() - last_sum > 1800:
                last_sum = time.time()
                N.send_async("30分ごとの状況（練習）", desk.summary_text(), tags=["bar_chart"], click=dash())
            if time.time() - last_fill > 600:
                last_fill = time.time()
                try:
                    k = await asyncio.to_thread(desk.scanner.backfill, desk.s)
                    desk.s["stats"]["backfilled"] = desk.s["stats"].get("backfilled", 0) + k
                except Exception as e:
                    desk._error("backfill", "", e)
            try:
                r = await asyncio.to_thread(desk.cycle)
            except Exception as e:             # a bug must never stop the desk
                desk._error("cycle", "", e)
                r = "error"
            if r == "killed":
                print("KILL file found -> stopping safely", flush=True)
                break
            await asyncio.sleep(C.TICK_SEC)

    def on_mig(m):
        desk.s["stats"]["migrations"] = desk.s["stats"].get("migrations", 0) + 1
        desk.scanner.on_migration(desk.s, m)
        L.step("new", "新しい卒業コインを受信 → 審査待ちリストに追加", (m or {}).get("symbol", ""),
               (m or {}).get("mint", ""), "NEW", agent="scout", note=f"新着 {(m or {}).get('symbol', '')} を受信")

    await asyncio.gather(S.listen_migrations(on_mig, until), ticker())
    desk.heartbeat()
    P.trim_log()
    print("desk stopped", json.dumps(desk.s["stats"], ensure_ascii=False), flush=True)


def cmd_status():
    s = P.load_state()
    lines = [f"現金 ${s['cash']:.2f} ・ 確定損益 ${s['realized']:+.2f} ・ 今日 ${P.day_pnl(s):+.2f}",
             f"保有 {len(s['positions'])}/{C.MAX_OPEN_POSITIONS} ・ 停止: {s['halted'] or 'なし'}"
             f"{' ・ KILLファイルあり' if C.KILL_FILE.exists() else ''}"]
    for m, p in s["positions"].items():
        lines.append(f"  {p['symbol']:<10} ${p['cost']:.2f} @ {p['entry']:.8f}  {m}")
    try:
        from rich import print as rp
        from rich.panel import Panel
        rp(Panel("\n".join(lines), title="Desk status (PAPER)"))
    except ImportError:
        print("\n".join(lines))


def cmd_summary():
    import sqlite3
    try:
        db = sqlite3.connect(C.DB_FILE)
        rows = db.execute("select side, count(*), sum(pnl) from trades group by side").fetchall()
        wins = db.execute("select count(*) from trades where side='SELL' and pnl>0").fetchone()[0]
        sells = db.execute("select count(*) from trades where side='SELL'").fetchone()[0]
    except Exception:
        rows, wins, sells = [], 0, 0
    s = P.load_state()
    print(f"買い/売り: {dict((r[0], r[1]) for r in rows)} ・ 勝率 {wins}/{sells} ・ 確定損益 ${s['realized']:+.2f}")
    print("見送り理由の上位:", sorted(s["stats"]["rejected"].items(), key=lambda x: -x[1])[:8])


def cmd_rug(mint):
    pair = S.dex_pairs([mint]).get(mint)
    res = A.rug_check(mint, pair, P.load_state(), load_radar())
    print(("合格" if res["ok"] else "不合格"), "\n - " + "\n - ".join(res["reasons"]))
    print("facts:", json.dumps(res.get("facts"), ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "status", "summary", "rug"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("DESK_MINUTES") or 50))
    a = ap.parse_args()
    if a.once:
        d = Desk(); d.resume(); print(d.cycle()); return
    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "summary":
        return cmd_summary()
    if a.cmd == "rug":
        return cmd_rug(a.arg)
    asyncio.run(run_loop(a.minutes))


if __name__ == "__main__":
    main()
