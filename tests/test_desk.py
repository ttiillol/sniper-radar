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


@pytest.fixture
def sniper(monkeypatch):
    """Old strategy (fresh pump.fun graduates only) - its rules are kept and still tested."""
    monkeypatch.setattr(C, "STRATEGY", "sniper")
    monkeypatch.setattr(C, "EXIT_PROFILE", C.EXIT_PROFILE_E)


@pytest.fixture
def profile_s(monkeypatch):
    monkeypatch.setattr(C, "EXIT_PROFILE", C.EXIT_PROFILE_S)


@pytest.fixture
def profile_e(monkeypatch):
    monkeypatch.setattr(C, "EXIT_PROFILE", C.EXIT_PROFILE_E)


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


def test_size_is_a_share_of_the_whole_book():
    s = P.new_state()
    assert s["cash"] == 1000 and s["book"] == C.BOOK
    r = A.risk(s, pair(), NOW)
    assert r["ok"] and r["size"] == pytest.approx(100)                     # 10% of $1,000
    s["cash"] -= 300
    s["positions"]["a"] = {"cost": 300.0}
    assert A.risk(s, pair(), NOW)["size"] == pytest.approx(100), "same size while money is in positions"


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
    s["daily"][P.hst_day(NOW)] = -C.DAILY_LOSS_LIMIT
    assert not A.risk(s, pair(), NOW)["ok"]
    # next Hawaii day -> allowed again
    assert A.risk(s, pair(), NOW + 86400)["ok"]


def test_total_loss_full_stop():
    s = P.new_state()
    s["realized"] = -C.TOTAL_LOSS_LIMIT
    assert not A.risk(s, pair(), NOW)["ok"]
    assert s["halted"]
    assert not A.risk(s, pair(), NOW + 5 * 86400)["ok"], "full stop is permanent"


def test_worst_case_gap_budget():
    s = P.new_state()
    s["daily"][P.hst_day(NOW)] = -60.0         # $40 room left today
    s["positions"]["a"] = {"cost": 100.0}       # worst case -$25 already reserved
    r = A.risk(s, pair(), NOW)                  # new $100 position worst case -$25 > $15 room
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


def test_ladder_then_breakeven_floor(profile_e):
    p = pos()
    frac, why = A.exit_decision(p, 1.55, 80_000, NOW + 60)
    assert frac == pytest.approx(1 / 3) and "利益確定" in why and "引き上げ" in why, why
    assert A.exit_decision(p, 1.20, 80_000, NOW + 120)[0] == 0, "above the break-even floor -> hold"
    frac, why = A.exit_decision(p, 0.99, 80_000, NOW + 180)
    assert frac == 1.0 and "建値" in why, why


def test_stale_exit_option_and_moonbag(profile_e):
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


def test_small_win_locked_before_first_take_profit(profile_e):
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
def test_old_graduation_skipped_without_spending_credits(monkeypatch, sniper):
    s = P.new_state()
    s["candidates"]["OLD"] = {"seen": NOW - 1800, "grad": None, "symbol": "Z", "status": "waiting", "checks": 0}
    called = []
    d = M.Desk(state=s, fetch_pairs=lambda m: {"OLD": pair(age_min=60 * 24 * 400)},
               rug=lambda *a: called.append(1))
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    d.cycle(NOW)
    assert not called and s["candidates"]["OLD"]["status"] == "skipped"


def test_non_pumpswap_skipped(monkeypatch, sniper):
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
def test_simulate_uses_desk_exit_rules(profile_e):
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
    e = T.simulate_profile(path, T.PROFILES["E_ハイブリッド(旧)"])
    sc = T.simulate_profile(path, T.PROFILES["S_勢いスキャル(旧)"])
    r = T.simulate_profile(path, T.PROFILES["R_ランナー(採用中)"])
    assert a < 0 < b and e > 0, (a, b, e)
    assert sc < 0, "the scalp's shallow stop gives up a dip-then-runner (its known trade-off)"
    assert r > 0.5, "the runner survives a -10% dip and rides the move"
    assert r == pytest.approx(T.simulate(path)), "R is the live profile"




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
    one_r = 10 * J.RISK_PCT                                   # 1R = stop distance x amount bought
    assert tr[0]["R"] == pytest.approx(tr[0]["pnl"] / one_r, abs=0.02) and tr[0]["R"] > 0 > tr[1]["R"]
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


def test_reentry_only_when_whales_pile_in(monkeypatch, sniper):
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


