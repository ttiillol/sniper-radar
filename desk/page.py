"""Builds site/desk.html (phone page for the paper desk)."""
import json
import time
from pathlib import Path

from . import config as C
from . import portfolio as P
from . import tracker as T
from . import journal as J
from . import flow as F


def build(site: Path):
    s = P.load_state()
    try:
        lines = C.DECISIONS_LOG.read_text().splitlines()[-150:]
        decisions = [json.loads(x) for x in lines if x.strip()][::-1]
    except FileNotFoundError:
        decisions = []
    slim = [{k: d.get(k) for k in ("ts", "kind", "mint", "symbol", "action", "reason")} for d in decisions]
    mk = time.strftime("%Y-%m", time.gmtime())
    try:
        kols = F.load_kols()
        kol_count = {"A": sum(1 for v in kols.values() if v["tier"] == "A"), "all": len(kols)}
    except Exception:
        kol_count = {"A": 0, "all": 0}
    kol_seen = [dict(k, status=(s["candidates"].get(k["mint"]) or {}).get("status", "done"))
                for k in (s.get("kol_seen") or [])[-15:]][::-1]
    try:
        tr = J.trades(s)
        jstats, bvl = J.stats(tr), J.backtest_vs_live(s, tr)
        jtr = [{k: t[k] for k in ("symbol", "mint", "closed", "R", "ret", "pnl", "hold_min", "exit")} for t in tr[-40:]][::-1]
    except Exception:
        jstats, bvl, jtr = {"n": 0, "curve": []}, {}, []
    payload = {
        "cash": s["cash"], "realized": s["realized"], "today": P.day_pnl(s),
        "positions": s["positions"], "halted": s["halted"], "kill": C.KILL_FILE.exists(),
        "heartbeat": s["last_heartbeat"], "gaps": s["gaps"][-5:][::-1], "stats": s["stats"],
        "credits": s["credits"].get(mk, 0), "decisions": slim, "rpc": s.get("rpc"),
        "samples": s.get("rpc_samples") or {}, "watch": sum(
            1 for c in s["candidates"].values() if c.get("status") == "waiting"),
        "verify": T.summarize(s.get("outcomes", [])), "tracking": len(s.get("tracking", {})),
        "outcomes_n": len(s.get("outcomes", [])),
        "selfcheck": s.get("selfcheck"), "kol_seen": kol_seen, "kol_count": kol_count, "journal": jstats, "jtrades": jtr, "bvl": bvl, "risk_pct": J.RISK_PCT,
        "limits": {"daily": C.DAILY_LOSS_LIMIT, "total": C.TOTAL_LOSS_LIMIT, "max": C.MAX_OPEN_POSITIONS,
                   "start": C.START_CASH},
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                            .replace("</", "<\\/"))
    (site / "desk.html").write_text(html, encoding="utf-8")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>練習売買デスク</title>
