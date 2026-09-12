#!/usr/bin/env bash
# Deploy StartupOS to the Azure VM with Docker Compose.
# Run from your Mac inside the repo:  ./scripts/deploy_azure.sh
# Requires: ssh access to $VM (your key), rsync. First run installs Docker on the VM if missing.
set -euo pipefail

VM="${VM:-azureuser@20.106.244.178}"
REMOTE_DIR="${REMOTE_DIR:-~/startupos}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$HERE/.env" ]; then
  echo "no .env in $HERE — copy .env.example and fill keys first" >&2; exit 1
fi

echo "→ syncing $HERE to $VM:$REMOTE_DIR"
rsync -az --delete \
  --exclude '.git' --exclude 'web/node_modules' --exclude 'web/.next' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude 'pgdata' \
  "$HERE/" "$VM:$REMOTE_DIR/"

echo "→ ensuring docker on the VM"
ssh "$VM" 'command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && echo "docker installed — log out/in once for group membership")'

echo "→ building and starting services (db, migrate, ingest, daemon, api)"
ssh "$VM" "cd $REMOTE_DIR && sudo docker compose up -d --build db migrate && sudo docker compose up -d --build ingest daemon api"

echo "→ status"
ssh "$VM" "cd $REMOTE_DIR && sudo docker compose ps"

cat <<EOF

Done. Useful commands on the VM (ssh $VM):
  cd $REMOTE_DIR && sudo docker compose logs -f daemon      # agent daemon
  cd $REMOTE_DIR && sudo docker compose logs -f ingest      # cron workers
  cd $REMOTE_DIR && sudo docker compose exec api python -m signals.digest
  curl http://localhost:8000/cockpit                        # API (open port 8000 in the NSG to reach it remotely)
Update later: re-run this script. Stop: ssh $VM "cd $REMOTE_DIR && sudo docker compose down".
EOF