def test_low_score_coin_is_rejected_not_an_error(monkeypatch, sniper):
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


# ------------------------------------------------------------------ real response shapes (captured 2026-11-23 from the live APIs)
GT_TRADES = {"data": [
    {"id": "x1", "type": "trade", "attributes": {"block_number": 1, "block_timestamp": "2026-11-23T21:27:29Z",
     "tx_hash": "h1", "tx_from_address": "7Yyq8Wmiw8ovTS5kTEusmJzri1SZ31fZ6rSfeqWqdVK8", "kind": "sell",
     "volume_in_usd": "100.0583658897431685636330978887168607042492752649252939537267904602552", "is_anomalous": False}},
    {"id": "x2", "type": "trade", "attributes": {"block_number": 1, "block_timestamp": "2026-11-23T21:27:29Z",
     "tx_hash": "h2", "tx_from_address": "55823rbXDfAuqUUHNEc9zHf7fuZ3rFuR3a7uPqR1KAiv", "kind": "buy",
     "volume_in_usd": "7.21381845140905961079608053233841778783865665007072805103618454", "is_anomalous": False}}]}
GT_TRENDING = {"data": [
    {"id": "p1", "attributes": {"name": "FIONA / SOL", "address": "DjbLtX1HRGZZrJgJ7CwTX6SN3D7tBLiytkGwomBUwTVB"},
     "relationships": {"dex": {"data": {"id": "pumpswap"}},
                       "base_token": {"data": {"id": "solana_B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump"}}}},
    {"id": "p2", "attributes": {"name": "PEPE / SOL", "address": "HDoj"},
     "relationships": {"dex": {"data": {"id": "meteora"}},
                       "base_token": {"data": {"id": "solana_PEPEqnuuCDbBC89p1u9vpnP1KQ2oj1xTcQBsjt9X55m"}}}}]}
JUP_QUOTE = {"inputMint": "B34n", "inAmount": "1000000000", "outputMint": "So11111111111111111111111111111111111111112",
             "outAmount": "34856016", "swapMode": "ExactIn", "slippageBps": 300,
             "priceImpactPct": "0.0081375400943202930239781645",      # a FRACTION incl. fees (0.81%)
             "routePlan": [{"swapInfo": {"label": "Meteora DLMM"}}]}


def test_real_shapes_geckoterminal(monkeypatch):
    from desk import flow as F
    monkeypatch.setattr(F, "_last_call", {"t": 0.0})
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: GT_TRENDING if "trending" in url else GT_TRADES)
    tr = F.pool_trades("DjbLtX1HRGZZrJgJ7CwTX6SN3D7tBLiytkGwomBUwTVB")
    assert [x["kind"] for x in tr] == ["sell", "buy"] and round(tr[1]["usd"], 2) == 7.21
    assert tr[0]["wallet"] == "7Yyq8Wmiw8ovTS5kTEusmJzri1SZ31fZ6rSfeqWqdVK8" and tr[0]["t"] == 1795469249
    tm = F.trending_mints()                                   # scalp: any DEX
    assert [t["symbol"] for t in tm] == ["FIONA", "PEPE"] and tm[0]["mint"].endswith("pump")
    monkeypatch.setattr(C, "STRATEGY", "sniper")              # sniper: PumpSwap only
    assert [t["symbol"] for t in F.trending_mints()] == ["FIONA"]


def test_real_shapes_jupiter_sell_check(monkeypatch):
    from desk import flow as F
    monkeypatch.setattr(F.S, "dex_pairs", lambda ms: {ms[0]: {"priceUsd": "0.0049"}})
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: JUP_QUOTE)
    assert F.sell_check("B34n", 10)["ok"] is True
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: dict(JUP_QUOTE, priceImpactPct="0.083"))
    assert F.sell_check("B34n", 10)["ok"] is False
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: {"error": "No routes found", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
    assert F.sell_check("B34n", 10)["ok"] is False                    # no route -> honeypot suspicion
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: None)
    assert F.sell_check("B34n", 10)["ok"] is None                     # API down -> not a fail


def test_selfcheck_report(monkeypatch):
    from desk import flow as F
    from desk import selfcheck as SC
    monkeypatch.setattr(F, "_last_call", {"t": 0.0})
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: GT_TRENDING if "trending" in url else GT_TRADES if "trades" in url else JUP_QUOTE)
    monkeypatch.setattr(SC.S, "dex_pairs", lambda ms: {ms[0]: {"pairAddress": "P", "priceUsd": "0.0049", "liquidity": {"usd": 52000}}})
    monkeypatch.setattr(SC.S, "rugcheck", lambda m: {"score": 1})
    r = SC.run()
    assert r["coin"] == "FIONA" and all(v["ok"] for v in r["results"].values())
    assert "OK  Jupiter売却テスト" in SC.text(r)


