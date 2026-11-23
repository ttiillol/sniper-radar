"""Rule tests for the paper desk. Run: pytest -q"""
import ast
import time
from pathlib import Path

import pytest

from desk import agents as A
from desk import config as C
from desk import portfolio as P
from desk import main as M

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DESK", tmp_path)
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(C, "DECISIONS_LOG", tmp_path / "d.jsonl")
    monkeypatch.setattr(C, "DB_FILE", tmp_path / "d.sqlite")
    monkeypatch.setattr(C, "KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(C, "LIVE_FILE", tmp_path / "live.json")
    monkeypatch.setattr(C, "FLOW_ON", False)           # no network in tests; flow tests turn it on with fakes
    P._db = None
    yield
    P._db = None


def pair(liq=80_000, age_min=30, price=0.001, sym="GOOD", m5=2.0, h1=40, buys=30, sells=15, h1tx=400):
    return {"dexId": "pumpswap", "priceUsd": str(price), "liquidity": {"usd": liq},
            "pairCreatedAt": (NOW - age_min * 60) * 1000, "baseToken": {"symbol": sym, "address": "M"},
            "priceChange": {"m5": m5, "h1": h1}, "txns": {"m5": {"buys": buys, "sells": sells},
                                                          "h1": {"buys": h1tx // 2, "sells": h1tx // 2}},
            "info": {"socials": [{"type": "twitter"}]}}


def good_rug_inputs(**over):
    supply = 1_000_000_000
    d = dict(minfo={"mint_authority": None, "freeze_authority": None, "supply": supply, "decimals": 6},
             holders=[{"owner": "POOL", "amount": 200_000_000, "is_wallet": False}] +
                     [{"owner": f"W{i}", "amount": 10_000_000, "is_wallet": True} for i in range(12)],
             rc={"creator": "DEV", "creatorBalance": 1_000_000, "markets": [{"lp": {"lpLockedPct": 100}}],
                 "risks": []},
             gp={"mintable": {"status": "0"}, "freezable": {"status": "0"}},
             early=[{"wallet": "X", "gap": 1}])
    d.update(over)
    return d


def rug(p=None, state=None, radar=None, **over):
    return A.rug_check("MINT", p if p is not None else pair(), state or P.new_state(),
                       radar or {"bots": set(), "tokens": {}}, now=NOW, rpc=lambda *a: None,
                       **good_rug_inputs(**over))


# ------------------------------------------------------------------ safety
def test_no_withdraw_or_transfer_code_anywhere():
    banned = ("withdraw", "transfer", "sendtransaction", "send_transaction", "sign_tx",
              "signtransaction", "keypair", "private_key", "secret_key")
    for f in list((ROOT / "desk").glob("*.py")) + [ROOT / "scanner.py"]:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert not any(b in node.name.lower() for b in banned), f"{f.name}: {node.name}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value.lower()
                assert "sendtransaction" not in v and "requestairdrop" not in v, f.name


def test_live_order_always_refuses():
    with pytest.raises(P.LiveTradingDisabled):
        P.execute_live_order("MINT", 10)


# ------------------------------------------------------------------ slippage / sizing
def test_slippage_constant_product():
    assert P.slippage(10, 80_000) == pytest.approx(10 / (40_000 + 10))
    assert P.slippage(1000, 30_000) > C.MAX_SLIPPAGE


def test_small_mode_size_and_cap():
    s = P.new_state()
    r = A.risk(s, pair(), NOW)
    assert r["ok"] and C.SMALL_MIN <= r["size"] <= C.SMALL_MAX
    assert r["size"] <= s["cash"] * max(C.MAX_POSITION_PCT, C.SMALL_MAX / s["cash"])


def test_risk_rejects_high_slippage():
    assert not A.risk(P.new_state(), pair(liq=600), NOW)["ok"]


def test_max_five_positions():
    s = P.new_state()
    for i in range(5):
        s["positions"][f"m{i}"] = {"cost": 1.0}
    r = A.risk(s, pair(), NOW)
    assert not r["ok"] and "上限" in r["reasons"][0]


def test_daily_loss_limit_hawaii_day():
    s = P.new_state()
    s["daily"][P.hst_day(NOW)] = -10.0
    assert not A.risk(s, pair(), NOW)["ok"]
    # next Hawaii day -> allowed again
    assert A.risk(s, pair(), NOW + 86400)["ok"]


def test_total_loss_full_stop():
    s = P.new_state()
    s["realized"] = -30.0
    assert not A.risk(s, pair(), NOW)["ok"]
    assert s["halted"]
    assert not A.risk(s, pair(), NOW + 5 * 86400)["ok"], "full stop is permanent"


def test_worst_case_gap_budget():
    s = P.new_state()
    s["daily"][P.hst_day(NOW)] = -6.0          # $4 room left today
    s["positions"]["a"] = {"cost": 6.0}         # worst case -$3 already reserved
    r = A.risk(s, pair(), NOW)                  # new $10 position worst case -$5 > $1 room
    assert not r["ok"] and "最大損失" in r["reasons"][0]


def test_kill_file_blocks_buys():
    C.KILL_FILE.write_text("stop")
    assert not A.risk(P.new_state(), pair(), NOW)["ok"]


# ------------------------------------------------------------------ rug rules
def test_rug_all_pass():
    r = rug()
    assert r["ok"], r["reasons"]


@pytest.mark.parametrize("kw,expect", [
    (dict(minfo={"mint_authority": "X", "freeze_authority": None, "supply": 10**9, "decimals": 6}), "mint権限"),
    (dict(minfo={"mint_authority": None, "freeze_authority": "X", "supply": 10**9, "decimals": 6}), "freeze権限"),
    (dict(rc={"creator": "DEV", "creatorBalance": 60_000_000, "markets": [{"lp": {"lpLockedPct": 100}}]}), "開発者"),
    (dict(rc={"creator": "DEV", "creatorBalance": 0, "markets": [{"lp": {"lpLockedPct": 80}}]}), "LPロック"),
    (dict(holders=[{"owner": f"W{i}", "amount": 40_000_000, "is_wallet": True} for i in range(10)]), "上位10"),
    (dict(rc=None), "RugCheck"),
    (dict(gp={"mintable": {"status": "1"}}), "GoPlus"),
    (dict(early=[{"wallet": f"B{i}", "gap": 0} for i in range(3)]), "まとめ買い"),
])
def test_rug_each_rule_fails(kw, expect, monkeypatch):
    if kw.get("rc", 1) is None:
        monkeypatch.setattr(A.S, "rugcheck", lambda m: None)
    r = rug(**kw)
    assert not r["ok"] and any(expect in x for x in r["reasons"]), r["reasons"]


def test_rug_liquidity_age_and_missing_pair():
    assert not rug(p=pair(liq=C.MIN_LIQ_USD - 1))["ok"]
    assert not rug(p=pair(age_min=10))["ok"]
    r = A.rug_check("MINT", None, P.new_state(), {"bots": set(), "tokens": {}}, now=NOW)
    assert not r["ok"] and "データ不足" in r["reasons"][0]


def test_rug_impersonation():
    assert not rug(p=pair(sym="BONK"))["ok"]
    s = P.new_state()
    s["seen_symbols"]["GOOD"] = {"mint": "OTHERMINT", "t": NOW - 86400}
    assert not rug(state=s)["ok"], "copy of a coin that graduated yesterday"
    s["seen_symbols"]["GOOD"] = {"mint": "OTHERMINT", "t": NOW - 30 * 86400}
    assert rug(state=s)["ok"], "same name a month ago is not a copy"


def test_rug_radar_bot_holdings():
    r = rug(radar={"bots": {"W0", "W1", "W2", "W3", "W4", "W5"}, "tokens": {}})
    assert not r["ok"] and any("ボット" in x for x in r["reasons"])


def test_onchain_failure_is_fail():
    def boom(*a):
        raise RuntimeError("down")
    r = A.rug_check("MINT", pair(), P.new_state(), {"bots": set(), "tokens": {}}, now=NOW, rpc=boom,
                    rc={"creator": "D", "creatorBalance": 0, "markets": [{"lp": {"lpLockedPct": 100}}]},
                    gp={}, early=[])
    assert not r["ok"] and any("データ不足" in x for x in r["reasons"])


# ------------------------------------------------------------------ exits
def pos(entry=1.0):
    return {"entry": entry, "peak": entry, "opened": NOW, "half_sold": False, "cost": 10, "qty": 10 / entry,
            "symbol": "T", "liq": 80_000}


def test_stop_loss_and_gap_fill_is_honest():
    p = pos()
    frac, why = A.exit_decision(p, 0.67, 80_000, NOW)       # gapped to -33%
    assert frac == 1.0 and "損切り" in why and "-33" in why


def test_ladder_then_breakeven_floor():
    p = pos()
    frac, why = A.exit_decision(p, 1.55, 80_000, NOW + 60)
    assert frac == pytest.approx(1 / 3) and "利益確定" in why and "引き上げ" in why, why
    assert A.exit_decision(p, 1.20, 80_000, NOW + 120)[0] == 0, "above the break-even floor -> hold"
    frac, why = A.exit_decision(p, 0.99, 80_000, NOW + 180)
    assert frac == 1.0 and "建値" in why, why


def test_stale_exit_option_and_moonbag():
    from desk.exits import step
    prof = dict(C.EXIT_PROFILE, stale_min=90)
    st = {"entry": 1.0, "peak": 1.0, "opened": NOW}
    assert step(st, 1.05, NOW + 91 * 60, prof)[0] == 0, "above entry: keep waiting"
    frac, why = step(st, 0.98, NOW + 92 * 60, prof)
    assert frac == 1.0 and "撤退" in why
    m = pos()                                            # moonbag keeps running far past +400%
    for t, px in enumerate([1.6, 2.6, 5.0, 9.0, 12.0], 1):
        A.exit_decision(m, px, 80_000, NOW + t * 60)
    assert m["sold"] == pytest.approx(2 / 3), "last third still held at 12x"
    frac, why = A.exit_decision(m, 12.0 * 0.64, 80_000, NOW + 600)
    assert frac == 1.0 and "トレーリング" in why


def test_small_win_locked_before_first_take_profit():
    p = pos()
    assert A.exit_decision(p, 1.30, 80_000, NOW + 60)[0] == 0
    frac, why = A.exit_decision(p, 1.30 * 0.87, 80_000, NOW + 120)   # 13% off a +30% peak
    assert frac == 1.0 and "トレーリング" in why and "+13" in why, why


# ------------------------------------------------------------------ desk loop
def test_resume_after_gap_checks_stops_immediately():
    s = P.new_state()
    s["last_heartbeat"] = NOW - 3600
    s["cash"] = 90
    s["positions"]["M"] = pos(entry=0.001)
    d = M.Desk(state=s, fetch_pairs=lambda mints: {"M": pair(price=0.0006)})
    d.resume(NOW)
    assert "M" not in s["positions"], "stop checked right after restart"
    assert s["gaps"] and s["gaps"][-1]["sec"] == 3600
    assert s["realized"] < -3.5, "loss booked at the gapped price, not at -6%"


def test_full_buy_path_and_state_roundtrip(monkeypatch):
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "GOOD", "status": "waiting", "checks": 0}
    good = lambda mint, p, st, radar, now: rug(p=p, state=st)
    d = M.Desk(state=s, fetch_pairs=lambda mints: {m: pair() for m in mints}, rug=good)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    assert d.cycle(NOW) == "ok"
    assert "M" in s["positions"] and s["cash"] < C.START_CASH
    s2 = P.load_state()
    assert "M" in s2["positions"] and s2["last_heartbeat"] == NOW
    logs = C.DECISIONS_LOG.read_text()
    assert '"BUY"' in logs


def test_kill_file_stops_loop():
    C.KILL_FILE.write_text("x")
    d = M.Desk(state=P.new_state(), fetch_pairs=lambda m: {})
    assert d.cycle(NOW) == "killed"


# ------------------------------------------------------------------ robustness (found by self-check)
def test_old_graduation_skipped_without_spending_credits(monkeypatch):
    s = P.new_state()
    s["candidates"]["OLD"] = {"seen": NOW - 1800, "grad": None, "symbol": "Z", "status": "waiting", "checks": 0}
    called = []
    d = M.Desk(state=s, fetch_pairs=lambda m: {"OLD": pair(age_min=60 * 24 * 400)},
               rug=lambda *a: called.append(1))
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert not called and s["candidates"]["OLD"]["status"] == "skipped"


def test_non_pumpswap_skipped(monkeypatch):
    p = pair(); p["dexId"] = "raydium"
    d = M.Desk(state=P.new_state(), rug=lambda *a: 1 / 0)
    assert d.evaluate("X", p, {"bots": set(), "tokens": {}}, NOW) == "skipped"


def test_bug_in_one_coin_never_stops_the_desk(monkeypatch):
    s = P.new_state()
    for m in ("A", "B"):
        s["candidates"][m] = {"seen": NOW - 1800, "grad": None, "symbol": m, "status": "waiting", "checks": 0}
    def rug(mint, *a):
        if mint == "A":
            raise KeyError("boom")
        return rug_ok(mint, *a)
    rug_ok = lambda mint, p, st, radar, now: globals()["rug"](p=p, state=st)
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: pair(sym=m + "X") for m in ms}, rug=rug)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    assert d.cycle(NOW) == "ok"
    assert s["candidates"]["A"]["status"] == "error" and "B" in s["positions"]
    assert s["stats"]["errors"] == 1