<style>
:root{--bg:#eef0f3;--card:#fff;--line:#e3e6eb;--tx:#121722;--mut:#6b7280;--red:#e5484d;--org:#e0820b;--grn:#16a34a;--acc:#2563eb;--soft:#f6f7f9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;padding:16px 14px 60px;max-width:780px;margin:auto}
a{color:var(--acc);text-decoration:none}h1{font-size:20px;margin:4px 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.nav{display:flex;gap:8px;margin-bottom:12px}.nav a{flex:1;text-align:center;padding:8px;border-radius:10px;background:var(--card);border:1px solid var(--line);font-size:13px}.nav a.on{background:var(--acc);color:#fff;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}h2{font-size:15px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.st{background:var(--soft);border-radius:10px;padding:10px}.st b{display:block;font-size:20px}.st span{font-size:11px;color:var(--mut)}
.pos{color:var(--grn)}.neg{color:var(--red)}.banner{padding:12px;border-radius:10px;font-weight:700;margin-bottom:14px}
.b-red{background:#fdeced;color:var(--red)}.b-grn{background:#e7f6ec;color:var(--grn)}.b-org{background:#fdf1e0;color:var(--org)}
.row{padding:9px 0;border-top:1px solid var(--line);font-size:13.5px}.row:first-child{border-top:0}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;margin-right:6px;font-weight:700}
.BUY{background:#e7f6ec;color:var(--grn)}.SELL{background:#e8efff;color:var(--acc)}.REJECT{background:#eef0f3;color:var(--mut)}.WATCH{background:#fdf1e0;color:var(--org)}.RESUME,.PAUSE,.NO_PRICE,.ERROR{background:#fdeced;color:var(--red)}
.small{font-size:11.5px;color:var(--mut)}.bar{height:8px;border-radius:9px;background:var(--soft);overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--red)}
.filters{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}.f{padding:5px 10px;border-radius:99px;background:var(--soft);font-size:12px;color:var(--mut)}.f.on{background:var(--acc);color:#fff;font-weight:700}
.fd{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line);font-size:13.5px}.fd:first-child{border-top:0}
.lb{font:700 11px ui-monospace,Menlo,monospace;padding:3px 0;width:46px;text-align:center;border-radius:6px;flex:none}
.L-BUY{background:#e7f6ec;color:var(--grn)}.L-SELL{background:#e8efff;color:var(--acc)}.L-STOP{background:#fdeced;color:var(--red)}.L-SKIP{background:#eef0f3;color:var(--mut)}
.warn{font-size:12.5px;color:var(--mut)}.warn b{color:var(--org)}
/* live "now doing" card */
.lv{border-color:var(--line)}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--red);margin-right:6px;animation:pulse 1.2s infinite}
.dot.off{background:var(--mut);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(229,72,77,.7)}70%{box-shadow:0 0 0 9px rgba(229,72,77,0)}100%{box-shadow:0 0 0 0 rgba(229,72,77,0)}}
.now{background:var(--soft);border-radius:12px;padding:12px;margin-bottom:10px}
.now .t{font-size:15px;font-weight:700;line-height:1.45}.now .k{width:10px;height:10px;margin:0 8px 0 0;vertical-align:1px}
.sym{display:inline-block;background:#eef2ff;color:var(--acc);border-radius:7px;padding:1px 8px;margin-right:6px;font:700 13px ui-monospace,Menlo,monospace}
.scan{height:4px;border-radius:4px;background:#eceef2;overflow:hidden;margin-top:10px;position:relative}
.scan i{position:absolute;top:0;left:-40%;width:40%;height:100%;background:linear-gradient(90deg,transparent,var(--acc),transparent);animation:sweep 1.1s linear infinite}
.scan.idle i{background:linear-gradient(90deg,transparent,#cfd4dc,transparent);animation-duration:3s}
@keyframes sweep{to{left:100%}}
.ms{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.ms span{background:var(--soft);border-radius:8px;padding:4px 8px;font-size:11.5px;color:var(--mut)}.ms b{color:var(--tx)}
.lf{display:flex;gap:8px;align-items:flex-start;padding:7px 4px;border-top:1px solid var(--line);font-size:13px;border-radius:6px}
.k{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;background:var(--acc)}.lf .k{margin-top:6px}
.k-buy{background:var(--grn)}.k-sell{background:var(--acc)}.k-stop,.k-error{background:var(--red)}.k-reject,.k-skip,.k-idle,.k-start{background:#9aa1ad}.k-wait,.k-watch{background:var(--org)}.k-new{background:#b69cff}.k-work,.k-check,.k-hold{background:var(--acc)}
.lf .ic{width:22px;text-align:center;flex:none}.lf .ag{flex:none;width:44px;text-align:right;font-size:11px;color:var(--mut);padding-top:2px}
.new{animation:flash 2.2s ease-out}@keyframes flash{0%{background:#dbe7ff;transform:translateY(-6px);opacity:.2}25%{opacity:1;transform:none}100%{background:transparent}}
.rr{display:flex;gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:13px}
.rb{flex:none;width:90px;height:10px;background:var(--soft);border-radius:5px;position:relative}.rb::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#cfd4dc}
.rb i{position:absolute;top:0;height:100%;border-radius:5px}
.cmp{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.cmp div{background:var(--soft);border-radius:10px;padding:9px;font-size:11.5px;color:var(--mut)}.cmp b{display:block;font-size:17px;color:var(--tx)}
.r{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:99px;margin-left:4px;white-space:nowrap;display:inline-block}
.r-BUY{background:#e7f6ec;color:var(--grn)}.r-SELL{background:#e8efff;color:var(--acc)}.r-STOP,.r-ERROR,.r-NO_PRICE{background:#fdeced;color:var(--red)}
.r-REJECT,.r-SKIP{background:#eef0f3;color:var(--mut)}.r-WATCH,.r-WAIT{background:#fdf1e0;color:var(--org)}.r-NEW{background:#f1ecff;color:#b69cff}
#lfeed{max-height:430px;overflow-y:auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.hd{display:flex;gap:12px;align-items:center;margin:2px 0 14px}.logo{width:52px;height:52px;border-radius:14px;background:#121722;flex:none;display:grid;place-items:center}
.hd h1{font-size:22px;margin:0;letter-spacing:-.3px}.hd h1 em{font-style:normal;color:var(--grn)}.hd .sub{margin:2px 0 0;font:11px ui-monospace,Menlo,monospace}
.bal{margin-left:auto;text-align:right}.bal small{font:10px ui-monospace,Menlo,monospace;color:var(--mut);letter-spacing:1px}.bal b{display:block;font:800 30px/1.05 ui-monospace,Menlo,monospace;letter-spacing:-1px}
.bal span{font:11px ui-monospace,Menlo,monospace;color:var(--mut)}
.ct{display:flex;justify-content:space-between;align-items:center;font:11px ui-monospace,Menlo,monospace;color:#4b5563;letter-spacing:.6px;margin-bottom:6px}
.ct .on{color:var(--grn)}
.hub{position:relative;width:100%;aspect-ratio:760/440;margin-top:4px}
.hub svg.wires{position:absolute;inset:0;width:100%;height:100%}
.ag{position:absolute;transform:translate(-50%,-50%);width:30%;max-width:210px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:6px 8px;display:flex;gap:7px;align-items:center;box-shadow:0 1px 2px rgba(16,24,40,.05);transition:border-color .3s,box-shadow .3s}
.ag .ic{flex:none;width:24px;height:24px;border-radius:7px;display:grid;place-items:center;color:#fff;font:700 9px ui-monospace,Menlo,monospace}
.ag .nm{font:700 10.5px ui-monospace,Menlo,monospace;letter-spacing:.3px;white-space:nowrap}.ag .tx{font:10px ui-monospace,Menlo,monospace;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ag .bd{min-width:0;flex:1}
.ag.act{border-width:2px;box-shadow:0 0 0 4px var(--glow)}.ag.idle .tx{color:#a3a9b3}
.core{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center}
.core .ring{width:86px;height:86px;border-radius:50%;background:#fff;border:3px solid #121722;display:grid;place-items:center;margin:auto;box-shadow:0 0 0 8px rgba(22,163,74,.08),0 0 0 16px rgba(22,163,74,.05)}
.core.busy .ring{animation:corepulse 1.4s infinite}@keyframes corepulse{0%{box-shadow:0 0 0 6px rgba(22,163,74,.18),0 0 0 14px rgba(22,163,74,.08)}100%{box-shadow:0 0 0 14px rgba(22,163,74,0),0 0 0 26px rgba(22,163,74,0)}}
.core .cap{font:700 10.5px ui-monospace,Menlo,monospace;margin-top:8px;white-space:nowrap;background:#fff;padding:2px 8px;border-radius:7px;display:inline-block}.core .cap span{color:var(--mut);font-weight:400}
.wire{stroke:#d6dae1;stroke-width:.35;stroke-dasharray:1 1.2}.wire.hot{stroke-width:.6;stroke-dasharray:none}.wire.act{stroke-width:.9;stroke-dasharray:2 1.5;animation:flow 0.8s linear infinite}
@keyframes flow{to{stroke-dashoffset:-7}}
.charts{display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:8px;overflow-x:auto;padding-bottom:2px}
.cc{border:1px solid var(--line);border-radius:12px;padding:9px;min-width:140px}.cc.hold{border:2px solid var(--grn)}
.cc .h{display:flex;justify-content:space-between;align-items:center;font:700 12px ui-monospace,Menlo,monospace}.cc .h a{color:var(--tx)}
.cc .px{font:700 15px ui-monospace,Menlo,monospace;margin-top:4px}.cc .mc{font:10px ui-monospace,Menlo,monospace;color:var(--mut)}
.cc .ft{display:flex;justify-content:space-between;font:10px ui-monospace,Menlo,monospace;color:var(--mut);margin-top:4px}
.cc svg{width:100%;height:84px;display:block;margin-top:6px}
.trio{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}.trio>.card{min-width:0;margin-bottom:0}.trio .wide{grid-column:1/-1}.trio{margin-bottom:14px}
@media(max-width:640px){.trio{grid-template-columns:minmax(0,1fr)}.hub{aspect-ratio:390/600}.ag{width:34%;padding:4px 6px;gap:4px;border-radius:8px}.ag .ic{display:none}.ag .nm{font-size:9.5px}.ag .tx{font-size:8.5px}
.core .ring{width:62px;height:62px}.core .ring svg{width:30px;height:30px}.core .cap{font-size:9px}.hd{flex-wrap:wrap}.hd h1{font-size:19px}.logo{width:42px;height:42px}
.bal{margin-left:0;width:100%;text-align:left;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}.bal b{font-size:26px}.ct{font-size:10px}}
.lad{font:11px ui-monospace,Menlo,monospace}.lad .lr{display:grid;grid-template-columns:62px 1fr 64px;gap:6px;align-items:center;height:21px}
.lad .tk{height:14px;display:flex}.lad .tk i{display:block;height:100%;border-radius:2px}.lad .tk.s i{margin-left:auto;background:#f8c9cb}.lad .tk.b i{background:#bfe8cc}.lad .mid{display:flex;justify-content:space-between;align-items:center;background:#e7f6ec;border-radius:4px;padding:3px 6px;margin:3px 0;font-weight:700}.lad .mid span:last-child{font-weight:400;color:var(--mut)}
.lad .lr .n{text-align:right;color:var(--mut)}
.radar svg{width:100%;max-width:230px;display:block;margin:4px auto}
.sweep{transform-origin:50px 50px;animation:spin 4s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.lg{font:11px ui-monospace,Menlo,monospace;display:flex;justify-content:space-between;padding:2px 0}
.term{background:#0e1117;color:#c9d1d9;border-radius:10px;padding:10px;font:11px/1.55 ui-monospace,Menlo,monospace;height:300px;overflow:hidden;position:relative}
.term .l{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.term .tm{color:#6e7681}.term .g{color:#3fb950}.term .r{color:#f85149}.term .b{color:#58a6ff}.term .o{color:#d29922}.term .p{color:#bc8cff}.term .d{color:#8b949e}
.term .cur{color:#3fb950}.term .cur::after{content:"_";animation:blink 1s step-end infinite}@keyframes blink{50%{opacity:0}}
.term .new{animation:tflash 1.6s ease-out}@keyframes tflash{0%{background:#1f6feb44}100%{background:transparent}}
</style></head><body>
<div class="nav"><a href="./">レーダー</a><a class="on" href="desk.html">デスク</a></div>
<div class="hd"><div class="logo"><svg width="30" height="30" viewBox="0 0 30 30"><circle cx="15" cy="15" r="11" fill="none" stroke="#fff" stroke-width="2.4"/><circle cx="15" cy="15" r="4" fill="#3ddc84"/><path d="M15 1v6M15 23v6M1 15h6M23 15h6" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg></div>
<div><h1>Sniper Radar <em>Meme Desk</em></h1><div class="sub" id="hsub"></div></div>
<div class="bal"><small>BALANCE</small><b id="hbal">-</b><span id="hstat"></span></div></div>
<div id="banner"></div>
<div class="card"><div class="ct"><span>12の担当 → 1つのデスク ・ リアルタイム</span><span class="on" id="online">● -</span></div>
<div class="hub" id="hub"><svg class="wires" viewBox="0 0 100 100" preserveAspectRatio="none" id="wires"></svg>
<div class="core" id="core"><div class="ring"><svg width="40" height="40" viewBox="0 0 30 30"><circle cx="15" cy="15" r="11" fill="none" stroke="#121722" stroke-width="2.4"/><circle cx="15" cy="15" r="4" fill="#16a34a"/><path d="M15 1v6M15 23v6M1 15h6M23 15h6" stroke="#121722" stroke-width="2.4" stroke-linecap="round"/></svg></div><div class="cap" id="corecap">DESK <span>· 接続中</span></div></div>
</div></div>
<div class="card"><div class="ct"><span>コインチャート ・ 保有中と直近の審査 ・ 5分ごと</span><span id="chst">-</span></div><div class="charts" id="charts"></div></div>
<div class="trio">
<div class="card"><div class="ct"><span>売り買いの板 ・ 金額帯</span><span class="on" id="ladsym"></span></div><div class="lad" id="lad"></div></div>
<div class="card radar"><div class="ct"><span>大口レーダー</span><span id="radsym"></span></div><div id="radar"></div></div>
<div class="card wide"><div class="ct"><span>$ tail -f desk.log</span><span id="lcount"></span></div><div class="term" id="term"></div></div>
</div>
<div class="card"><div class="grid">
<div class="st"><b id="eq">-</b><span>資金（保有分は買値で計算）</span></div>
<div class="st"><b id="rl">-</b><span>確定した損益（累計）</span></div>
<div class="st"><b id="td">-</b><span>今日の損益（ハワイ時間）</span></div>
<div class="st"><b id="np">-</b><span>保有中 / 審査待ち</span></div>
</div>
<div class="small" style="margin-top:10px">今日の損失枠 <span id="dl"></span></div><div class="bar"><i id="dlb"></i></div>
<div class="small" style="margin-top:8px">累計の損失枠 <span id="tl"></span></div><div class="bar"><i id="tlb"></i></div>
</div>
<div class="card" id="rc"></div>
<div class="card" id="bvl"></div>
<div class="card" id="kol"></div>
<div class="card"><h2>売買の記録 <span class="small">（1分ごとに自動更新）</span></h2><div id="feed"></div></div>
<div class="card" id="vc"></div>
<div class="card" id="hc"></div>
<div class="card"><h2>保有中</h2><div id="poss"></div></div>
<div class="card"><h2>判断の記録</h2>
<div class="filters"><span class="f on" data-f="all">すべて</span><span class="f" data-f="BUY">買い</span><span class="f" data-f="SELL">売り</span><span class="f" data-f="REJECT">見送り</span><span class="f" data-f="WATCH">様子見</span></div>
<div id="dec"></div></div>
<div class="card"><h2>見送った理由ランキング</h2><div id="rej"></div></div>
<div class="card" id="gapc"><h2>止まっていた時間の記録</h2><div id="gaps"></div></div>
<div class="card warn"><h2>注意</h2>
これは<b>練習（ペーパー）</b>です。本物のお金は動きません。ミームコインは数分で価値がほぼゼロになることがあり、損切りが効かずに大きく負けることがあります。
この画面の成績が良くても、本番で同じ結果になる保証はありません。<br><br>
ルール：1回 $5〜10 ／ 売り方E：−30%で損切り／+25%まで上がったら高値から−12%で全部利確（小さな勝ちを確保）／+50%で1/3売って残りは負けなし／+150%で1/3／最後の1/3は高値から−35%まで持つ ／ 作成者が売ったら即売却 ／ 買う前に売却テスト（売れないコインは買わない）／ 損切り後は大口3人以上が2分で買ったときだけ1回買い直し ／ 最大5件 ／ 1日 −$10 で新規停止 ／ 累計 −$30 で完全停止 ／ 急落で損切りが効かない前提（−50%）で枠を計算</div>
<script>
const D=__DATA__;const $=s=>document.querySelector(s);
const ago=t=>{if(!t)return"-";const m=Math.round((Date.now()/1000-t)/60);return m<60?m+"分前":m<1440?Math.round(m/60)+"時間前":Math.round(m/1440)+"日前"};
const usd=v=>(v<0?"-$":"$")+Math.abs(v).toFixed(2), sg=v=>(v>=0?"+":"")+usd(v).replace("+","");
const cls=v=>v>0?"pos":v<0?"neg":"";
const open=Object.values(D.positions).reduce((a,p)=>a+p.cost,0);
$("#hsub").dataset.upd="最終更新 "+ago(D.heartbeat)+" ・ 審査 "+(D.stats.evaluated||0)+" 件 ・ 買い "+(D.stats.bought||0)+" ・ 売り "+(D.stats.sold||0);
$("#eq").textContent=usd(D.cash+open);
$("#rl").innerHTML=`<span class="${cls(D.realized)}">${sg(D.realized)}</span>`;
$("#td").innerHTML=`<span class="${cls(D.today)}">${sg(D.today)}</span>`;
$("#np").textContent=Object.keys(D.positions).length+"/"+D.limits.max+" ・ "+D.watch;
const dl=Math.max(0,-D.today),tl=Math.max(0,-D.realized);
$("#dl").textContent=`${usd(dl)} / ${usd(D.limits.daily)}`;$("#dlb").style.width=Math.min(100,dl/D.limits.daily*100)+"%";
$("#tl").textContent=`${usd(tl)} / ${usd(D.limits.total)}`;$("#tlb").style.width=Math.min(100,tl/D.limits.total*100)+"%";
const stale=D.heartbeat&&Date.now()/1000-D.heartbeat>3*3600;
$("#banner").innerHTML=D.halted?`<div class="banner b-red">完全停止中：${D.halted}</div>`:D.kill?`<div class="banner b-red">停止ファイルにより停止中</div>`:
 dl>=D.limits.daily?`<div class="banner b-org">今日の損失上限に到達 — 明日まで新規購入なし</div>`:stale?`<div class="banner b-org">3時間以上更新されていません</div>`:"";
const P=Object.entries(D.positions);
$("#poss").innerHTML=P.length?P.map(([m,p])=>`<div class="row"><b>${(p.symbol||"?").replace(/</g,"")}</b> ${usd(p.cost)} ・ ${ago(p.opened)}に購入${p.half_sold?" ・ 半分利確済":""}
 <div class="small">買値 ${p.entry.toPrecision(4)} ・ 最高値 ${(p.peak/p.entry*100-100).toFixed(1)}% ・ <a href="https://dexscreener.com/solana/${m}" target="_blank" rel="noopener">チャート</a></div></div>`).join(""):'<div class="small">保有なし</div>';
(function(){
  const V=D.verify||{}, pct=x=>(x*100).toFixed(1)+"%";
  const tbl=(title,g,order)=>{const rows=Object.entries(g||{}).filter(([k,v])=>v).sort((a,b)=>order?order.indexOf(a[0])-order.indexOf(b[0]):b[1].n-a[1].n);
    if(!rows.length)return"";return `<div style="margin-top:10px"><b style="font-size:13px">${title}</b>`+rows.map(([k,v])=>`<div class="row" style="display:flex;gap:8px;align-items:center"><span style="flex:1">${k.replace(/</g,"")}</span>
    <span class="small">${v.n}件</span><span class="small" style="width:70px;text-align:right">勝率 ${pct(v.win)}</span><b class="${v.avg>0?"pos":"neg"}" style="width:62px;text-align:right">${v.avg>0?"+":""}${pct(v.avg)}</b></div>`).join("")+"</div>"};
  const NMD={BUY:"買った",REJECT:"見送った",WATCH:"様子見した"};
  const dec=Object.fromEntries(Object.entries(V.decision||{}).map(([k,v])=>[NMD[k]||k,v]));
  $("#vc").innerHTML=`<h2>検証：もし買っていたら <span class="small">（6時間追跡・6つの売り方（A〜F）を同じ値動きで比較）</span></h2>
  <div class="small">追跡中 ${D.tracking}件 ・ 結果が出た ${D.outcomes_n}件。見送ったコインの成績が良ければ、そのルールは厳しすぎる証拠です。</div>
  ${D.outcomes_n?tbl("売り方の比較（同じコイン・違う売りルール）",V.profiles)+tbl("判断別",dec)+tbl("見送り理由別",V.reason)+tbl("流動性別",V.liq,["$30k以上","$10k〜30k","$5k〜10k","$5k未満","不明"])+tbl("点数別",V.score,["60以上","40〜59","40未満"])+tbl("保有者数別",V.holders,["75人未満","75〜300人","300人以上"])+tbl("狙撃ボットの保有率別",V.snipers,["5%未満","5〜20%","20%以上"])+tbl("作成者の過去",V.creator)+tbl("KOLの購入別（ベンチマーク）",V.kol):'<div class="small" style="margin-top:8px">まだ結果がありません（審査から4時間後に出始めます）</div>'}`;
})();
(function(){
  const lab=d=>d.action==="BUY"?"BUY":d.action==="SELL"?((d.reason||"").startsWith("損切り")?"STOP":"SELL"):d.action==="REJECT"?"SKIP":null;
  const F=D.decisions.map(d=>[lab(d),d]).filter(x=>x[0]).slice(0,40);
  $("#feed").innerHTML=F.length?F.map(([l,d])=>{const pnl=(d.reason||"").match(/[+-]\d+(\.\d+)?%/);
    return `<div class="fd"><span class="lb L-${l}">${l}</span><div style="flex:1;min-width:0"><b>${(d.symbol||"?").replace(/</g,"")}</b>
    <span class="small">${ago(d.ts)}</span><div class="small">${(d.reason||"").replace(/^\[Rug\] /,"").replace(/</g,"").slice(0,90)}</div></div></div>`}).join("")
   :'<div class="small">まだ動きがありません</div>';
})();
const NM={BUY:"買い",SELL:"売り",REJECT:"見送り",WATCH:"様子見",RESUME:"再開",PAUSE:"一時停止",NO_PRICE:"価格不明",ERROR:"エラー"};
let F="all";function drawDec(){const L=D.decisions.filter(d=>F==="all"||d.action===F).slice(0,60);
 $("#dec").innerHTML=L.length?L.map(d=>`<div class="row"><span class="tag ${d.action}">${NM[d.action]||d.action}</span><b>${(d.symbol||"").replace(/</g,"")}</b> <span class="small">${ago(d.ts)}</span>
 <div class="small">${(d.reason||"").replace(/</g,"")}</div></div>`).join(""):'<div class="small">まだ記録がありません</div>'}
document.querySelectorAll(".f").forEach(e=>e.onclick=()=>{document.querySelectorAll(".f").forEach(x=>x.classList.remove("on"));e.classList.add("on");F=e.dataset.f;drawDec()});drawDec();
(function(){const st=D.stats,pf=Object.entries(st.prefiltered||{}).sort((a,b)=>b[1]-a[1]).slice(0,4).map(([k,v])=>k+" "+v).join("・")||"なし";
$("#hc").innerHTML=`<h2>デスクの調子</h2><div class="small" style="line-height:1.9">
卒業コインの受信 ${st.migrations||0} ・ 補充 ${st.backfilled||0} ・ 審査 ${st.evaluated||0}<br>
無料で除外：${pf}<br>
エラー ${st.errors||0} 件 ・ データ窓口 ${D.rpc==="helius"?"Helius（専用）":"公共（混みやすい）"} ・ 今月 ${(D.credits||0).toLocaleString()} クレジット
${D.selfcheck?"<br>データ窓口の自己点検（"+ago(D.selfcheck.ts)+"・"+String(D.selfcheck.coin||"").replace(/</g,"")+"で試験）："+Object.entries(D.selfcheck.results).map(([k,v])=>`<span class="${v.ok?"pos":"neg"}">${v.ok?"OK":"NG"}</span> ${k}`).join(" ・ "):""}
${Object.keys(D.samples).length?"<br>エラー例："+Object.values(D.samples).map(x=>String(x).replace(/</g,"")).join(" ／ "):""}</div>`})();
const R=Object.entries(D.stats.rejected||{}).sort((a,b)=>b[1]-a[1]).slice(0,10);
$("#rej").innerHTML=R.length?R.map(([k,v])=>`<div class="row" style="display:flex;justify-content:space-between"><span>${k.replace(/</g,"")}</span><b>${v}</b></div>`).join(""):'<div class="small">まだありません</div>';
$("#gaps").innerHTML=D.gaps.length?D.gaps.map(g=>`<div class="row">${ago(g.ts)} ・ ${Math.round(g.sec/60)}分停止 ・ 再開時の保有 ${g.open}件</div>`).join(""):'<div class="small">記録なし</div>';
(function(){
  const J=D.journal||{n:0},pct=x=>(x>0?"+":"")+(x*100).toFixed(1)+"%",Rf=x=>x==null?"-":(x>0?"+":"")+x.toFixed(2)+"R";
  let h=`<h2>成績（R倍数） <span class="small">1R＝買った額×${Math.round(D.risk_pct*100)}%（損切り幅＝計画したリスク）</span></h2>`;
  if(!J.n){h+='<div class="small">まだ終わったトレードがありません。売り終わると、ここに1件ずつ「何R勝った／負けた」が並びます。</div>'}
  else{
    h+=`<div class="grid">
    <div class="st"><b>${J.n}件 ・ ${(J.win*100).toFixed(0)}%</b><span>終わったトレード ・ 勝率</span></div>
    <div class="st"><b class="${cls(J.avgR)}">${Rf(J.avgR)}</b><span>1回あたり平均（期待値）</span></div>
    <div class="st"><b class="${cls(J.totalR)}">${Rf(J.totalR)}</b><span>合計R（${sg(J.pnl)}）</span></div>
    <div class="st"><b>${J.pf==null?"負けなし":J.pf.toFixed(2)}</b><span>PF（総利益÷総損失・1超えでプラス）</span></div>
    <div class="st"><b class="neg">-$${J.mdd.toFixed(2)}</b><span>最大の落ち込み（${J.mdd_pct}%）</span></div>
    <div class="st"><b>${J.hold}分</b><span>平均保有 ・ 勝ち平均 ${Rf(J.avgWinR)} ／ 負け平均 ${Rf(J.avgLossR)}</span></div></div>`;
    const c=J.curve;if(c.length>1){const ys=c.map(p=>p[1]).concat([D.limits.start]),mn=Math.min(...ys),mx=Math.max(...ys),W=600,H=90,
      X=i=>i/(c.length-1)*W,Y=v=>H-6-(v-mn)/((mx-mn)||1)*(H-12);
      h+=`<div class="small" style="margin-top:10px">資金の推移（終わったトレードごと）</div><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:90px;margin-top:4px">
      <line x1="0" x2="${W}" y1="${Y(D.limits.start)}" y2="${Y(D.limits.start)}" stroke="#cfd4dc" stroke-dasharray="4 4"/>
      <polyline fill="none" stroke="${c[c.length-1][1]>=D.limits.start?"#3ddc84":"#ff5d5d"}" stroke-width="2.5" points="${c.map((p,i)=>X(i)+","+Y(p[1])).join(" ")}"/></svg>`}
    h+=`<div style="margin-top:8px">`+(D.jtrades||[]).slice(0,15).map(t=>{const w=Math.min(50,Math.abs(t.R)/3*50);
      return `<div class="rr"><b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><a href="https://dexscreener.com/solana/${t.mint}" target="_blank" rel="noopener">${(t.symbol||"?").replace(/</g,"")}</a>
      <span class="small"> ${ago(t.closed)} ・ ${t.hold_min}分保有</span></b><span class="rb"><i style="${t.R>=0?"left:50%":"right:50%"};width:${w}%;background:${t.R>=0?"var(--grn)":"var(--red)"}"></i></span>
      <b class="${cls(t.R)}" style="width:62px;text-align:right">${Rf(t.R)}</b></div>`}).join("")+`</div>`;
  }
  $("#rc").innerHTML=h;
  const B=D.bvl||{},box=(t,v)=>`<div>${t}<b>${v?(v.win*100).toFixed(0)+"%":"-"}</b>勝率 ・ 平均 ${v?pct(v.avg):"-"}<br>${v?v.n:0}件</div>`;
  let msg='まだ件数が少ないので判断できません（どちらも20件以上で目安になります）';
  if(B.sim_bought&&B.live&&B.sim_bought.n>=20&&B.live.n>=20){const d=(B.live.avg-B.sim_bought.avg)*100;
    msg=Math.abs(d)<5?'検証と実際がほぼ同じ — 検証の数字を信用してルール調整できます':d<0?`実際が検証より ${Math.abs(d).toFixed(1)}ポイント悪い — 手数料・すべり・急落時の売り遅れが原因の可能性。検証だけで判断しないこと`:`実際が検証より ${d.toFixed(1)}ポイント良い — たまたまの可能性も。件数を増やして確認`}
  $("#bvl").innerHTML=`<h2>検証 vs 実際 <span class="small">（バックテストどおりに勝てているか）</span></h2>
  <div class="cmp">${box("検証<br>（買ったコイン）",B.sim_bought)}${box("実際<br>（練習売買）",B.live)}${box("参考<br>（審査した全コイン）",B.sim_all)}</div>
  <div class="small" style="margin-top:8px">検証＝同じコインに売り方Eを当てはめた計算上の成績。${msg}</div>`;
})();
(function(){
  const K=D.kol_count||{},S=D.kol_seen||[],ST={bought:"買った",rejected:"見送り",waiting:"審査中",skipped:"除外",expired:"期限切れ",error:"エラー",done:"判断済み"};
  $("#kol").innerHTML=`<h2>KOLベンチマーク <span class="small">kolscan月間上位${K.all||0}人（慎重派A ${K.A||0}人）の買いを物差しにする</span></h2>
  <div class="small">真似して買うためではなく、「プロが買ったコインを、このデスクはどう判断したか」を比べるための欄です。KOLが買うと点数が少し上がります（A +10、B +4、最大+20）。</div>`+
  (S.length?S.map(k=>`<div class="row" style="display:flex;gap:8px;align-items:center"><b style="flex:1;min-width:0"><a href="https://dexscreener.com/solana/${k.mint}" target="_blank" rel="noopener">${String(k.symbol||"?").replace(/</g,"")}</a>
   <span class="small"> ${ago(k.ts)} ・ ${k.names.map(n=>String(n).replace(/</g,"")).join("・")}${k.n>k.names.length?" ほか":""}</span></b>
   <span class="tag ${k.status==="bought"?"BUY":k.status==="rejected"?"REJECT":"WATCH"}">${ST[k.status]||k.status}</span></div>`).join(""):'<div class="small" style="margin-top:8px">まだ、審査したコインをKOLが買った例はありません。</div>');
})();
(function(){
  const esc=x=>String(x==null?"":x).replace(/[<>&"]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
  const AG=[["scout","新着スカウト","#ea580c",50,7],["stop","損切り番","#dc2626",19,17],["trend","注目ランキング","#db2777",81,17],
    ["tp","利確ラダー","#b45309",16,35],["whale","大口ウォッチ","#0d9488",84,35],["exec","約定・売却テスト","#16a34a",16,52],
    ["kol","KOLウォッチ","#7c3aed",84,52],["sizer","資金ルール","#9333ea",16,69],["flow","買い手の流れ","#2563eb",84,69],
    ["picker","点数づけ","#ca8a04",19,87],["bundle","まとめ買い検出","#be123c",50,94],["rug","安全チェック","#e11d48",81,87]];
  const AB={scout:"SC",stop:"SL",trend:"TR",tp:"TP",whale:"WH",exec:"EX",kol:"KL",sizer:"SZ",flow:"HF",picker:"PK",bundle:"BH",rug:"RG"};
  const sec=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));return s<60?s+"秒前":s<3600?Math.floor(s/60)+"分前":Math.floor(s/3600)+"時間前"};
  const hub=$("#hub");
  hub.insertAdjacentHTML("beforeend",AG.map(([id,nm,c,x,y])=>`<div class="ag idle" id="ag-${id}" style="left:${x}%;top:${y}%;--glow:${c}22;border-color:var(--line)"><span class="ic" style="background:${c}">${AB[id]}</span><div class="bd"><div class="nm" style="color:${c}">${nm}</div><div class="tx">待機中</div></div></div>`).join(""));
  $("#wires").innerHTML=AG.map(([id,,c,x,y])=>`<line id="w-${id}" class="wire" x1="${x}" y1="${y}" x2="50" y2="50" style="--c:${c}"/>`).join("");
  const seen=new Set();let first=true,last=null,fails=0;
  const cls={BUY:"g",SELL:"b",STOP:"r",ERROR:"r",NO_PRICE:"r",REJECT:"d",SKIP:"d",WATCH:"o",WAIT:"o",NEW:"p",OK:"g",HOLD:"b"};
  const lab={BUY:"BUY ",SELL:"SELL",STOP:"STOP",ERROR:"ERR ",NO_PRICE:"N/A ",REJECT:"SKIP",SKIP:"SKIP",WATCH:"WAIT",WAIT:"WAIT",NEW:"NEW "};
  const hm=t=>{const d=new Date(t*1000);return d.toTimeString().slice(0,8)};
  function spark(path,up){if(!path||path.length<2)return'<svg viewBox="0 0 100 40"></svg>';const mn=Math.min(...path),mx=Math.max(...path),
    X=i=>i/(path.length-1)*100,Y=v=>36-(v-mn)/((mx-mn)||1)*32,pts=path.map((v,i)=>X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" "),c=up?"#16a34a":"#e5484d";
    return `<svg viewBox="0 0 100 40" preserveAspectRatio="none"><polygon points="0,40 ${pts} 100,40" fill="${c}" opacity=".1"/><polyline points="${pts}" fill="none" stroke="${c}" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>`}
  function paint(){
    if(!last){$("#corecap").innerHTML=`DESK <span>· ${fails>2?"ライブ情報なし":"接続中"}</span>`;return}
    const L=last,n=L.now||{},age=Date.now()/1000-L.ts,busy=n.icon!=="idle"&&n.icon!=="start";
    $("#core").className="core"+(busy&&age<30?" busy":"");
    $("#corecap").innerHTML=age>90?`DESK <span>· ${sec(L.ts)}から停止?</span>`:busy?`DESK <span>· ${n.sym?esc(n.sym)+" ":""}${n.agent?"作業中":"巡回中"}</span>`:`DESK <span>· 待機中</span>`;
    const A=L.agents||{},now=Date.now()/1000;let on=0;
    AG.forEach(([id,,c])=>{const a=A[id],el=$("#ag-"+id),w=$("#w-"+id);const act=a&&n.agent===id&&now-a.t<8,hot=a&&now-a.t<120;
      if(a&&now-a.t<7200)on++;
      el.className="ag"+(act?" act":"")+(a?"":" idle");el.style.borderColor=act?c:hot?c+"66":"var(--line)";
      el.querySelector(".tx").textContent=a?a.x+" ・ "+sec(a.t):"待機中";
      w.setAttribute("class","wire"+(act?" act":hot?" hot":""));w.style.stroke=act||hot?c:"";});
    $("#online").textContent="● "+on+"/12 稼働（2時間以内）";
    document.querySelectorAll("#term .tm").forEach(e=>{});
  }
  function render(L){
    const st=L.status||{},H=st.head;
    if(H){const r=H.balance/H.start;$("#hbal").textContent="$"+H.balance.toFixed(2);
      $("#hstat").innerHTML=`${r.toFixed(2)}× from $${H.start} ・ ${H.n} trades ・ <span class="pos">${H.w}W</span>/<span class="neg">${H.l}L</span>`}
    $("#hsub").innerHTML=`<span class="pos">●</span> 稼働中 ・ pump.fun卒業コイン ・ 練習（本物のお金は動かない） ・ 保有 ${H?H.open+"/"+H.max:"-"} ・ 審査待ち ${st.waiting??"-"}`;
    const CH=st.charts||[];$("#chst").textContent=CH.length?CH.length+"件":"審査待ち";
    $("#charts").innerHTML=CH.length?CH.map(c=>`<div class="cc${c.st==="保有中"?" hold":""}"><div class="h"><a href="https://dexscreener.com/solana/${encodeURIComponent(c.m)}" target="_blank" rel="noopener">$${esc(c.s||"?")}</a><span class="${c.chg>=0?"pos":"neg"}">${c.chg>=0?"+":""}${(c.chg*100).toFixed(1)}%</span></div>
      <div class="px">${c.p?(+c.p).toPrecision(4):"-"}</div><div class="mc">流動性 $${c.liq?Math.round(c.liq/1000)+"K":"-"}</div>${spark(c.path,c.chg>=0)}<div class="ft"><span>${c.path.length}点</span><span>${esc(c.st)}</span></div></div>`).join("")
      :'<div class="small">まだ追跡中のコインがありません（審査すると、ここに値動きが出ます）</div>';
    const F=st.flow;
    if(F&&F.ladder){const mx=Math.max(1,...F.ladder.map(r=>Math.max(r.buy,r.sell)));$("#ladsym").textContent="$"+F.symbol+" ・ "+sec(F.t);
      $("#lad").innerHTML=F.ladder.map(r=>`<div class="lr"><span class="n">${r.ns}件</span><div class="tk s"><i style="width:${(r.sell/mx*100).toFixed(1)}%"></i></div><span>${esc(r.b)}</span></div>`).join("")+
        `<div class="mid"><span class="${F.net>=0?"pos":"neg"}">${F.net>=0?"+":"-"}$${Math.abs(F.net).toLocaleString()}</span><span>直近10分の差し引き</span></div>`+
        F.ladder.slice().reverse().map(r=>`<div class="lr"><span class="n">${r.nb}件</span><div class="tk b"><i style="width:${(r.buy/mx*100).toFixed(1)}%"></i></div><span>${esc(r.b)}</span></div>`).join("")+
        `<div class="small" style="margin-top:6px">上の赤＝売り、下の緑＝買い（金額帯ごとの合計）</div>`;
      const W=F.whales||[];$("#radsym").textContent="$"+F.symbol;
      const dots=W.map((w,i)=>{const ang=(w.ago/600)*2*Math.PI-Math.PI/2,lr=Math.log10(Math.max(w.usd,500)),rad=12+34*Math.min(1,(lr-2.7)/2);
        return `<circle cx="${(50+rad*Math.cos(ang)).toFixed(1)}" cy="${(50+rad*Math.sin(ang)).toFixed(1)}" r="${(1.6+Math.min(2.4,(lr-2.7)*1.4)).toFixed(1)}" fill="${w.k==="buy"?"#16a34a":"#e5484d"}" opacity=".85"/>`}).join("");
      $("#radar").innerHTML=`<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="46" fill="none" stroke="#e3e6eb"/><circle cx="50" cy="50" r="31" fill="none" stroke="#e3e6eb"/><circle cx="50" cy="50" r="16" fill="none" stroke="#e3e6eb"/>
        <line x1="4" y1="50" x2="96" y2="50" stroke="#eef0f3"/><line x1="50" y1="4" x2="50" y2="96" stroke="#eef0f3"/>
        <g class="sweep"><path d="M50 50 L96 50 A46 46 0 0 0 82.5 17.5 Z" fill="#16a34a" opacity=".12"/><line x1="50" y1="50" x2="96" y2="50" stroke="#16a34a" stroke-width=".8"/></g>
        ${dots}<circle cx="50" cy="50" r="4.5" fill="#121722"/><circle cx="50" cy="50" r="1.8" fill="#fff"/></svg>
        <div class="lg"><span><span class="pos">●</span> 大口の買い</span><b class="pos">${F.wb}人</b></div><div class="lg"><span><span class="neg">●</span> 大口の売り</span><b class="neg">${F.ws}人</b></div>
        <div class="lg"><span>差し引き $${esc(F.symbol)}</span><b class="${F.net>=0?"pos":"neg"}">${F.net>=0?"+":"-"}$${(Math.abs(F.net)/1000).toFixed(1)}k</b></div>
        <div class="small">中心から遠いほど大きい買い・売り。1周＝10分（真上が今）</div>`}
    else{$("#lad").innerHTML='<div class="small">コインを審査すると、そのコインの直近10分の売り買いがここに出ます</div>';$("#radar").innerHTML='<div class="small">審査中のコインの大口（1回$500以上）の動きがここに出ます</div>'}
    const FE=(L.feed||[]).slice(0,40);$("#lcount").textContent=(L.steps||0).toLocaleString()+" steps";
    const n=L.now||{};
    $("#term").innerHTML=`<div class="l cur">&gt; ${n.sym?esc(n.sym)+" ":""}${esc(n.text||"")}</div>`+FE.map(f=>{const k=f.t+f.x,isNew=!first&&!seen.has(k);seen.add(k);
      return `<div class="l${isNew?" new":""}"><span class="tm">${hm(f.t)}</span> <span class="${cls[f.r]||"b"}">${lab[f.r]||"... "}</span> ${f.s?`<span class="b">${esc(f.s)}</span> `:""}${esc(f.x)}</div>`}).join("");
    first=false;
  }
  async function poll(){
    try{const r=await fetch("live.json?"+Date.now(),{cache:"no-store"});if(!r.ok)throw 0;const L=await r.json();
      fails=0;if(!last||L.ts!==last.ts){last=L;render(L)}}catch(e){fails++}
    paint();
  }
  poll();setInterval(poll,2000);setInterval(paint,1000);
})();
</script></body></html>
"""