# ------------------------------------------------------------------ KOL benchmark
def test_kol_list_file_is_valid():
    import re as _re
    from desk import flow as F
    F._kol_cache.update(t=0.0, v={})
    k = F.load_kols()
    assert len(k) == 51 and sum(1 for v in k.values() if v["tier"] == "A") == 20
    assert k["AVAZvHLR2PcWpDf8BXY4rVxNHYRBytycHkcB5z5QNXYm"] == {"tier": "W", "name": "Ansem(とされる)"}
    b58 = _re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    assert all(b58.match(a) for a in k) and k["862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"]["name"] == "Wugi"


def test_kol_tiers_weight_the_score():
    from desk import flow as F
    kols = {"A1": {"tier": "A", "name": "Wugi"}, "B1": {"tier": "B", "name": "ban"}, "B2": {"tier": "B", "name": "decu"}}
    base = [_tr(NOW - 10 - i, "buy", 40, f"x{i}") for i in range(10)]
    s0 = F.score(F.analyze(base, NOW, kols=kols))["score"]
    f = F.analyze(base + [_tr(NOW - 5, "buy", 300, "A1"), _tr(NOW - 6, "buy", 300, "B1")], NOW, kols=kols)
    assert f["kol_names"] == ["Wugi", "ban"] and f["kol_a"] == 1
    v = F.score(f)
    assert v["score"] == s0 + 14 and "Wugi" in v["reasons"][-1]


def test_kol_buys_are_recorded_for_the_benchmark(monkeypatch):
    from desk import flow as F
    monkeypatch.setattr(F, "load_kols", lambda: {"K1": {"tier": "A", "name": "xander"}})
    trades = {"v": [_tr(NOW - 20, "buy", 100, f"b{i}") for i in range(20)] + [_tr(NOW - 30, "buy", 800, "K1")]}
    d, s, _ = _flow_desk(monkeypatch, trades)
    d.cycle(NOW)
    ks = s["kol_seen"]
    assert ks[-1]["names"] == ["xander"] and ks[-1]["symbol"] == "GOOD" and ks[-1]["a"] == 1
    assert s["tracking"]["M"]["facts"]["kol_n"] == 1


# ------------------------------------------------------------------ cockpit (12 agents, charts, ladder) on the phone page
def test_cockpit_live_data(monkeypatch):
    import json as _j
    from desk import live as L
    L.reset()
    trades = {"v": [_tr(NOW - 20 - i, "buy", 100, f"b{i}") for i in range(20)] + [_tr(NOW - 15, "buy", 3_000, "W1"),
                                                                                   _tr(NOW - 40, "sell", 900, "S1")]}
    d, s, _ = _flow_desk(monkeypatch, trades)
    d.cycle(NOW)
    live = _j.loads(C.LIVE_FILE.read_text())
    ag = live["agents"]
    assert {"rug", "bundle", "whale", "kol", "flow", "picker", "sizer", "exec"} <= set(ag)
    assert set(ag) <= set(L.AGENTS) and "練習買い" in ag["exec"]["x"]
    st = live["status"]
    assert st["head"]["open"] == 1 and st["head"]["balance"] == pytest.approx(1000, abs=5)
    assert st["charts"][0]["st"] == "保有中" and len(st["charts"][0]["path"]) >= 1
    fl = st["flow"]
    assert fl["symbol"] == "GOOD" and fl["wb"] == 1 and fl["ws"] == 1 and len(fl["ladder"]) == 5
    assert sum(r["nb"] for r in fl["ladder"]) == 21 and fl["whales"][0]["k"] == "buy"


def test_desk_page_has_the_cockpit(tmp_path):
    from desk import page
    P.save_state(P.new_state())
    page.build(tmp_path)
    html = (tmp_path / "desk.html").read_text()
    for part in ('id="hub"', 'id="charts"', 'id="lad"', 'id="radar"', 'id="term"', "Meme Desk", "BALANCE"):
        assert part in html


