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
import scanner


_radar_cache = {"t": 0, "v": None}


def load_radar():
    """Sniper-radar output (rebuilt from run logs; cached 10 min)."""
    if _radar_cache["v"] is None or time.time() - _radar_cache["t"] > 600:
        try:
            wallets, tokens, _ = scanner.rebuild(C.DATA)
        except Exception:
            wallets, tokens = {}, []
        _radar_cache.update(t=time.time(), v={
            "bots": {w for w, v in wallets.items() if v.get("count", 0) >= 5},
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


class Desk:
    def __init__(self, state=None, fetch_pairs=S.dex_pairs, rug=A.rug_check):
        self.s = state or P.load_state()
        self.scanner = A.Scanner()
        self.fetch_pairs = fetch_pairs
        self.rug = rug
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
        pairs = self.fetch_pairs(list(self.s["positions"]))
        for mint in list(self.s["positions"]):
            try:
                self._exit_one(mint, pairs, now)
            except Exception as e:
                self._error("exit", mint, e)

    def _exit_one(self, mint, pairs, now):
        if True:
            pos = self.s["positions"][mint]
            pair = pairs.get(mint)
            price = S.pair_price(pair) if pair else None
            if not price:
                P.log_decision("exit", mint, pos["symbol"], "NO_PRICE", "価格を取得できず — 次の確認で再試行")
                return
            liq = (pair.get("liquidity") or {}).get("usd")
            pos["liq"] = liq or pos.get("liq")
            frac, why = A.exit_decision(pos, price, liq, now)
            if frac:
                chg = price / pos["entry"] - 1
                pnl = P.paper_sell(self.s, mint, frac, price, why, now)
                P.log_decision("exit", mint, pos["symbol"], "SELL", why, fraction=frac,
                               change_pct=round(chg * 100, 2), pnl=round(pnl, 2))

    def _error(self, where, mint, e):
        import traceback
        st = self.s["stats"]
        st["errors"] = st.get("errors", 0) + 1
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        P.log_decision("error", mint, "", "ERROR", f"[{where}] {msg}")
        print("ERROR", where, mint, msg, flush=True)
        traceback.print_exc()

    # ------------------------------------------------ entries
    def prefilter(self, mint, pair, now):
        """Free checks before spending any RPC credits."""
        if not pair:
            return "データ不足: DexScreenerにペアが無い"
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
            st = self.s["stats"].setdefault("prefiltered", {})
            key = re.sub(r"\$?\d[\d,\.]*", "N", bad.split("（")[0])
            st[key] = st.get(key, 0) + 1
            return "skipped"
        cand = self.s["candidates"].setdefault(mint, {"seen": int(now), "grad": None, "symbol": "",
                                                      "status": "waiting", "checks": 0})
        cand["checks"] += 1
        self.s["stats"]["evaluated"] += 1
        if pair and pair.get("pairCreatedAt"):
            cand["grad"] = pair["pairCreatedAt"] / 1000
            if (now - cand["grad"]) / 60 < C.MIN_AGE_AFTER_GRAD_MIN:
                cand["next_check"] = cand["grad"] + C.MIN_AGE_AFTER_GRAD_MIN * 60
                return "wait"
        sym = ((pair or {}).get("baseToken") or {}).get("symbol", cand.get("symbol", ""))
        cand["symbol"] = sym

        def reject(stage, reasons, **extra):
            cand["status"] = "rejected"
            key = re.sub(r"\$?\d[\d,\.]*", "N", reasons[0].split("（")[0]).replace("上位N", "上位10")[:32]
            self.s["stats"]["rejected"][key] = self.s["stats"]["rejected"].get(key, 0) + 1
            P.log_decision("entry", mint, sym, "REJECT", f"[{stage}] " + " / ".join(reasons), **extra)
            return "rejected"

        if cand.get("radar") and mint not in radar["tokens"]:
            radar["tokens"][mint] = cand["radar"]   # reuse the expensive early-buyer lookup
        rg = self.rug(mint, pair, self.s, radar, now)
        f = rg.get("facts") or {}
        if "block0" in f:
            cand["radar"] = {"block0": f["block0"], "snipers": f.get("snipers", 0)}
        u = (sym or "").upper().lstrip("$")
        if u and u not in self.s["seen_symbols"]:
            self.s["seen_symbols"][u] = mint
        if not rg["ok"]:
            return reject("Rug", rg["reasons"], facts=rg.get("facts"), notes=rg.get("notes"))
        sn, wh, hy = A.sniper(pair), A.whale(rg["facts"], cand), A.hype(pair)
        score = A.entry_score(sn, wh, hy)
        why = {"sniper": sn["reasons"], "whale": wh["reasons"], "hype": hy["reasons"], "score": score}
        if not sn["ok"] or score < C.MIN_ENTRY_SCORE:
            if cand["checks"] < 4 and sn["ok"] and score >= 40:
                cand["next_check"] = now + 300      # look again in 5 minutes
                P.log_decision("entry", mint, sym, "WATCH", f"点数{score}（{C.MIN_ENTRY_SCORE}未満）— 5分後にまた確認", **why)
                return "watch"
            return reject("Sniper", sn["reasons"] if not sn["ok"] else [f"点数{score}（{C.MIN_ENTRY_SCORE}未満）"], **why)
        rk = A.risk(self.s, pair, now)
        if not rk["ok"]:
            return reject("Risk", rk["reasons"], **why)
        price = S.pair_price(pair)
        fill, slip = P.paper_buy(self.s, mint, sym, rk["size"], price, rg["facts"]["liq"],
                                 f"点数{score}", now)
        cand["status"] = "bought"
        P.log_decision("entry", mint, sym, "BUY", f"点数{score}・{rk['reasons'][0]}", price=fill,
                       facts=rg["facts"], **why)
        return "bought"

    def cycle(self, now=None):
        now = now or time.time()
        if C.KILL_FILE.exists():
            self.check_exits(now)
            return "killed"
        self.check_exits(now)
        if not credits_ok(self.s):
            P.log_decision("risk", "", "", "PAUSE", "今月のHelius無料枠の上限に近いため新規の審査を停止")
            return "budget"
        due = self.scanner.due(self.s, now)
        if due:
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
        self.heartbeat(now)
        return "ok"

    def heartbeat(self, now=None):
        self.s["last_heartbeat"] = int(now or time.time())
        mk = scanner.month_key()
        used = scanner.CALLS["n"] - self._calls0
        self._calls0 = scanner.CALLS["n"]
        self.s["credits"][mk] = self.s["credits"].get(mk, 0) + used
        self.s["rpc"] = scanner.RPC_KIND
        self.s["rpc_samples"] = dict(scanner.ERR_SAMPLES)
        P.save_state(self.s)


# ------------------------------------------------------------ runners
async def run_loop(minutes):
    desk = Desk()
    until = time.time() + minutes * 60
    desk.resume()
    n = desk.scanner.backfill(desk.s)
    print(f"desk started; backfilled {n} graduated coins; open={len(desk.s['positions'])}", flush=True)

    desk.s["stats"]["backfilled"] = desk.s["stats"].get("backfilled", 0) + n

    async def ticker():
        while time.time() < until:
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
    print(("✅ 合格" if res["ok"] else "❌ 不合格"), "\n - " + "\n - ".join(res["reasons"]))
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
