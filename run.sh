#!/usr/bin/env bash
# Paper-trading desk. Usage:
#   ./run.sh              loop (default 50 min; DESK_MINUTES to change)
#   ./run.sh --once       one cycle
#   ./run.sh status | summary | rug <mint>
# Stop safely: touch data/KILL
cd "$(dirname "$0")"
exec python3 -m desk.main "$@"
