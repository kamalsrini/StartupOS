#!/usr/bin/env bash
# Deploy StartupOS to the Azure VM with Docker Compose.
# Run from your Mac inside the repo:  ./scripts/deploy_azure.sh
# Requires: ssh access to $VM (your key), rsync. First run installs Docker on the VM if missing.
#
# Two modes, decided by STARTUPOS_DOMAIN in .env:
#   set    → the public edge: db, migrate, ingest, daemon, api, web, caddy. HTTPS on your hostname.
#   unset  → the old set (db, migrate, ingest, daemon, api) on the VM's loopback only. Nothing public.
# Full runbook, including DNS and the NSG rules: docs/HOSTING.md
set -euo pipefail

VM="${VM:-azureuser@20.106.244.178}"
REMOTE_DIR="${REMOTE_DIR:-~/startupos}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$HERE/.env" ]; then
  echo "no .env in $HERE — copy .env.example and fill keys first" >&2; exit 1
fi

# Read a KEY=value out of .env without sourcing it. Handles the `KEY=value   # trailing comment` shape that
# .env.example uses, and strips surrounding quotes. Last assignment wins, as with a real dotenv loader.
env_get() {
  sed -n "s/^[[:space:]]*$1=//p" "$HERE/.env" \
    | tail -n1 \
    | sed -e 's/[[:space:]]\{1,\}#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//'
}

DOMAIN="$(env_get STARTUPOS_DOMAIN)"
PREFIX="$(env_get STARTUPOS_PATH_PREFIX)"
PUBLIC_URL="$(env_get STARTUPOS_PUBLIC_URL)"
WEB_URL="$(env_get STARTUPOS_WEB_URL)"
ACME_EMAIL="$(env_get STARTUPOS_ACME_EMAIL)"
COOKIE_SECURE="$(env_get STARTUPOS_COOKIE_SECURE)"

fail() { echo "✗ $*" >&2; exit 1; }

if [ -n "$DOMAIN" ]; then
  # These four must agree or sign-in breaks in ways that look like a Google problem. Check before we ship.
  [ -n "$ACME_EMAIL" ] || fail "STARTUPOS_DOMAIN is set but STARTUPOS_ACME_EMAIL is not. Add to .env:
    STARTUPOS_ACME_EMAIL=you@yourdomain.com          # Let's Encrypt expiry warnings go here"
  [ "$PREFIX" = "/api" ] || fail "STARTUPOS_DOMAIN is set but STARTUPOS_PATH_PREFIX is '$PREFIX'. Add to .env:
    STARTUPOS_PATH_PREFIX=/api"
  [ "$PUBLIC_URL" = "https://$DOMAIN/api" ] || fail "STARTUPOS_PUBLIC_URL is '$PUBLIC_URL'. Add to .env:
    STARTUPOS_PUBLIC_URL=https://$DOMAIN/api"
  [ "$WEB_URL" = "https://$DOMAIN" ] || fail "STARTUPOS_WEB_URL is '$WEB_URL'. Add to .env:
    STARTUPOS_WEB_URL=https://$DOMAIN"
  [ "$COOKIE_SECURE" != "0" ] || fail "STARTUPOS_COOKIE_SECURE=0 with a public domain would send the session
    cookie over plain HTTP. Set STARTUPOS_COOKIE_SECURE=1 (or remove the line — it defaults to 1)."
  PROFILE=(--profile public)
  SERVICES="db, migrate, ingest, daemon, api, web, caddy"
  echo "→ public deploy for https://$DOMAIN"
else
  PROFILE=()
  SERVICES="db, migrate, ingest, daemon, api"
  echo "→ no STARTUPOS_DOMAIN in .env: private deploy, nothing published (see docs/HOSTING.md to go public)"
fi

echo "→ syncing $HERE to $VM:$REMOTE_DIR"
rsync -az --delete \
  --exclude '.git' --exclude 'web/node_modules' --exclude 'web/.next' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude 'pgdata' \
  "$HERE/" "$VM:$REMOTE_DIR/"

echo "→ ensuring docker on the VM"
ssh "$VM" 'command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && echo "docker installed — log out/in once for group membership")'

if [ -n "$DOMAIN" ]; then
  echo "→ validating Caddyfile"
  ssh "$VM" "cd $REMOTE_DIR && sudo docker run --rm -e SITE_ADDRESS='$DOMAIN' -e STARTUPOS_ACME_EMAIL='$ACME_EMAIL' \
    -v \"\$PWD/Caddyfile\":/etc/caddy/Caddyfile:ro caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile" \
    || fail "the Caddyfile is not valid — fix it before deploying (nothing on the VM was changed)"
fi

echo "→ building and starting services ($SERVICES)"
ssh "$VM" "cd $REMOTE_DIR && sudo docker compose up -d --build db migrate"
if [ -n "$DOMAIN" ]; then
  ssh "$VM" "cd $REMOTE_DIR && sudo docker compose --profile public up -d --build ingest daemon api web caddy"
else
  ssh "$VM" "cd $REMOTE_DIR && sudo docker compose up -d --build ingest daemon api"
fi

echo "→ status"
ssh "$VM" "cd $REMOTE_DIR && sudo docker compose ${PROFILE[*]:-} ps"

if [ -n "$DOMAIN" ]; then
  cat <<EOS

Public URL:  https://$DOMAIN
API health:  https://$DOMAIN/api/health

The first request to a new hostname takes ~10-30s while Caddy gets the certificate from Let's Encrypt. If it
does not appear, ports 80 AND 443 must be open in the Azure NSG and DNS must already point at this VM
(docs/HOSTING.md has the exact az commands). Watch it happen:
  ssh $VM "cd $REMOTE_DIR && sudo docker compose logs -f caddy"

Then verify from your Mac:
  ./scripts/smoke_public.sh https://$DOMAIN
EOS
else
  cat <<EOS

Nothing is published. The API answers on the VM's loopback only:
  ssh $VM "curl -s http://localhost:8000/health"
EOS
fi

cat <<EOF

Useful commands on the VM (ssh $VM):
  cd $REMOTE_DIR && sudo docker compose logs -f daemon      # agent daemon
  cd $REMOTE_DIR && sudo docker compose logs -f ingest      # cron workers
  cd $REMOTE_DIR && sudo docker compose exec api python -m signals.digest
Update later: re-run this script.
Stop: ssh $VM "cd $REMOTE_DIR && sudo docker compose --profile public down".
NEVER run 'down -v' in production: it deletes the database AND Caddy's certificates (Let's Encrypt allows only
5 certificates per hostname per week).
EOF
