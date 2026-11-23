"""Builds site/desk.html (phone page for the paper desk)."""
import json
import time
from pathlib import Path

from . import config as C
from . import portfolio as P
from . import tracker as T
from . import journal as J


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
        "journal": jstats, "jtrades": jtr, "bvl": bvl, "risk_pct": J.RISK_PCT,
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
/* live "now doing" card */
.lv{border-color:var(--line)}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--red);margin-right:6px;animation:pulse 1.2s infinite}
.dot.off{background:var(--mut);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,93,93,.7)}70%{box-shadow:0 0 0 9px rgba(255,93,93,0)}100%{box-shadow:0 0 0 0 rgba(255,93,93,0)}}
.now{background:#11141a;border-radius:12px;padding:12px;margin-bottom:10px}
.now .t{font-size:15px;font-weight:700;line-height:1.45}.now .k{width:10px;height:10px;margin:0 8px 0 0;vertical-align:1px}
.sym{display:inline-block;background:#232836;color:var(--acc);border-radius:7px;padding:1px 8px;margin-right:6px;font:700 13px ui-monospace,Menlo,monospace}
.scan{height:4px;border-radius:4px;background:#1d2130;overflow:hidden;margin-top:10px;position:relative}
.scan i{position:absolute;top:0;left:-40%;width:40%;height:100%;background:linear-gradient(90deg,transparent,var(--acc),transparent);animation:sweep 1.1s linear infinite}
.scan.idle i{background:linear-gradient(90deg,transparent,#3b4152,transparent);animation-duration:3s}
@keyframes sweep{to{left:100%}}
.ms{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.ms span{background:#11141a;border-radius:8px;padding:4px 8px;font-size:11.5px;color:var(--mut)}.ms b{color:var(--tx)}
.lf{display:flex;gap:8px;align-items:flex-start;padding:7px 4px;border-top:1px solid var(--line);font-size:13px;border-radius:6px}
.k{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;background:var(--acc)}.lf .k{margin-top:6px}
.k-buy{background:var(--grn)}.k-sell{background:var(--acc)}.k-stop,.k-error{background:var(--red)}.k-reject,.k-skip,.k-idle,.k-start{background:#5b6170}.k-wait,.k-watch{background:var(--org)}.k-new{background:#b69cff}.k-work,.k-check,.k-hold{background:var(--acc)}
.lf .ic{width:22px;text-align:center;flex:none}.lf .ag{flex:none;width:44px;text-align:right;font-size:11px;color:var(--mut);padding-top:2px}
.new{animation:flash 2.2s ease-out}@keyframes flash{0%{background:#23314d;transform:translateY(-6px);opacity:.2}25%{opacity:1;transform:none}100%{background:transparent}}
.rr{display:flex;gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:13px}
.rb{flex:none;width:90px;height:10px;background:#11141a;border-radius:5px;position:relative}.rb::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#3b4152}
.rb i{position:absolute;top:0;height:100%;border-radius:5px}
.cmp{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.cmp div{background:#11141a;border-radius:10px;padding:9px;font-size:11.5px;color:var(--mut)}.cmp b{display:block;font-size:17px;color:var(--tx)}
.r{font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:99px;margin-left:4px;white-space:nowrap;display:inline-block}
.r-BUY{background:#0f2e1d;color:var(--grn)}.r-SELL{background:#16263f;color:var(--acc)}.r-STOP,.r-ERROR,.r-NO_PRICE{background:#3a1416;color:var(--red)}
.r-REJECT,.r-SKIP{background:#2a2d35;color:var(--mut)}.r-WATCH,.r-WAIT{background:#3a2810;color:var(--org)}.r-NEW{background:#26203a;color:#b69cff}
#lfeed{max-height:430px;overflow-y:auto}
</style></head><body>
<div class="nav"><a href="./">レーダー</a><a class="on" href="desk.html">デスク</a></div>
<h1>練習売買デスク <span class="small">（ペーパートレード）</span></h1>
<div class="sub" id="upd"></div>
<div id="banner"></div>
<div class="card lv"><h2><span class="dot" id="ldot"></span>いま実行中 <span class="small" id="lage">接続中…</span></h2>
<div class="now"><div class="t" id="lnow">ライブ情報を待っています…</div><div class="small" id="lsince"></div><div class="scan" id="lscan"><i></i></div></div>
<div class="ms" id="lms"></div>
<div id="lfeed"></div></div>
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
ルール：1回 $5〜10 ／ 売り方E：−30%で損切り／+25%まで上がったら高値から−12%で全部利確（小さな勝ちを確保）／+50%で1/3売って残りは負けなし／+150%で1/3／最後の1/3は高値から−35%まで持つ ／ 最大5件 ／ 1日 −$10 で新規停止 ／ 累計 −$30 で完全停止 ／ 急落で損切りが効かない前提（−50%）で枠を計算</div>
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
$("#banner").innerHTML=D.halted?`<div class="banner b-red">完全停止中：${D.halted}</div>`:D.kill?`<div class="banner b-red">停止ファイルにより停止中</div>`:
 dl>=D.limits.daily?`<div class="banner b-org">今日の損失上限に到達 — 明日まで新規購入なし</div>`:stale?`<div class="banner b-org">3時間以上更新されていません</div>`:`<div class="banner b-grn">● 稼働中（練習モード）</div>`;
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
  $("#vc").innerHTML=`<h2>検証：もし買っていたら <span class="small">（6時間追跡・4つの売り方を同じ値動きで比較）</span></h2>
  <div class="small">追跡中 ${D.tracking}件 ・ 結果が出た ${D.outcomes_n}件。見送ったコインの成績が良ければ、そのルールは厳しすぎる証拠です。</div>
  ${D.outcomes_n?tbl("売り方の比較（同じコイン・違う売りルール）",V.profiles)+tbl("判断別",dec)+tbl("見送り理由別",V.reason)+tbl("流動性別",V.liq,["$30k以上","$10k〜30k","$5k〜10k","$5k未満","不明"])+tbl("点数別",V.score,["60以上","40〜59","40未満"])+tbl("保有者数別",V.holders,["75人未満","75〜300人","300人以上"])+tbl("狙撃ボットの保有率別",V.snipers,["5%未満","5〜20%","20%以上"])+tbl("作成者の過去",V.creator):'<div class="small" style="margin-top:8px">まだ結果がありません（審査から4時間後に出始めます）</div>'}`;
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
      <line x1="0" x2="${W}" y1="${Y(D.limits.start)}" y2="${Y(D.limits.start)}" stroke="#3b4152" stroke-dasharray="4 4"/>
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
  const esc=x=>String(x==null?"":x).replace(/[<>&"]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
  const RN={BUY:"買い",SELL:"売り",STOP:"損切り",REJECT:"見送り",SKIP:"除外",WATCH:"様子見",WAIT:"待ち",NEW:"新着",ERROR:"エラー",NO_PRICE:"価格なし"};
  const sec=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));return s<60?s+"秒前":s<3600?Math.floor(s/60)+"分前":Math.floor(s/3600)+"時間前"};
  const seen=new Set();let first=true,last=null,fails=0;
  function paint(){
    if(!last){$("#lage").textContent=fails>2?"ライブ情報なし（iMacが新しい版で再起動すると表示されます）":"接続中…";$("#ldot").className="dot off";return}
    const L=last,n=L.now||{},age=Date.now()/1000-L.ts,busy=n.icon!=="idle"&&n.icon!=="start";
    $("#ldot").className=age>90?"dot off":"dot";
    $("#lage").textContent=age>90?sec(L.ts)+"から動きがありません":"・ "+sec(L.ts)+"に更新";
    $("#lnow").innerHTML=`<span class="k k-${esc(n.icon)}"></span>${n.sym?`<span class="sym">${esc(n.sym)}</span>`:""}${esc(n.text)}`;
    $("#lsince").textContent=n.since?(busy?"この作業を始めて "+Math.max(0,Math.round(Date.now()/1000-n.since))+"秒":"待機 "+Math.max(0,Math.round(Date.now()/1000-n.since))+"秒"):"";
    $("#lscan").className="scan"+(busy?"":" idle");
    document.querySelectorAll("#lfeed .ag").forEach(e=>e.textContent=sec(+e.dataset.t));
  }
  function render(L){
    const st=L.status||{};
    $("#lms").innerHTML=st.cash!=null?`<span>残高 <b>$${(+st.cash).toFixed(2)}</b></span><span>保有 <b>${st.open}</b></span><span>審査済 <b>${st.evaluated}</b></span><span>審査待ち <b>${st.waiting}</b></span><span>買い <b>${st.bought}</b></span><span>処理ステップ <b>${(L.steps||0).toLocaleString()}</b></span>`:"";
    $("#lfeed").innerHTML=(L.feed||[]).map(f=>{const k=f.t+f.x,isNew=!first&&!seen.has(k);seen.add(k);
      const s=f.s?(f.m?`<a class="sym" href="https://dexscreener.com/solana/${encodeURIComponent(f.m)}" target="_blank" rel="noopener">${esc(f.s)}</a>`:`<span class="sym">${esc(f.s)}</span>`):"";
      return `<div class="lf${isNew?" new":""}"><span class="k k-${esc(f.i)}"></span><div style="flex:1;min-width:0">${s}${esc(f.x)}${f.r&&RN[f.r]?`<span class="r r-${esc(f.r)}">${RN[f.r]}</span>`:""}</div><span class="ag" data-t="${f.t}">${sec(f.t)}</span></div>`}).join("")||'<div class="small">まだ動きがありません</div>';
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