def test_evaluations_capped_per_cycle(monkeypatch):
    s = P.new_state()
    for i in range(10):
        s["candidates"][f"M{i}"] = {"seen": NOW - 1800, "grad": None, "symbol": "", "status": "waiting", "checks": 0}
    n = []
    def rug(*a):
        n.append(1)
        return {"ok": False, "reasons": ["x"], "facts": {}}
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: pair() for m in ms}, rug=rug)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert len(n) == C.MAX_EVALS_PER_CYCLE


def test_db_writes_from_many_threads():
    import threading
    errs = []
    def w(i):
        try:
            P.log_decision("t", f"m{i}", "S", "REJECT", "x")
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs


def test_new_tx_version_is_adopted(monkeypatch):
    import scanner
    monkeypatch.setitem(scanner.TX_VERSION, "max", 0)
    calls = []
    def fake(method, params):
        v = params[1]["maxSupportedTransactionVersion"]
        calls.append(v)
        if v < 1:
            raise scanner.RpcError("tx_version", "Transaction version (1) is not supported by the requesting client")
        return {"transaction": {"message": {"accountKeys": ["SIGNER"]}}, "meta": {}}
    signer, _ = scanner.tx_signer_and_delta("sig", "MINT", fake)
    assert signer == "SIGNER" and calls == [0, 1] and scanner.TX_VERSION["max"] == 1