# ------------------------------------------------------------------ momentum scalp strategy (live since 2026-11-23)
def _scalp_desk(monkeypatch, pr=None, trades=None, rug_calls=None):
    from desk import notify as N
    monkeypatch.setattr(C, "FLOW_ON", True)
    monkeypatch.setattr(N, "send_async", lambda *a, **k: None)
    monkeypatch.setattr(M, "credits_ok", lambda st: True)
    monkeypatch.setattr(M, "load_radar", lambda: {"bots": set(), "tokens": {}})
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 60, "grad": None, "symbol": "GOOD", "status": "waiting", "checks": 0}
    st = {"pair": pr or dict(pair(age_min=60 * 24 * 30), dexId="meteora", pairAddress="POOL")}
    calls = rug_calls if rug_calls is not None else []

    def rugf(mint, p, state, radar, now):
        calls.append(now)
        return A.rug_check(mint, p, state, radar, now=NOW, rpc=lambda *a: None, early=[], **{
            k: v for k, v in good_rug_inputs().items() if k != "early"})
    tv = trades or {"v": [_tr(NOW - 20 - i, "buy", 120, f"b{i}") for i in range(25)] + [_tr(NOW - 30, "sell", 300, "s")]}
    d = M.Desk(state=s, fetch_pairs=lambda ms: {m: st["pair"] for m in ms}, rug=rugf,
               trades=lambda pool: tv["v"], sell_check=lambda m, usd: {"ok": True, "reason": "ok"})
    return d, s, st, calls


def test_scalp_trades_an_old_non_pump_coin_with_momentum(monkeypatch):
    d, s, _, _ = _scalp_desk(monkeypatch)                    # 30-day-old meteora pool, +2% in 5 min, +40% in 1h
    d.cycle(NOW)
    assert "M" in s["positions"]


def test_scalp_prefilter_only_checks_liquidity():
    m = M.Desk(state=P.new_state())
    assert m.prefilter("M", dict(pair(age_min=99_999), dexId="raydium"), NOW) is None
    assert "流動性" in m.prefilter("M", pair(liq=8_000), NOW)


def test_weak_momentum_waits_a_minute_and_spikes_are_not_chased(monkeypatch):
    d, s, st, _ = _scalp_desk(monkeypatch)
    st["pair"] = dict(st["pair"], priceChange={"m5": 0.2, "h1": 5})
    d.cycle(NOW)
    c = s["candidates"]["M"]
    assert not s["positions"] and c["status"] == "waiting" and c["next_check"] == NOW + C.SCALP_RECHECK_SEC
    assert A.momentum(dict(pair(), priceChange={"m5": 40, "h1": 80}))["ok"] is False      # vertical spike
    assert A.momentum(dict(pair(), priceChange={"m5": 3, "h1": -5}))["ok"] is False       # hour is down
    sell_heavy = F_analyze_sell_heavy()
    assert A.momentum(pair(), sell_heavy)["ok"] is False                                   # sellers in control


def F_analyze_sell_heavy():
    from desk import flow as F
    return F.analyze([_tr(NOW - 10, "buy", 100, "a"), _tr(NOW - 20, "sell", 900, "b")], NOW)


def test_rug_check_is_reused_while_waiting_for_momentum(monkeypatch):
    calls = []
    d, s, st, _ = _scalp_desk(monkeypatch, rug_calls=calls)
    st["pair"] = dict(st["pair"], priceChange={"m5": 0.2, "h1": 5})
    d.cycle(NOW)
    st["pair"] = dict(st["pair"], priceChange={"m5": 2.5, "h1": 30})
    d.cycle(NOW + C.SCALP_RECHECK_SEC)
    assert "M" in s["positions"] and len(calls) == 1


def test_scalp_reenters_after_cooldown_up_to_three_times_a_day(monkeypatch):
    d, s, st, _ = _scalp_desk(monkeypatch)
    t = NOW
    for i in range(3):
        d.cycle(t)
        assert "M" in s["positions"], i
        st["pair"] = dict(st["pair"], priceUsd=str(0.001 * 0.85))            # -15% -> stop
        d.cycle(t + 30)
        assert "M" not in s["positions"]
        c = s["candidates"]["M"]
        if i < 2:
            assert c["status"] == "waiting" and c["next_check"] == t + 30 + C.SCALP_COOLDOWN_SEC
        st["pair"] = dict(st["pair"], priceUsd="0.001")
        t += 30 + C.SCALP_COOLDOWN_SEC
    assert s["candidates"]["M"]["status"] != "waiting" and s["stats"]["bought"] == 3


