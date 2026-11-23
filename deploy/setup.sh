#!/usr/bin/env bash
# One-time setup on a free always-on Linux server (Ubuntu). Run:
#   curl -fsSL https://raw.githubusercontent.com/ttiillol/sniper-radar/main/deploy/setup.sh | bash
set -euo pipefail
REPO_DIR="$HOME/sniper-radar"
sudo apt-get update -y && sudo apt-get install -y git python3 python3-venv
[ -d "$REPO_DIR" ] || git clone https://github.com/ttiillol/sniper-radar "$REPO_DIR"
cd "$REPO_DIR"
python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/python -m pytest -q tests          # refuse to start if any rule test fails

ENV=/etc/sniper-radar.env
if [ ! -f "$ENV" ]; then
  read -rp "Helius RPC URL (https://mainnet.helius-rpc.com/?api-key=...): " RPC </dev/tty
  read -rp "GitHub token (contents:write for sniper-radar only): " GHT </dev/tty
  echo "RPC_URL=$RPC" | sudo tee "$ENV" >/dev/null && sudo chmod 600 "$ENV"
  git remote set-url origin "https://x-access-token:${GHT}@github.com/ttiillol/sniper-radar"
fi

chmod +x deploy/*.sh
U=$(whoami)
unit() {  # name, command
sudo tee /etc/systemd/system/sr-$1.service >/dev/null <<UNIT
[Unit]
Description=sniper-radar $1
After=network-online.target
[Service]
User=$U
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV
ExecStart=$REPO_DIR/$2
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
}
unit radar deploy/run_radar.sh
unit desk deploy/run_desk.sh
unit publish deploy/publish.sh
sudo sed -i 's/Restart=always/Type=oneshot/' /etc/systemd/system/sr-publish.service
sudo tee /etc/systemd/system/sr-publish.timer >/dev/null <<UNIT
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now sr-radar sr-desk sr-publish.timer
echo "✅ running. Check: systemctl status sr-radar sr-desk"