def test_wallet_groups_found():
    import scanner
    toks = [{"early": [{"wallet": "A"}, {"wallet": "B"}, {"wallet": "C"}, {"wallet": f"X{i}"}]} for i in range(3)]
    toks += [{"early": [{"wallet": "A"}, {"wallet": "Z"}]}]
    g = scanner.wallet_groups(toks)
    assert g and set(g[0]["wallets"]) == {"A", "B", "C"} and g[0]["together"] == 3


# ------------------------------------------------------------------ verification engine
def test_simulate_uses_desk_exit_rules():
    from desk import tracker as T
    t0 = NOW
    stop = [[t0, 1.0], [t0 + 300, 0.97], [t0 + 600, 0.65], [t0 + 900, 2.0]]
    assert T.simulate(stop) == pytest.approx(-0.35 - T.FRICTION), "stopped out, later pump ignored"
    lad = [[t0, 1.0], [t0 + 300, 1.6], [t0 + 600, 3.0], [t0 + 900, 2.0]]
    assert T.simulate(lad) == pytest.approx(0.6 / 3 + 2.0 / 3 + 1.0 / 3 - T.FRICTION)


def test_tracker_lifecycle_and_summary():
    from desk import tracker as T
    s = P.new_state()
    T.start(s, "M1", "A", 1.0, 12_000, "REJECT", ["流動性 $8,000（$10,000未満）"], None, NOW)
    T.start(s, "M2", "B", 1.0, 40_000, "BUY", [], 70, NOW)
    for i in range(1, 80):
        now = NOW + i * 300
        T.update(s, {m: (1 + 0.02 * i if m == "M1" else 1 - 0.01 * i) for m in T.due(s, now)}, now)
    assert not s["tracking"] and len(s["outcomes"]) == 2
    sm = T.summarize(s["outcomes"])
    assert sm["reason"]["流動性 N"]["avg"] > 0 > sm["decision"]["BUY"]["avg"]
    assert set(sm["liq"]) == {"$10k〜30k", "$30k以上"}


