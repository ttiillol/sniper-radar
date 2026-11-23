#!/bin/bash
# Started by launchd at login and restarted if it ever stops.
cd "$(dirname "$0")/.."
caffeinate -is -w $$ &                 # keep the Mac awake while running (the screen may still sleep)
# pick up code improvements without touching local data
git fetch -q origin 2>/dev/null && git checkout -q origin/main -- scanner.py build_page.py desk tests deploy requirements.txt 2>/dev/null
./.venv/bin/pip install -q -r requirements.txt 2>/dev/null
./.venv/bin/python -m pytest -q tests >/dev/null 2>&1 || { echo "tests failed - not starting"; sleep 600; exit 1; }

export DESK_TICK=10
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
if [ -x bin/cloudflared ]; then
  : > data/tunnel.log
  bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8787 >> data/tunnel.log 2>&1 &
  T=$!
  for i in $(seq 1 30); do
    U=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' data/tunnel.log | head -1)
    [ -n "$U" ] && break; sleep 2
  done
  if [ -n "$U" ]; then
    echo "$U/desk.html" > data/dashboard_url.txt
    ./.venv/bin/python -c "from desk import notify as N; N.send('スマホ用ダッシュボード', 'タップで開きます（iMacが再起動するとアドレスが変わり、新しいアドレスをまた通知します）', click='$U/desk.html')"
  fi
fi

wait $R $D
kill $H $T 2>/dev/null