def test_exit_profile_s_rules(profile_s):
    def pos():
        return {"entry": 1.0, "peak": 1.0, "opened": NOW, "qty": 10, "cost": 10, "symbol": "X"}
    p = pos()
    assert A.exit_decision(p, 0.95, 80_000, NOW + 60)[0] == 0
    assert A.exit_decision(p, 0.939, 80_000, NOW + 90) == (1.0, A.exit_decision(pos(), 0.939, 80_000, NOW + 90)[1])
    p = pos()                                                  # +7% then a 4% pullback -> lock the small win
    A.exit_decision(p, 1.07, 80_000, NOW + 60)
    frac, why = A.exit_decision(p, 1.07 * 0.955, 80_000, NOW + 120)
    assert frac == 1.0 and "トレーリング" in why
    p = pos()                                                  # +10% -> half, then the rest can't lose
    frac, why = A.exit_decision(p, 1.10, 80_000, NOW + 60)
    assert frac == pytest.approx(0.5) and "+10%" in why
    assert A.exit_decision(p, 1.03, 80_000, NOW + 120)[0] == 0
    assert A.exit_decision(p, 1.005, 80_000, NOW + 180)[0] == 1.0
    p = pos()                                                  # flat and below entry after 20 minutes -> out
    assert A.exit_decision(p, 0.99, 80_000, NOW + 19 * 60)[0] == 0
    assert A.exit_decision(p, 0.99, 80_000, NOW + 20 * 60)[0] == 1.0
    p = pos()                                                  # never longer than 45 minutes
    assert A.exit_decision(p, 1.03, 80_000, NOW + 45 * 60)[0] == 1.0


def test_one_minute_candles_score_strictly():
    from desk import tracker as T
    t0 = NOW
    # one candle touches both +12% (take profit) and -7% (stop): the stop must win
    both = [[t0 + 60, 1.0, 1.12, 0.93, 1.05], [t0 + 120, 1.05, 1.06, 1.0, 1.02], [t0 + 180, 1.02, 1.03, 1.0, 1.01]]
    assert T.simulate_profile(T.candle_path(t0, 1.0, both), C.EXIT_PROFILE_S) < -0.06
    s = {"outcomes": [{"t0": t0, "p0": 1.0, "pool": "P", "sim": 0.5, "max_up": 0, "max_down": 0, "end": 0,
                       "decision": "BUY", "reasons": [], "liq": 1, "score": 1, "sims": {}, "mint": "M"}]}
    got = []
    assert T.refine(s, lambda pool, a, b, mint: got.append(mint) or both, now=t0 + T.TRACK_HOURS * 3600 + 200) == 1
    o = s["outcomes"][0]
    assert got == ["M"], "the coin's own price is requested"
    assert o["fine"] and o["sims"]["S_勢いスキャル(旧)"] < -0.06 and "R_ランナー(採用中)" in o["sims"]


def test_ohlcv_real_shape(monkeypatch):
    from desk import flow as F
    raw = {"data": {"attributes": {"ohlcv_list": [
        [1795471560, 3.447e-06, 3.4516e-06, 3.4351e-06, 3.4373e-06, 3800.7],
        [1795471500, 3.4522e-06, 3.456e-06, 3.4391e-06, 3.447e-06, 17796.4]]}}}
    monkeypatch.setattr(F, "_last_call", {"t": 0.0})
    monkeypatch.setattr(F.S, "get_json", lambda url, **k: raw)
    c = F.ohlcv("P", 1795471500, 1795471600)
    assert [x[0] for x in c] == [1795471500, 1795471560] and c[0][2] == 3.456e-06 and len(c[0]) == 5


def test_scalp_candidates_expire_when_they_stop_trending():
    s = P.new_state()
    s["candidates"]["M"] = {"seen": NOW - 3 * 3600, "grad": NOW - 30 * 86400, "symbol": "OLD", "status": "waiting", "checks": 0}
    s["candidates"]["N"] = {"seen": NOW - 3 * 3600, "refreshed": NOW - 60, "grad": None, "symbol": "HOT", "status": "waiting", "checks": 0}
    assert A.Scanner().due(s, NOW) == ["N"] and s["candidates"]["M"]["status"] == "expired"


def test_scalp_free_precheck_skips_expensive_checks(monkeypatch):
    calls = []
    d, s, st, _ = _scalp_desk(monkeypatch, rug_calls=calls)
    st["pair"] = dict(st["pair"], priceChange={"m5": -2, "h1": -8})
    for i in range(3):
        d.cycle(NOW + i * C.SCALP_RECHECK_SEC)
    c = s["candidates"]["M"]
    assert calls == [] and c["pre_checks"] == 3 and c["checks"] == 0 and s["stats"]["evaluated"] == 0
    st["pair"] = dict(st["pair"], priceChange={"m5": 2.5, "h1": 30})
    d.cycle(NOW + 3 * C.SCALP_RECHECK_SEC)
    assert "M" in s["positions"] and len(calls) == 1