def test_rejected_coins_are_tracked(monkeypatch):
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "X", "status": "waiting", "checks": 0}
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: pair() for m in ms},
               rug=lambda *a: {"ok": False, "reasons": ["流動性 $1"], "facts": {}})
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert "M" in s["tracking"] and s["tracking"]["M"]["decision"] == "REJECT"


# ------------------------------------------------------------------ keyless endpoint rotation
def test_rpc_skips_endpoint_that_blocks_a_method(monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "RPC_URLS", ["https://a.test", "https://b.test"])
    monkeypatch.setattr(scanner, "_blocked", set())
    monkeypatch.setattr(scanner, "_cool", {})
    monkeypatch.setattr(scanner, "MIN_INTERVAL", 0)
    hits = []
    def one(url, method, params):
        hits.append(url)
        if url == "https://a.test":
            raise scanner.RpcError("blocked", "Request blocked")
        return "OK"
    monkeypatch.setattr(scanner, "_one", one)
    assert scanner.rpc("getTokenLargestAccounts", []) == "OK"
    assert scanner.rpc("getTokenLargestAccounts", []) == "OK"
    assert hits == ["https://a.test", "https://b.test", "https://b.test"], "blocked endpoint remembered"


def test_holders_fall_back_to_rugcheck():
    from desk import sources as S
    import scanner
    def rpc(method, params):
        if method == "getTokenLargestAccounts":
            raise scanner.RpcError("blocked")
        return {"value": [{"owner": "11111111111111111111111111111111"}, {"owner": "SomeProgram"}]}
    rc = {"topHolders": [{"owner": "W1", "amount": "5000"}, {"owner": "POOL", "amount": "90000"}]}
    rows = S.top_holders("MINT", rpc, rc)
    assert [(r["owner"], r["amount"], r["is_wallet"]) for r in rows] == [("W1", 5000, True), ("POOL", 90000, False)]


