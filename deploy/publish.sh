#!/usr/bin/env bash
# Every 10 min: push the latest data so GitHub rebuilds the phone pages.
cd "$(dirname "$0")/.."
git add data >/dev/null 2>&1
git diff --cached --quiet && exit 0
git -c user.name="sniper-radar-server" -c user.email="actions@users.noreply.github.com" commit -qm "live data"
for i in 1 2 3; do git pull -q --rebase -X theirs origin main && git push -q && exit 0; sleep $((i*5)); done
exit 1