def test_skipped_coin_is_not_reopened_every_scan(monkeypatch):
    d, s, st, _ = _scalp_desk(monkeypatch, pr=dict(pair(liq=15), pairAddress="P"))
    d.cycle(NOW)
    c = s["candidates"]["M"]
    assert c["status"] == "skipped" and c["last_eval"] == NOW


def test_old_book_is_archived_and_a_fresh_one_starts():
    import json as _j
    C.STATE_FILE.write_text(_j.dumps({"cash": 85.3, "realized": -5.2, "positions": {"W": {"cost": 9.5}},
                                      "outcomes": [{"x": 1}], "credits": {"2026-11": 1234}}))
    s = P.load_state()
    assert s["cash"] == 1000 and not s["positions"] and not s.get("outcomes") and s["book"] == C.BOOK
    assert s["credits"] == {"2026-11": 1234}
    arch = list(C.STATE_FILE.parent.glob("state-archive-*.json"))
    assert len(arch) == 1 and _j.loads(arch[0].read_text())["cash"] == 85.3
    assert P.load_state()["book_start"] == s["book_start"], "only once"


def test_journal_ignores_trades_from_an_archived_book():
    from desk import journal as J
    s = P.new_state()
    _round_trip(s, "OLD", "OLD", 10, 0.5)
    s2 = P.new_state()
    s2["book_start"] = int(__import__("time").time()) + 1
    assert J.trades(s) and J.trades(s2) == []


# ------------------------------------------------------------------ conviction sizing (10% / 20% / 50%)
def _perfect_inputs():
    pr = dict(pair(liq=400_000, m5=6.0, h1=45), pairAddress="POOL")
    flow = {"pressure": 0.72, "burst_wallets": 3}
    return pr, {"holders": 900}, flow


def test_conviction_tiers():
    pr, facts, flow = _perfect_inputs()
    assert A.conviction(pr, facts, flow, 70)[0] == "max"
    for bad in (dict(pr, priceChange={"m5": 20, "h1": 45}), dict(pr, liquidity={"usd": 90_000})):
        assert A.conviction(bad, facts, flow, 70)[0] == "normal"               # one missing item -> not perfect
    assert A.conviction(pr, {"holders": 120}, flow, 85)[0] == "strong"
    assert A.conviction(pr, facts, {"pressure": 0.72, "burst_wallets": 0}, 70, kol_a=1)[0] == "max"
    assert A.conviction(pr, {"holders": 120}, {"pressure": 0.5}, 90)[0] == "normal"


def test_conviction_sizes_and_caps():
    pr, _, _ = _perfect_inputs()
    s = P.new_state()
    assert A.risk(s, pr, NOW)["size"] == pytest.approx(100)
    assert A.risk(s, pr, NOW, tier="strong")["size"] == pytest.approx(200)
    r = A.risk(s, pr, NOW, tier="max")
    assert r["ok"] and r["size"] == pytest.approx(500) and r["gap"] == C.WORST_CASE_GAP_DEEP
    thin = dict(pr, liquidity={"usd": 60_000})                                 # $500 would move it > 1%
    assert A.risk(s, thin, NOW, tier="max")["size"] == pytest.approx(0.01 * 30_000 / 0.99)
    s["daily"][P.hst_day(NOW)] = -40.0                                        # $60 room: $500 x 15% = $75 too big
    assert not A.risk(s, pr, NOW, tier="max")["ok"] and A.risk(s, pr, NOW)["ok"]


def test_perfect_momentum_buys_big(monkeypatch):
    pr, _, _ = _perfect_inputs()
    t = NOW
    trades = {"v": [_tr(t - 10, "buy", 13_000, "A"), _tr(t - 40, "buy", 14_000, "B"), _tr(t - 70, "buy", 14_000, "C")] +
                   [_tr(t - 90 - i, "buy", 200, f"n{i}") for i in range(40)] + [_tr(t - 100, "sell", 900, "s")]}
    d, s, st, _ = _scalp_desk(monkeypatch, pr=pr, trades=trades)
    d.rug = lambda mint, p, state, radar, now: _rug_ok(mint, p, state, radar)
    d.cycle(t)
    pos = s["positions"]["M"]
    assert pos["tier"] == "max" and pos["cost"] == pytest.approx(500) and pos["gap"] == C.WORST_CASE_GAP_DEEP