# ------------------------------------------------------------------ notifications
def test_notify_disabled_without_topic(monkeypatch):
    from desk import notify as N
    monkeypatch.setattr(N, "TOPIC", "")
    called = []
    monkeypatch.setattr(N.requests, "post", lambda *a, **k: called.append(1))
    assert N.send("t", "m") is False and not called


def test_buy_and_stop_send_notifications(monkeypatch):
    from desk import notify as N
    sent = []
    monkeypatch.setattr(N, "send_async", lambda title, msg, **k: sent.append(title))
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "GOOD", "status": "waiting", "checks": 0}
    good = lambda mint, p, st, radar, now: rug(p=p, state=st)
    prices = {"p": 0.001}
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: pair(price=prices["p"]) for m in ms}, rug=good)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert any(t.startswith("BUY") for t in sent)
    prices["p"] = 0.0006                                   # -40% -> stop (live profile stops at -30%)
    d.cycle(NOW + 60)
    assert any(t.startswith("STOP") for t in sent)
    assert "残高" in d.summary_text()


# ------------------------------------------------------------------ rules from the bot survey
def _rc(**k):
    base = {"creator": "DEV", "creatorBalance": 0, "markets": [{"lp": {"lpLockedPct": 100}}], "risks": [],
            "totalHolders": 500, "topHolders": [], "creatorTokens": []}
    base.update(k)
    return base


def test_few_holders_rejected():
    r = rug(rc=_rc(totalHolders=40))
    assert not r["ok"] and any("保有者" in x for x in r["reasons"])
    assert rug(rc=_rc(totalHolders=500))["ok"]


def test_insiders_rejected():
    r = rug(rc=_rc(topHolders=[{"pct": 12, "insider": True}, {"pct": 9, "insider": True}]))
    assert not r["ok"] and any("インサイダー" in x for x in r["reasons"])


