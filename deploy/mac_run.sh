#!/bin/bash
# Started by launchd at login and restarted if it ever stops.
cd "$(dirname "$0")/.."
caffeinate -is -w $$ &                 # keep the Mac awake while running (the screen may still sleep)
# pick up code improvements without touching local data
git fetch -q origin 2>/dev/null && git checkout -q origin/main -- scanner.py build_page.py desk tests deploy requirements.txt 2>/dev/null
./.venv/bin/pip install -q -r requirements.txt 2>/dev/null
./.venv/bin/python -m pytest -q tests >/dev/null 2>&1 || { echo "tests failed - not starting"; sleep 600; exit 1; }
export DESK_TICK=10
./.venv/bin/python scanner.py --minutes 1440 --max-tokens 3500 &
R=$!
./.venv/bin/python -m desk.main run --minutes 1440 &
D=$!
wait $R $D