def _rug_ok(mint, p, state, radar):
    return A.V(True, reasons=["全項目合格"], notes=[], creator="DEV",
               facts={"liq": (p.get("liquidity") or {}).get("usd"), "holders": 900, "creator": "DEV", "top10_pct": 12.0})


# ------------------------------------------------------------------ no chasing: buy the pullback after a spike
def _spike(price, m5=14.0, h1=60.0):
    return dict(pair(price=price, m5=m5, h1=h1), dexId="meteora", pairAddress="POOL")


def test_spike_is_not_chased_and_dip_with_bounce_is_bought(monkeypatch):
    calls = []
    t = NOW
    buys_back = {"v": [_tr(t + 400 - i, "buy", 150, f"b{i}") for i in range(20)] + [_tr(t + 390, "sell", 300, "s")]}
    d, s, st, _ = _scalp_desk(monkeypatch, pr=_spike(0.0010), trades=buys_back, rug_calls=calls)
    d.cycle(t)                                                         # +14% in 5 min -> watch, no rug call
    c = s["candidates"]["M"]
    assert not s["positions"] and c["pb"]["peak"] == 0.0010 and calls == []
    st["pair"] = _spike(0.0011)                                         # still rising -> new high
    d.cycle(t + 20)
    assert c["pb"]["peak"] == 0.0011 and not s["positions"]
    st["pair"] = _spike(0.00103, m5=-4)                                 # -6.4% from the high, no bounce yet
    d.cycle(t + 40)
    assert not s["positions"] and calls == []
    st["pair"] = _spike(0.001045, m5=-2)                                # bounced 1.5% off the low, buyers back
    d.cycle(t + 400)
    assert "M" in s["positions"] and len(calls) == 1 and "pb" not in c


def test_broken_spike_is_given_up(monkeypatch):
    d, s, st, _ = _scalp_desk(monkeypatch, pr=_spike(0.0010))
    d.cycle(NOW)
    st["pair"] = _spike(0.00082, m5=-15)                               # -18% from the high
    d.cycle(NOW + 20)
    assert s["candidates"]["M"]["status"] == "rejected" and not s["positions"]


def test_pullback_wait_has_a_time_limit(monkeypatch):
    d, s, st, _ = _scalp_desk(monkeypatch, pr=_spike(0.0010))
    d.cycle(NOW)
    st["pair"] = _spike(0.00099, m5=3)                                  # shallow dip, never a proper pullback
    d.cycle(NOW + C.PULLBACK_TTL_SEC + 1)
    assert s["candidates"]["M"]["status"] == "rejected"


def test_dip_without_buyers_back_keeps_waiting(monkeypatch):
    t = NOW
    sellers = {"v": [_tr(t + 400 - i, "sell", 150, f"s{i}") for i in range(20)] + [_tr(t + 390, "buy", 100, "b")]}
    d, s, st, _ = _scalp_desk(monkeypatch, pr=_spike(0.0010), trades=sellers)
    d.cycle(t)
    st["pair"] = _spike(0.00094, m5=-5)
    d.cycle(t + 40)
    st["pair"] = _spike(0.00096, m5=-3)                                 # bounce, but sellers still dominate
    d.cycle(t + 400)
    c = s["candidates"]["M"]
    assert not s["positions"] and c["status"] == "waiting" and c["next_check"] == t + 400 + C.SCALP_RECHECK_SEC


# ------------------------------------------------------------------ wallet watch (tier W, e.g. the wallet attributed to Ansem)
def _tx(owner, mint, pre, post, err=None):
    tb = lambda v: [{"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": v}},
                    {"owner": owner, "mint": "So11111111111111111111111111111111111111112", "uiTokenAmount": {"uiAmount": 5}}]
    return {"meta": {"err": err, "preTokenBalances": tb(pre), "postTokenBalances": tb(post)}}