def test_serial_creator_rejected():
    dead = [{"mint": f"D{i}", "marketCap": 900} for i in range(4)]
    r = rug(rc=_rc(creatorTokens=dead))
    assert not r["ok"] and any("常習" in x for x in r["reasons"])
    alive = [{"mint": f"D{i}", "marketCap": 900_000} for i in range(4)]
    assert rug(rc=_rc(creatorTokens=alive))["ok"]


def test_snipers_and_bundles_still_holding():
    holders = [{"owner": "POOL", "amount": 200_000_000, "is_wallet": False},
               {"owner": "SNIPER", "amount": 60_000_000, "is_wallet": True},
               {"owner": "BUNDLE", "amount": 120_000_000, "is_wallet": True}] + \
              [{"owner": f"W{i}", "amount": 1_000_000, "is_wallet": True} for i in range(10)]
    early = [{"wallet": "SNIPER", "gap": 2}, {"wallet": "BUNDLE", "gap": 0}]
    r = rug(holders=holders, early=early)
    assert any("まとめ買い財布がまだ" in x for x in r["reasons"]), r["reasons"]
    assert r["facts"]["sniper_hold_pct"] == 6.0 and r["facts"]["bundle_hold_pct"] == 12.0


def test_exit_profiles_differ_on_same_path():
    from desk import tracker as T
    t0 = NOW
    # dips -10% (old -6% rule stops out), then runs to 3x
    path = [[t0, 1.0], [t0 + 300, 0.90], [t0 + 600, 1.5], [t0 + 900, 3.0], [t0 + 1200, 2.0]]
    a = T.simulate_profile(path, T.PROFILES["A_旧ルール(-6%)"])
    b = T.simulate_profile(path, T.PROFILES["B_業界標準(-35%,2倍で半分)"])
    e = T.simulate_profile(path, T.PROFILES["E_ハイブリッド(採用中)"])
    assert a < 0 < b and e > 0, (a, b, e)
    assert e == pytest.approx(T.simulate(path)), "E is the live profile"




# ------------------------------------------------------------------ live "now doing" feed (display only)
def test_live_feed_shows_each_step(monkeypatch):
    import json as _j
    from desk import live as L
    from desk import notify as N
    L.reset()
    monkeypatch.setattr(N, "send_async", lambda *a, **k: None)
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "GOOD", "status": "waiting", "checks": 0}
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: pair() for m in ms},
               rug=lambda mint, p, st, radar, now: rug(p=p, state=st))
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    live = _j.loads(C.LIVE_FILE.read_text())
    results = [f["r"] for f in live["feed"]]
    assert "BUY" in results and any(f["s"] == "GOOD" for f in live["feed"])
    assert any("審査中" in f["x"] for f in live["feed"])        # the "checking this coin now" step
    assert live["now"]["icon"] == "idle" and live["status"]["open"] == 1


def test_live_feed_can_never_stop_the_desk(monkeypatch, tmp_path):
    from desk import live as L
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(C, "LIVE_FILE", blocker / "sub" / "live.json")   # impossible path
    L.step("work", "test", "SYM", "MINT")                                  # must not raise
    L.idle("wait")
    L.flush()


# ------------------------------------------------------------------ trade journal (R multiples) + daily report
def _round_trip(s, mint, sym, cost, sell_price, entry_price=1.0):
    P.paper_buy(s, mint, sym, cost, entry_price, 10_000_000, "t", NOW)   # huge pool -> ~no slippage
    return P.paper_sell(s, mint, 1.0, sell_price, "t", NOW + 600)


def test_journal_r_multiples_and_stats():
    from desk import journal as J
    s = P.new_state()
    _round_trip(s, "W", "WIN", 10, 1.5)        # about +46% after fees
    _round_trip(s, "L", "LOSE", 10, 0.7)       # about -31% (the stop)
    tr = J.trades(s)
    assert [t["symbol"] for t in tr] == ["WIN", "LOSE"]
    assert tr[0]["R"] > 1.4 and -1.1 < tr[1]["R"] < -0.9       # 1R = $3 on a $10 buy
    st = J.stats(tr)
    assert st["n"] == 2 and st["wins"] == 1 and st["win"] == 0.5
    assert st["pf"] > 1 and st["mdd"] > 0 and len(st["curve"]) == 2
    assert st["avgR"] == round((tr[0]["R"] + tr[1]["R"]) / 2, 2)


