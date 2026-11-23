"""Builds site/desk.html (phone page for the paper desk)."""
import json
import time
from pathlib import Path

from . import config as C
from . import portfolio as P


def build(site: Path):
    s = P.load_state()
    try:
        lines = C.DECISIONS_LOG.read_text().splitlines()[-150:]
        decisions = [json.loads(x) for x in lines if x.strip()][::-1]
    except FileNotFoundError:
        decisions = []
    slim = [{k: d.get(k) for k in ("ts", "kind", "mint", "symbol", "action", "reason")} for d in decisions]
    mk = time.strftime("%Y-%m", time.gmtime())
    payload = {
        "cash": s["cash"], "realized": s["realized"], "today": P.day_pnl(s),
        "positions": s["positions"], "halted": s["halted"], "kill": C.KILL_FILE.exists(),
        "heartbeat": s["last_heartbeat"], "gaps": s["gaps"][-5:][::-1], "stats": s["stats"],
        "credits": s["credits"].get(mk, 0), "decisions": slim, "rpc": s.get("rpc"),
        "samples": s.get("rpc_samples") or {}, "watch": sum(
            1 for c in s["candidates"].values() if c.get("status") == "waiting"),
        "limits": {"daily": C.DAILY_LOSS_LIMIT, "total": C.TOTAL_LOSS_LIMIT, "max": C.MAX_OPEN_POSITIONS,
                   "start": C.START_CASH},
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                            .replace("</", "<\\/"))
    (site / "desk.html").write_text(html, encoding="utf-8")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>練習売買デスク</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262a33;--tx:#e8eaf0;--mut:#8b91a0;--red:#ff5d5d;--org:#ffa53d;--grn:#3ddc84;--acc:#6aa8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;padding:16px 14px 60px;max-width:640px;margin:auto}
