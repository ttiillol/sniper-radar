"""Builds site/index.html (phone-friendly report) from data/*.json."""
import json
import time
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"


def load(name, default):
    try:
        return json.loads((DATA / name).read_text())
    except Exception:
        return default


def main():
    import scanner
    wallets, tokens, meta = scanner.rebuild(DATA)      # merge every run log
    scanner.write_derived(wallets, tokens, meta, DATA)
    # wallets seen early on 2+ coins are worth listing; keep the page light
    ranked = sorted(((w, v) for w, v in wallets.items() if v["count"] >= 2),
                    key=lambda x: (-x[1]["count"], -x[1]["block0"]))[:400]
    payload = {
        "generated": int(time.time()),
        "meta": meta,
        "walletsTotal": len(wallets),
        "ranked": [[w, v["count"], v["block0"], v["last"], v["recent"]] for w, v in ranked],
        # full lookup table for the search box: wallet -> [count, block0]
        "all": {w: [v["count"], v["block0"]] for w, v in wallets.items()},
        "tokens": tokens[:60],
    }
    groups = scanner.wallet_groups(tokens)
    payload["groups"] = [[g["wallets"], g["together"]] for g in groups[:30]]
    SITE.mkdir(exist_ok=True)
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"),
                                                   ensure_ascii=False).replace("</", "<\\/"))
    (SITE / "index.html").write_text(html, encoding="utf-8")
    print("site/index.html written,", len(html) // 1024, "KB")

    # Machine-readable feed for other bots (e.g. the trading desk):
    #   bots     = wallets that sniped 5+ coins  -> never copy, treat as dumpers
    #   suspects = 3-4 coins
    #   tokens   = per-coin count of block-0 (bundle) buyers and snipers
    feed = {
        "updated": int(time.time()),
        "tokens_checked": meta.get("tokens_total", 0),
        "bots": [w for w, v in wallets.items() if v["count"] >= 5],
        "groups": [g["wallets"] for g in groups],     # wallets that always buy together (one operator)
        "suspects": [w for w, v in wallets.items() if 3 <= v["count"] < 5],
        "tokens": {t["mint"]: {"block0": t["block0"], "snipers": t["snipers"],
                               "early": [e["wallet"] for e in t.get("early", [])]}
                   for t in tokens},
    }
    (SITE / "bots.json").write_text(json.dumps(feed, separators=(",", ":")))
    print("site/bots.json written:", len(feed["bots"]), "bots,", len(feed["tokens"]), "tokens")

    from desk import page as desk_page
    desk_page.build(SITE)
    print("site/desk.html written")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>狙撃ボット・レーダー</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262a33;--tx:#e8eaf0;--mut:#8b91a0;--red:#ff5d5d;--org:#ffa53d;--grn:#3ddc84;--acc:#6aa8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;padding:16px 14px 60px;max-width:640px;margin:auto}
h1{font-size:20px;margin:4px 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}
h2{font-size:15px;margin:0 0 10px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.stat{background:#11141a;border-radius:10px;padding:10px;text-align:center}.stat b{display:block;font-size:20px}.stat span{font-size:11px;color:var(--mut)}
input{width:100%;padding:12px;border-radius:10px;border:1px solid var(--line);background:#0b0d11;color:var(--tx);font-size:14px}
button{margin-top:8px;width:100%;padding:12px;border:0;border-radius:10px;background:var(--acc);color:#04122a;font-weight:700;font-size:15px}
#res{margin-top:10px;font-size:14px}
.verdict{padding:12px;border-radius:10px;font-weight:700}
.v-red{background:#3a1416;color:var(--red)}.v-org{background:#3a2810;color:var(--org)}.v-grn{background:#0f2e1d;color:var(--grn)}
.tabs{display:flex;gap:6px;margin-bottom:10px}.tab{flex:1;padding:8px;border-radius:9px;background:#11141a;text-align:center;font-size:13px;color:var(--mut)}.tab.on{background:var(--acc);color:#04122a;font-weight:700}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.addr{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--acc);text-decoration:none}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;white-space:nowrap}.t-red{background:#3a1416;color:var(--red)}.t-org{background:#3a2810;color:var(--org)}.t-pur{background:#2a1d3d;color:#c49bff}
.grow{flex:1;min-width:0}.small{font-size:11.5px;color:var(--mut)}.num{font-weight:700;font-size:16px;min-width:34px;text-align:right}
.copy{background:none;border:1px solid var(--line);color:var(--mut);width:auto;margin:0;padding:4px 8px;font-size:11px;font-weight:400}
.hrow{display:flex;justify-content:space-between;font-size:13px;padding:3px 0}.ok{color:var(--grn)}.warn{color:var(--org)}.bad{color:var(--red)}
.help{font-size:12.5px;color:var(--mut)}.help b{color:var(--tx)}
</style></head><body>
<div style="display:flex;gap:8px;margin-bottom:12px"><a href="./" style="flex:1;text-align:center;padding:8px;border-radius:10px;background:var(--acc);color:#04122a;font-weight:700;font-size:13px;text-decoration:none">🎯 レーダー</a><a href="desk.html" style="flex:1;text-align:center;padding:8px;border-radius:10px;background:var(--card);border:1px solid var(--line);color:var(--acc);font-size:13px;text-decoration:none">📈 デスク</a></div>
<h1>🎯 狙撃ボット・レーダー</h1>
<div class="sub" id="upd"></div>

<div class="card"><div class="stats">
<div class="stat"><b id="sTok">-</b><span>調べたコイン</span></div>
<div class="stat"><b id="sWal">-</b><span>早撃ち財布</span></div>
<div class="stat"><b id="sBot">-</b><span>ボット濃厚</span></div>
</div></div>

<div class="card" id="health"></div>

<div class="card"><h2>🔎 この財布、真似して大丈夫？</h2>
<input id="q" placeholder="財布アドレスを貼り付け" autocomplete="off" autocapitalize="off" spellcheck="false">
<button id="go">チェック</button><div id="res"></div></div>

<div class="card">
<div class="tabs"><div class="tab on" data-t="w">要注意の財布</div><div class="tab" data-t="t">最近のコイン</div></div>
<button id="bulk" style="margin:0 0 10px">🚫 ボット濃厚の財布をまとめてコピー</button>
<div id="list"></div></div>

<div class="card"><h2>👥 いつも一緒に買う財布グループ</h2><div id="grp"></div></div>

<div class="card help"><h2>見方</h2>
<b>同ブロック買い</b>：コインが作られたのと同じ瞬間に買った財布。作った本人の仲間（まとめ買い）の可能性が高い。<br>
<b>狙撃</b>：作られてから約1秒以内（3ブロック以内）に買った財布。<br>
<b>財布グループ</b>：同じ人が複数の財布でまとめ買いしている可能性が高い。どれか1つでも保有者にいたら要注意。<br>
<b>ボット濃厚</b>：いくつものコインで早撃ちしている財布。真似しても同じ値段では買えないので<b>コピー対象から外す</b>。<br>
※記録に無い＝「観測した範囲では早撃ちしていない」という意味で、安全の保証ではありません。</div>

<script>
const D=__DATA__;
const $=s=>document.querySelector(s);
const ago=t=>{const m=Math.round((Date.now()/1000-t)/60);return m<60?m+"分前":m<1440?Math.round(m/60)+"時間前":Math.round(m/1440)+"日前"};
const short=a=>a.slice(0,5)+"…"+a.slice(-4);
const link=a=>`<a class="addr" href="https://solscan.io/account/${a}" target="_blank" rel="noopener">${short(a)}</a>`;
const level=c=>c>=5?["t-red","ボット濃厚"]:c>=3?["t-org","狙撃の疑い"]:["t-org","早撃ち2回"];
$("#upd").textContent=(D.meta.last_run?"最終更新 "+ago(D.meta.last_run)+" ・ ":"")+"実行 "+(D.meta.runs||0)+" 回";
$("#sTok").textContent=(D.meta.tokens_total||0).toLocaleString();
$("#sWal").textContent=D.walletsTotal.toLocaleString();
$("#sBot").textContent=D.ranked.filter(r=>r[1]>=5).length;

const RS={rate_limited:"窓口が混雑",timeout:"応答が遅い",network:"通信エラー",server_error:"窓口の障害",too_busy:"取引が多すぎ",not_indexed_yet:"記録がまだ無い",rpc_error:"問い合わせエラー",bad_response:"応答が変",auth:"APIキーのエラー",budget:"無料枠の上限",monthly_budget_reached:"今月の無料枠を使い切り",out_of_time:"時間切れ",unexpected:"想定外のエラー",other:"その他"};
(function(){const s=D.meta.last_stats;if(!s){$("#health").style.display="none";return}
  const tried=s.checked+s.skipped, pct=tried?Math.round(s.checked/tried*100):0, cls=pct>=90?"ok":pct>=70?"warn":"bad";
  const rs=Object.entries(s.reasons||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${RS[k]||k} ${v}`).join("・")||"なし";
  const mk=new Date().toISOString().slice(0,7), cr=(D.meta.credits||{})[mk];
  $("#health").innerHTML=`<h2>🩺 直近の実行の調子</h2>
  <div class="hrow"><span>見つけた新コイン</span><b>${s.seen}</b></div>
  <div class="hrow"><span>調べきれた割合</span><b class="${cls}">${pct}%（${s.checked}/${tried}）</b></div>
  ${s.sources?`<div class="hrow"><span>新コインの入手元</span><span class="small">PumpPortal ${s.sources.ws} ・ 予備ルート ${s.sources.poll} ・ 接続失敗 ${s.sources.ws_errors}</span></div>`:""}
  <div class="hrow"><span>再挑戦した数</span><b>${s.retried||0}</b></div>
  <div class="hrow"><span>失敗の理由</span><span class="small">${rs}</span></div>
  ${Object.keys(s.samples||{}).length?`<div class="small" style="margin:4px 0 6px">エラー例：${Object.values(s.samples).map(x=>String(x).replace(/</g,"")).join(" ／ ")}</div>`:""}
  <div class="hrow"><span>データ窓口</span><b>${s.rpc==="helius"?"Helius（専用）":"公共（無料・混みやすい）"}</b></div>
  ${cr!=null?`<div class="hrow"><span>今月の使用クレジット</span><b>${cr.toLocaleString()} / 1,000,000</b></div>`:""}`;
})();
const TT=Math.max(D.meta.tokens_total||1,1);
function check(){
  const a=$("#q").value.trim();if(!a){return}
  const h=D.all[a];let v;
  if(!h) v=`<div class="verdict v-grn">✅ 記録なし</div><div class="small" style="margin-top:6px">調べた${(D.meta.tokens_total||0).toLocaleString()}コインの中では、早撃ちしていません（安全の保証ではありません）。</div>`;
  else{const[c,b]=h;const cls=c>=5?"v-red":"v-org";
    v=`<div class="verdict ${cls}">${c>=5?"🚫 ボット濃厚 — コピー対象外":c>=3?"⚠️ 狙撃の疑い":"⚠️ 早撃ちあり"}</div>
    <div class="small" style="margin-top:6px">早撃ち ${c} 回（うち同ブロック買い ${b} 回）${b>=2?"<br>→ コイン作者のまとめ買い用の財布の可能性あり":""}</div>`}
  $("#res").innerHTML=v+`<div class="small" style="margin-top:6px">${link(a)}</div>`;
}
$("#go").onclick=check;$("#q").addEventListener("keydown",e=>{if(e.key==="Enter")check()});

function wList(){
  if(!D.ranked.length) return '<div class="small">まだデータがありません。何回か実行されると溜まっていきます。</div>';
  return D.ranked.slice(0,150).map(([a,c,b,l,r])=>{const[t,n]=level(c);
    return `<div class="row"><div class="num">${c}</div><div class="grow">${link(a)} <span class="tag ${t}">${n}</span>${b>=2?' <span class="tag t-pur">同ブロック常連</span>':''}
    <div class="small">出現率 ${(c/TT*100).toFixed(1)}% ・ 最近：${r.slice(0,3).join("・")} ／ ${ago(l)}</div></div><button class="copy" data-a="${a}">コピー</button></div>`}).join("");
}
function tList(){
  if(!D.tokens.length) return '<div class="small">まだデータがありません。</div>';
  return D.tokens.map(t=>`<div class="row"><div class="grow"><b>${(t.symbol||"?").replace(/</g,"")}</b> <span class="small">${(t.name||"").replace(/</g,"").slice(0,24)}</span>
    <div class="small">同ブロック買い ${t.block0} ・ 狙撃 ${t.snipers} ・ ${ago(t.time)}</div></div>
    ${t.block0>=3?'<span class="tag t-red">まとめ買い</span>':t.snipers>=5?'<span class="tag t-org">狙撃多</span>':''}
    <a class="addr" href="https://pump.fun/coin/${t.mint}" target="_blank" rel="noopener">開く</a></div>`).join("");
}
let tab="w";const draw=()=>{$("#list").innerHTML=tab==="w"?wList():tList()};
document.querySelectorAll(".tab").forEach(el=>el.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));el.classList.add("on");tab=el.dataset.t;draw()});
$("#list").addEventListener("click",e=>{const b=e.target.closest(".copy");if(!b)return;navigator.clipboard?.writeText(b.dataset.a).then(()=>{b.textContent="✓"}).catch(()=>{$("#q").value=b.dataset.a;check();scrollTo(0,0)})});
$("#bulk").onclick=()=>{const list=D.ranked.filter(r=>r[1]>=5).map(r=>r[0]);const b=$("#bulk");
  if(!list.length){b.textContent="まだボット濃厚の財布はありません";return}
  navigator.clipboard?.writeText(list.join("\n")).then(()=>{b.textContent=`✓ ${list.length}個コピーしました`}).catch(()=>{b.textContent="コピーできませんでした"})};
$("#grp").innerHTML=(D.groups||[]).length?D.groups.map(([ws,n],i)=>`<div class="row"><div class="grow"><b>グループ${i+1}</b> <span class="tag t-red">${ws.length}財布</span> <span class="small">${n}コインで同時に早撃ち</span>
  <div class="small">${ws.slice(0,6).map(a=>link(a)).join(" ")}${ws.length>6?" …":""}</div></div></div>`).join(""):'<div class="small">まだ見つかっていません（3コイン以上で一緒に早撃ちした財布をまとめます）</div>';
draw();
</script></body></html>
"""

if __name__ == "__main__":
    main()