def test_journal_ignores_open_and_partial_positions():
    from desk import journal as J
    s = P.new_state()
    P.paper_buy(s, "O", "OPEN", 10, 1.0, 10_000_000, "t", NOW)
    P.paper_sell(s, "O", 1 / 3, 1.5, "t", NOW + 60)            # partial sell, still holding
    assert J.trades(s) == [] and J.stats([])["n"] == 0


def test_day_report_sent_once_after_hawaii_date_changes():
    from desk import journal as J
    s = P.new_state()
    sent = []
    send = lambda t, b: sent.append((t, b))
    day1 = 1_800_000_000
    assert not J.maybe_day_report(s, send, day1)                 # first call only remembers the day
    assert not J.maybe_day_report(s, send, day1 + 60)
    assert J.maybe_day_report(s, send, day1 + 86_400)            # next day -> one report
    assert not J.maybe_day_report(s, send, day1 + 86_400 + 60)
    assert len(sent) == 1 and "まとめ" in sent[0][0] and "残高" in sent[0][1]


def test_summary_lists_shortlist_candidates():
    s = P.new_state()
    s["candidates"]["A"] = {"symbol": "AAA", "status": "waiting", "score": 55, "checks": 1}
    s["candidates"]["B"] = {"symbol": "BBB", "status": "waiting", "checks": 0}
    s["candidates"]["C"] = {"symbol": "CCC", "status": "rejected", "score": 90, "checks": 1}
    txt = M.Desk(state=s).summary_text()
    assert "有力候補: AAA 55点" in txt and "CCC" not in txt and "審査前のコイン 1件" in txt


def test_desk_page_builds_with_journal(tmp_path):
    from desk import page
    s = P.new_state()
    _round_trip(s, "W", "WIN", 10, 1.5)
    P.save_state(s)
    page.build(tmp_path)
    html = (tmp_path / "desk.html").read_text()
    assert "成績（R倍数）" in html and '"journal":{"n":1' in html and "検証 vs 実際" in html


# ------------------------------------------------------------------ order-flow agents (12-agent desk ideas)
def _tr(t, kind, usd, wallet):
    return {"t": t, "kind": kind, "usd": usd, "wallet": wallet}


def test_flow_numbers_and_score():
    from desk import flow as F
    now = NOW
    tr = [_tr(now - 30, "buy", 12_000, "W1"), _tr(now - 50, "buy", 15_000, "W2"), _tr(now - 80, "buy", 14_000, "W3")]
    tr += [_tr(now - 100 - i, "buy", 50, f"b{i}") for i in range(35)] + [_tr(now - 200, "sell", 400, "s1")]
    tr += [_tr(now - 3600, "buy", 9_999, "OLD")]                      # outside the 10-minute window
    f = F.analyze(tr, now, creator="DEV", kols={"b3"})
    assert f["burst_wallets"] == 3 and f["burst_usd"] == 41_000      # "three whales, $41k in 2 minutes"
    assert f["buyers"] == 38 and f["kol_buys"] == ["b3"] and f["dev_sold_usd"] == 0
    v = F.score(f)
    assert v["ok"] and v["score"] >= 90
    dev = F.score(F.analyze(tr + [_tr(now - 10, "sell", 300, "DEV")], now, creator="DEV"))
    assert not dev["ok"] and "作成者が売っている" in dev["reasons"][0]
    assert F.score(None)["ok"] and F.score(None)["score"] == 50       # no data -> neutral, never blocks


def _flow_desk(monkeypatch, trades, sell=None):
    from desk import notify as N
    monkeypatch.setattr(C, "FLOW_ON", True)
    monkeypatch.setattr(N, "send_async", lambda *a, **k: None)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "GOOD", "status": "waiting", "checks": 0}
    prices = {"p": 0.001}
    mk = lambda: dict(pair(price=prices["p"]), pairAddress="POOL")
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: mk() for m in ms},
               rug=lambda mint, p, st, radar, now: A.rug_check(mint, p, st, radar, now=NOW, rpc=lambda *a: None,
                                                               **good_rug_inputs()),
               trades=lambda pool: trades["v"], sell_check=sell or (lambda m, usd: {"ok": True, "reason": "ok"}))
    return d, s, prices