def test_wallet_watch_reports_new_buys_and_sells():
    from desk import watch as W
    kols = {"ANS": {"tier": "W", "name": "Ansem(とされる)"}, "X": {"tier": "A", "name": "xander"}}
    chain = {"sigs": [{"signature": "s0", "blockTime": NOW - 100}], "tx": {}}
    calls = []

    def rpc(method, params):
        calls.append((method, params[0]))
        if method == "getSignaturesForAddress":
            return chain["sigs"]
        return chain["tx"][params[0]]
    s = {}
    assert W.poll(s, kols, rpc, NOW) == [] and s["wwatch"]["ANS"]["last"] == "s0"    # first look: nothing old
    assert ("getSignaturesForAddress", "X") not in calls, "only tier-W wallets are polled"
    chain["sigs"] = [{"signature": "s2", "blockTime": NOW}, {"signature": "s1", "blockTime": NOW - 10}] + chain["sigs"]
    chain["tx"] = {"s1": _tx("ANS", "COIN", 0, 1_000_000), "s2": _tx("ANS", "OLD", 500, 0)}
    ev = W.poll(s, kols, rpc, NOW)
    assert [(e["mint"], e["side"]) for e in ev] == [("COIN", "buy"), ("OLD", "sell")]
    assert ev[0]["name"] == "Ansem(とされる)" and s["wwatch"]["ANS"]["last"] == "s2"
    assert W.poll(s, kols, rpc, NOW + 60) == [], "nothing new"


def test_wallet_watch_never_raises():
    from desk import watch as W

    def boom(*a):
        raise RuntimeError("rpc down")
    s = {}
    assert W.poll(s, {"ANS": {"tier": "W", "name": "A"}}, boom, NOW) == []
    assert s["wwatch"]["ANS"]["error"] == "RuntimeError"


# ------------------------------------------------------------------ exit profile R (runner, live since 2026-11-23 evening)
def test_exit_profile_r_rules():
    def pos():
        return {"entry": 1.0, "peak": 1.0, "opened": NOW, "qty": 10, "cost": 10, "symbol": "X"}
    p = pos()
    assert A.exit_decision(p, 0.91, 80_000, NOW + 60)[0] == 0                   # -9% noise: still holding
    assert A.exit_decision(p, 0.875, 80_000, NOW + 90)[0] == 1.0               # -12.5%: stop
    p = pos()                                                                    # +18% then -10% from peak -> keep the win
    A.exit_decision(p, 1.18, 80_000, NOW + 60)
    frac, why = A.exit_decision(p, 1.18 * 0.895, 80_000, NOW + 120)
    assert frac == 1.0 and "トレーリング" in why
    p = pos()                                                                    # +25% -> 1/3, then can't lose
    assert A.exit_decision(p, 1.25, 80_000, NOW + 60)[0] == pytest.approx(1 / 3)
    assert A.exit_decision(p, 1.05, 80_000, NOW + 120)[0] == 0
    assert A.exit_decision(p, 1.019, 80_000, NOW + 180)[0] == 1.0             # floor: sell what's left
    p = pos()                                                                    # a big run: 1/3 at +25%, 1/3 at +100%, rest rides
    A.exit_decision(p, 1.25, 80_000, NOW + 60)
    assert A.exit_decision(p, 2.0, 80_000, NOW + 600)[0] == pytest.approx(0.5)  # 1/3 of the start = half of what's left
    assert A.exit_decision(p, 3.0, 80_000, NOW + 1200)[0] == 0
    assert A.exit_decision(p, 2.2, 80_000, NOW + 1500)[0] == 0                 # -27% from the 3x peak: still in
    assert A.exit_decision(p, 2.09, 80_000, NOW + 1600)[0] == 1.0
    p = pos()
    assert A.exit_decision(p, 0.97, 80_000, NOW + 59 * 60)[0] == 0
    assert A.exit_decision(p, 0.97, 80_000, NOW + 60 * 60)[0] == 1.0          # below entry after 60 minutes
    assert A.exit_decision(pos(), 1.10, 80_000, NOW + 6 * 3600)[0] == 1.0     # 6-hour limit


def test_refine_rejects_the_wrong_tokens_price_and_repairs_old_records():
    from desk import tracker as T
    t0 = NOW
    sol_price = [[t0 + 60 * i, 140.0, 141.0, 139.0, 140.5] for i in range(1, 5)]     # SOL in USD, not the coin
    o = {"t0": t0, "p0": 0.00002, "pool": "P", "mint": "M", "sim": 0.1, "max_up": 9000.0, "max_down": 0, "end": 0,
         "decision": "BUY", "reasons": [], "liq": 1, "score": 1, "sims": {"A": 9000.0}, "fine": True}
    s = {"outcomes": [o]}
    T.refine(s, lambda pool, a, b, mint: sol_price, now=t0 + T.TRACK_HOURS * 3600 + 200)
    assert o["fixed"] and not o["fine"] and o["fine_try"] == 1 and o["sims"] == {"A": 9000.0}