a{color:var(--acc);text-decoration:none}h1{font-size:20px;margin:4px 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.nav{display:flex;gap:8px;margin-bottom:12px}.nav a{flex:1;text-align:center;padding:8px;border-radius:10px;background:var(--card);border:1px solid var(--line);font-size:13px}.nav a.on{background:var(--acc);color:#04122a;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}h2{font-size:15px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.st{background:#11141a;border-radius:10px;padding:10px}.st b{display:block;font-size:20px}.st span{font-size:11px;color:var(--mut)}
.pos{color:var(--grn)}.neg{color:var(--red)}.banner{padding:12px;border-radius:10px;font-weight:700;margin-bottom:14px}
.b-red{background:#3a1416;color:var(--red)}.b-grn{background:#0f2e1d;color:var(--grn)}.b-org{background:#3a2810;color:var(--org)}
.row{padding:9px 0;border-top:1px solid var(--line);font-size:13.5px}.row:first-child{border-top:0}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;margin-right:6px;font-weight:700}
.BUY{background:#0f2e1d;color:var(--grn)}.SELL{background:#16263f;color:var(--acc)}.REJECT{background:#2a2d35;color:var(--mut)}.WATCH{background:#3a2810;color:var(--org)}.RESUME,.PAUSE,.NO_PRICE,.ERROR{background:#3a1416;color:var(--red)}
.small{font-size:11.5px;color:var(--mut)}.bar{height:8px;border-radius:9px;background:#11141a;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--red)}
.filters{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}.f{padding:5px 10px;border-radius:99px;background:#11141a;font-size:12px;color:var(--mut)}.f.on{background:var(--acc);color:#04122a;font-weight:700}
.fd{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line);font-size:13.5px}.fd:first-child{border-top:0}
.lb{font:700 11px ui-monospace,Menlo,monospace;padding:3px 0;width:46px;text-align:center;border-radius:6px;flex:none}
.L-BUY{background:#0f2e1d;color:var(--grn)}.L-SELL{background:#16263f;color:var(--acc)}.L-STOP{background:#3a1416;color:var(--red)}.L-SKIP{background:#2a2d35;color:var(--mut)}
.warn{font-size:12.5px;color:var(--mut)}.warn b{color:var(--org)}
</style></head><body>
<div class="nav"><a href="./">🎯 レーダー</a><a class="on" href="desk.html">📈 デスク</a></div>
<h1>📈 練習売買デスク <span class="small">（ペーパートレード）</span></h1>
<div class="sub" id="upd"></div>
<div id="banner"></div>
<div class="card"><div class="grid">
<div class="st"><b id="eq">-</b><span>資金（保有分は買値で計算）</span></div>
<div class="st"><b id="rl">-</b><span>確定した損益（累計）</span></div>
<div class="st"><b id="td">-</b><span>今日の損益（ハワイ時間）</span></div>
<div class="st"><b id="np">-</b><span>保有中 / 審査待ち</span></div>
</div>
<div class="small" style="margin-top:10px">今日の損失枠 <span id="dl"></span></div><div class="bar"><i id="dlb"></i></div>
<div class="small" style="margin-top:8px">累計の損失枠 <span id="tl"></span></div><div class="bar"><i id="tlb"></i></div>
</div>
<div class="card"><h2>📡 ライブフィード <span class="small">（1分ごとに自動更新）</span></h2><div id="feed"></div></div>
<div class="card" id="hc"></div>
<div class="card"><h2>保有中</h2><div id="poss"></div></div>
<div class="card"><h2>判断の記録</h2>
<div class="filters"><span class="f on" data-f="all">すべて</span><span class="f" data-f="BUY">買い</span><span class="f" data-f="SELL">売り</span><span class="f" data-f="REJECT">見送り</span><span class="f" data-f="WATCH">様子見</span></div>
<div id="dec"></div></div>
<div class="card"><h2>見送った理由ランキング</h2><div id="rej"></div></div>
<div class="card" id="gapc"><h2>止まっていた時間の記録</h2><div id="gaps"></div></div>
<div class="card warn"><h2>⚠️ 注意</h2>
これは<b>練習（ペーパー）</b>です。本物のお金は動きません。ミームコインは数分で価値がほぼゼロになることがあり、損切りが効かずに大きく負けることがあります。
この画面の成績が良くても、本番で同じ結果になる保証はありません。<br><br>
ルール：1回 $5〜10 ／ 損切り −6% ／ 最大5件 ／ 1日 −$10 で新規停止 ／ 累計 −$30 で完全停止 ／ 急落で損切りが効かない前提（−50%）で枠を計算</div>
<script>
const D=__DATA__;const $=s=>document.querySelector(s);
const ago=t=>{if(!t)return"-";const m=Math.round((Date.now()/1000-t)/60);return m<60?m+"分前":m<1440?Math.round(m/60)+"時間前":Math.round(m/1440)+"日前"};
const usd=v=>(v<0?"-$":"$")+Math.abs(v).toFixed(2), sg=v=>(v>=0?"+":"")+usd(v).replace("+","");
const cls=v=>v>0?"pos":v<0?"neg":"";
const open=Object.values(D.positions).reduce((a,p)=>a+p.cost,0);
$("#upd").textContent="最終更新 "+ago(D.heartbeat)+" ・ 審査 "+(D.stats.evaluated||0)+" 件 ・ 買い "+(D.stats.bought||0)+" ・ 売り "+(D.stats.sold||0);
$("#eq").textContent=usd(D.cash+open);
$("#rl").innerHTML=`<span class="${cls(D.realized)}">${sg(D.realized)}</span>`;
$("#td").innerHTML=`<span class="${cls(D.today)}">${sg(D.today)}</span>`;
$("#np").textContent=Object.keys(D.positions).length+"/"+D.limits.max+" ・ "+D.watch;
const dl=Math.max(0,-D.today),tl=Math.max(0,-D.realized);
$("#dl").textContent=`${usd(dl)} / ${usd(D.limits.daily)}`;$("#dlb").style.width=Math.min(100,dl/D.limits.daily*100)+"%";
$("#tl").textContent=`${usd(tl)} / ${usd(D.limits.total)}`;$("#tlb").style.width=Math.min(100,tl/D.limits.total*100)+"%";
const stale=D.heartbeat&&Date.now()/1000-D.heartbeat>3*3600;
$("#banner").innerHTML=D.halted?`<div class="banner b-red">⛔ 完全停止中：${D.halted}</div>`:D.kill?`<div class="banner b-red">⛔ 停止ファイルにより停止中</div>`:
 dl>=D.limits.daily?`<div class="banner b-org">⏸ 今日の損失上限に到達 — 明日まで新規購入なし</div>`:stale?`<div class="banner b-org">⚠️ 3時間以上更新されていません</div>`:`<div class="banner b-grn">● 稼働中（練習モード）</div>`;
const P=Object.entries(D.positions);
$("#poss").innerHTML=P.length?P.map(([m,p])=>`<div class="row"><b>${(p.symbol||"?").replace(/</g,"")}</b> ${usd(p.cost)} ・ ${ago(p.opened)}に購入${p.half_sold?" ・ 半分利確済":""}
 <div class="small">買値 ${p.entry.toPrecision(4)} ・ 最高値 ${(p.peak/p.entry*100-100).toFixed(1)}% ・ <a href="https://dexscreener.com/solana/${m}" target="_blank" rel="noopener">チャート</a></div></div>`).join(""):'<div class="small">保有なし</div>';
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
$("#hc").innerHTML=`<h2>🩺 デスクの調子</h2><div class="small" style="line-height:1.9">
卒業コインの受信 ${st.migrations||0} ・ 補充 ${st.backfilled||0} ・ 審査 ${st.evaluated||0}<br>
無料で除外：${pf}<br>
エラー ${st.errors||0} 件 ・ データ窓口 ${D.rpc==="helius"?"Helius（専用）":"公共（混みやすい）"} ・ 今月 ${(D.credits||0).toLocaleString()} クレジット
${Object.keys(D.samples).length?"<br>エラー例："+Object.values(D.samples).map(x=>String(x).replace(/</g,"")).join(" ／ "):""}</div>`})();
const R=Object.entries(D.stats.rejected||{}).sort((a,b)=>b[1]-a[1]).slice(0,10);
$("#rej").innerHTML=R.length?R.map(([k,v])=>`<div class="row" style="display:flex;justify-content:space-between"><span>${k.replace(/</g,"")}</span><b>${v}</b></div>`).join(""):'<div class="small">まだありません</div>';
$("#gaps").innerHTML=D.gaps.length?D.gaps.map(g=>`<div class="row">${ago(g.ts)} ・ ${Math.round(g.sec/60)}分停止 ・ 再開時の保有 ${g.open}件</div>`).join(""):'<div class="small">記録なし</div>';
</script></body></html>
"""