def test_dev_selling_coin_is_rejected(monkeypatch):
    trades = {"v": [_tr(NOW - 20, "sell", 500, "DEV")]}
    d, s, _ = _flow_desk(monkeypatch, trades)
    assert d.evaluate("M", dict(pair(), pairAddress="POOL"), {"bots": set(), "tokens": {}}, NOW) == "rejected"
    assert not s["positions"]


def test_failed_sell_simulation_blocks_buy(monkeypatch):
    trades = {"v": [_tr(NOW - 20, "buy", 100, f"b{i}") for i in range(20)]}
    d, s, _ = _flow_desk(monkeypatch, trades, sell=lambda m, usd: {"ok": False, "reason": "売却テスト失敗: 売る経路がない"})
    d.cycle(NOW)
    assert not s["positions"] and s["candidates"]["M"]["status"] == "rejected"


def test_dev_sell_while_holding_exits_at_once(monkeypatch):
    trades = {"v": [_tr(NOW - 20, "buy", 100, f"b{i}") for i in range(20)]}
    d, s, _ = _flow_desk(monkeypatch, trades)
    d.cycle(NOW)
    assert "M" in s["positions"] and s["positions"]["M"]["creator"] == "DEV"
    trades["v"] = trades["v"] + [_tr(NOW + 100, "sell", 2_000, "DEV")]
    d.cycle(NOW + 120)
    assert "M" not in s["positions"] and s["candidates"]["M"]["reentry_used"]


def test_reentry_only_when_whales_pile_in(monkeypatch):
    trades = {"v": [_tr(NOW - 20, "buy", 100, f"b{i}") for i in range(20)]}
    d, s, prices = _flow_desk(monkeypatch, trades)
    d.cycle(NOW)
    assert "M" in s["positions"]
    prices["p"] = 0.0006                                   # -40% -> stop
    d.cycle(NOW + 60)
    c = s["candidates"]["M"]
    assert "M" not in s["positions"] and c["status"] == "waiting" and c["reentry"]
    # 2 minutes later, no whales -> no second answer
    d.cycle(NOW + 60 + C.REENTRY_WAIT_SEC)
    assert "M" not in s["positions"] and c["status"] == "rejected"
    # a fresh coin state where three whales bought within 2 minutes -> re-buy
    c.update(status="waiting", next_check=0)
    t = NOW + 400
    trades["v"] = [_tr(t - 10, "buy", 13_000, "A"), _tr(t - 40, "buy", 14_000, "B"), _tr(t - 70, "buy", 14_000, "C")] + \
                  [_tr(t - 90 - i, "buy", 60, f"n{i}") for i in range(20)]
    d.cycle(t)
    assert "M" in s["positions"] and s["stats"]["bought"] == 2


def test_profile_f_from_the_post_is_compared():
    from desk import tracker as T
    f = T.PROFILES["F_2倍3倍利確(-12%)"]
    path = [[0, 1.0], [60, 0.87]]                           # -13% -> the -12% stop
    assert T.simulate_profile(path, f) < -0.12
    path = [[0, 1.0], [60, 2.05], [120, 3.1], [180, 2.2]]   # 2x and 3x ladders, then a 25%+ trail
    assert T.simulate_profile(path, f) > 1.0


def test_low_score_coin_is_rejected_not_an_error(monkeypatch):
    """Regression: 'score' was passed twice, so every low-score reject crashed and was counted as an error."""
    from desk import notify as N
    monkeypatch.setattr(N, "send_async", lambda *a, **k: None)
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 1800, "grad": None, "symbol": "MEH", "status": "waiting", "checks": 9}
    weak = pair(m5=-3, h1=-10, buys=5, sells=20, h1tx=40, sym="MEH")
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: weak for m in ms},
               rug=lambda mint, p, st, radar, now: rug(p=p, state=st))
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert s["candidates"]["M"]["status"] == "rejected" and s["stats"].get("errors", 0) == 0
