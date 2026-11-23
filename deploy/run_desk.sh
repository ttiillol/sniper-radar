#!/usr/bin/env bash
# Live paper desk: checks prices/exits every 10 s, runs all day, then restarts (state resumes).
cd "$(dirname "$0")/.."
export DESK_TICK="${DESK_TICK:-10}"
exec ./.venv/bin/python -m desk.main run --minutes 1440
