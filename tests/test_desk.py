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
    assert not rug(p=pair(liq=20_000))["ok"]
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


def test_take_profit_half_then_trailing():
    p = pos()
    assert A.exit_decision(p, 1.30, 80_000, NOW)[0] == 0.5
    assert A.exit_decision(p, 1.40, 80_000, NOW)[0] == 0
    assert A.exit_decision(p, 1.40 * 0.87, 80_000, NOW)[0] == 1.0


def test_time_stop():
    assert A.exit_decision(pos(), 1.0, 80_000, NOW + C.MAX_HOLD_MIN * 60)[0] == 1.0


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
