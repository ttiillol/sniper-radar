#!/usr/bin/env bash
# Live radar: listens all day; saves every 10 coins. Restarts itself daily (systemd also restarts on crash).
cd "$(dirname "$0")/.."
# Helius free = ~33k credits/day. Radar uses ~5 credits/coin -> ~3,500 coins/day (leaves room for the desk).
exec ./.venv/bin/python scanner.py --minutes 1440 --max-tokens "${RADAR_COINS_PER_DAY:-3500}"
