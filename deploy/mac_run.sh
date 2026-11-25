#!/bin/bash
# Started by launchd at login and restarted if it ever stops.
cd "$(dirname "$0")/.."
caffeinate -is -w $$ &                 # keep the Mac awake while running (the screen may still sleep)
# pick up code improvements without touching local data
git fetch -q origin 2>/dev/null && git checkout -q origin/main -- scanner.py build_page.py desk tests deploy requirements.txt 2>/dev/null
./.venv/bin/pip install -q -r requirements.txt 2>/dev/null
./.venv/bin/python -m pytest -q tests >/dev/null 2>&1 || { echo "tests failed - not starting"; sleep 600; exit 1; }

export DESK_TICK=3            # scalp: check prices / exits every 3 seconds
export NTFY_TOPIC="sniper-radar-3c6fb034fa4b"   # phone feed: https://ntfy.sh/sniper-radar-3c6fb034fa4b
mkdir -p site bin
rm -f data/dashboard_url.txt          # old tunnel address is dead after a restart

./.venv/bin/python scanner.py --minutes 1440 --max-tokens 3500 &
R=$!
./.venv/bin/python -m desk.main run --minutes 1440 &
D=$!

# phone dashboard: rebuild pages every 2 min, serve them locally, publish through a free Cloudflare quick tunnel
( while kill -0 $D 2>/dev/null; do ./.venv/bin/python build_page.py >/dev/null 2>&1; sleep 120; done ) &
./.venv/bin/python -m http.server 8787 --bind 127.0.0.1 --directory site >/dev/null 2>&1 &
H=$!
if [ ! -x bin/cloudflared ]; then
  case "$(uname -m)" in arm64) A=arm64;; *) A=amd64;; esac
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$A.tgz" | tar -xz -C bin 2>/dev/null
fi
notify() { ./.venv/bin/python -c "import sys; from desk import notify as N; N.send(sys.argv[1], sys.argv[2], click=(sys.argv[3] or None))" "$1" "$2" "${3:-}" 2>/dev/null; }
# Quick tunnels get refused when they are created too often (many restarts in one day): retry with
# growing pauses, tell the phone why, and re-create the tunnel (with a new address) if it ever drops.
tunnel() {
  local n=0 U T
  while kill -0 $D 2>/dev/null && [ $n -lt 10 ]; do
    n=$((n+1))
    : > data/tunnel.log
    bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8787 >> data/tunnel.log 2>&1 &
    T=$!
    U=""
    for i in $(seq 1 45); do
      U=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' data/tunnel.log | head -1)
      [ -n "$U" ] && break; sleep 2
    done
    if [ -n "$U" ]; then
      n=0
      echo "$U/desk.html" > data/dashboard_url.txt
      notify "スマホ用ダッシュボード" "タップで開きます（iMacが再起動するとアドレスが変わり、新しいアドレスをまた通知します）" "$U/desk.html"
      wait $T                                   # tunnel ended: loop and create a new one
      notify "ダッシュボードの接続が切れました" "新しいアドレスを作り直しています（届くまで1〜2分）" ""
    else
      kill $T 2>/dev/null
      WHY=$(grep -iE "429|too many|error|failed" data/tunnel.log | tail -1 | cut -c1-160)
      notify "ダッシュボード準備中（${n}回目の失敗）" "Cloudflareの無料トンネルが作れませんでした。${n}分後に自動でやり直します。理由: ${WHY:-不明}" ""
      sleep $((n*60))
    fi
  done
}
if [ -x bin/cloudflared ]; then
  pkill -f 'cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8787' 2>/dev/null   # leftovers of an earlier run
  tunnel &
  TP=$!
fi

# Auto-update: every 5 minutes look at GitHub; if the PROGRAM files on main changed (not just data),
# tell the phone and stop -> launchd starts this script again, which takes the new version.
# Never within 30 minutes of a start (Cloudflare refuses tunnels when we restart too often).
code_id() { git ls-tree "$1" scanner.py build_page.py desk tests deploy requirements.txt 2>/dev/null | git hash-object --stdin; }
STARTED=$(date +%s)
RUNNING=$(code_id HEAD)
git fetch -q origin 2>/dev/null && RUNNING=$(code_id origin/main)
(
  while kill -0 $D 2>/dev/null; do
    sleep 300
    [ $(( $(date +%s) - STARTED )) -lt 1800 ] && continue
    git fetch -q origin 2>/dev/null || continue
    NEW=$(code_id origin/main)
    if [ -n "$NEW" ] && [ "$NEW" != "$RUNNING" ]; then
      notify "新しい版を見つけました" "GitHubに新しいプログラムがあるので、自動で再起動して取り込みます（1〜2分。ダッシュボードのアドレスが変わります）" ""
      kill $R $D 2>/dev/null
      break
    fi
  done
) &
UP=$!

# Watchdog: the desk saves data/desk/state.json at the end of every cycle (every few seconds).
# If it has not been saved for 10 minutes the desk is stuck (11-25: frozen ~14 h until the daily restart):
# tell the phone and stop -> launchd starts everything again.
(
  sleep 600
  while kill -0 $D 2>/dev/null; do
    sleep 60
    M=$(date -r data/desk/state.json +%s 2>/dev/null || echo 0)
    AGE=$(( $(date +%s) - M ))
    if [ "$M" -gt 0 ] && [ $AGE -gt 600 ]; then
      notify "デスクが固まっていました" "$((AGE / 60))分間動きがなかったので、自動で起動し直します（1〜2分。ダッシュボードのアドレスが変わります）" ""
      kill $R $D 2>/dev/null
      sleep 5
      kill -9 $R $D 2>/dev/null
      break
    fi
  done
) &
WD=$!

wait $R $D
kill $UP $WD 2>/dev/null
kill $H $TP 2>/dev/null
pkill -f 'cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8787' 2>/dev/null
