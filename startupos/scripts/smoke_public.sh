#!/usr/bin/env bash
# Verify a deployed StartupOS from the outside.  ./scripts/smoke_public.sh https://os.example.com
#
# Run this from your laptop (not from the VM) after ./scripts/deploy_azure.sh — it checks the things that are
# invisible from inside the box: a real certificate, the app and the API on one hostname, and that nothing
# private answers without authentication.
set -uo pipefail

BASE="${1:-${STARTUPOS_PUBLIC_URL:-}}"
if [ -z "$BASE" ]; then
  echo "usage: $0 https://<your-domain>" >&2; exit 2
fi
BASE="${BASE%/}"
HOST="${BASE#https://}"; HOST="${HOST#http://}"; HOST="${HOST%%/*}"; HOST="${HOST%%:*}"

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }

echo "smoke: $BASE"

# 1. TLS: the certificate must be valid AND issued by a real CA. curl without -k fails on self-signed,
#    on a name mismatch and on an expired certificate, so one check covers all three.
if [ "${BASE#https://}" != "$BASE" ]; then
  if curl -sS --max-time 20 -o /dev/null "$BASE/" 2>/tmp/smoke_tls.$$; then
    ok "TLS certificate is valid and trusted (not self-signed)"
  else
    bad "TLS: $(tr -d '\n' </tmp/smoke_tls.$$)"
    echo "       Caddy serves its own self-signed cert when ACME fails. Check: DNS points at the VM, ports 80"
    echo "       and 443 are open in the NSG, and 'docker compose logs caddy' on the VM."
  fi
  rm -f /tmp/smoke_tls.$$
  issuer="$(echo | openssl s_client -servername "$HOST" -connect "$HOST:443" 2>/dev/null \
            | openssl x509 -noout -issuer 2>/dev/null || true)"
  case "$issuer" in
    *"Let's Encrypt"*|*"E5"*|*"E6"*|*"R10"*|*"R11"*|*ISRG*) ok "issuer: ${issuer#issuer=}" ;;
    *Caddy*|*"local"*)                                      bad "issuer looks self-signed: ${issuer#issuer=}" ;;
    "")                                                     bad "could not read the certificate issuer" ;;
    *)                                                      ok "issuer: ${issuer#issuer=}" ;;
  esac
else
  bad "$BASE is not https — a public StartupOS must be served over TLS"
fi

# 2. The web app answers at /. (A redirect to /cockpit or /login is a pass; a 5xx is not.)
c="$(code "$BASE/")"
case "$c" in 2??|3??) ok "GET / -> $c (web app)";; *) bad "GET / -> $c";; esac

# 3. The API answers at /api/health with ok:true — proves the prefix survived the proxy.
body="$(curl -s --max-time 20 "$BASE/api/health" || true)"
case "$body" in *'"ok":true'*) ok "GET /api/health -> ok";; *) bad "GET /api/health -> ${body:-<no body>}";; esac

# 4. Slack inbound is signature-gated: an unsigned POST must NOT be accepted.
#    401/403 = rejected (right). 503 = no Slack app configured yet, also not an open door.
c="$(code -X POST -H 'Content-Type: application/json' -d '{"type":"url_verification","challenge":"x"}' "$BASE/api/slack/events")"
case "$c" in
  401|403) ok "POST /api/slack/events (unsigned) -> $c (signature-gated)";;
  503)     ok "POST /api/slack/events -> 503 (Slack app not configured; still closed)";;
  *)       bad "POST /api/slack/events (unsigned) -> $c — expected 401/403/503";;
esac

# 5. A private API route must be 401 without a session.
c="$(code "$BASE/api/cockpit")"
case "$c" in 401) ok "GET /api/cockpit (no session) -> 401";; *) bad "GET /api/cockpit (no session) -> $c — expected 401";; esac

# 6. Sign-in starts: /api/auth/google redirects to Google (302) or 503 when GOOGLE_CLIENT_ID is unset.
loc="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 20 "$BASE/api/auth/google" || true)"
case "$loc" in
  302*accounts.google.com*) ok "GET /api/auth/google -> redirects to Google";;
  503*)                     bad "GET /api/auth/google -> 503: GOOGLE_CLIENT_ID is not set (see docs/GOOGLE-SIGNIN.md)";;
  *)                        bad "GET /api/auth/google -> $loc";;
esac

# 7. The interactive API documentation must NOT be served here (Sprint 3d PE review): it is the product's whole
#    route and schema map, and a public deployment has STARTUPOS_DOMAIN/STARTUPOS_PATH_PREFIX set, which turns
#    it off. 404 is the pass; a 200 means STARTUPOS_ENABLE_DOCS=1 was left in .env.
for d in docs redoc openapi.json; do
  c="$(code "$BASE/api/$d")"
  case "$c" in
    404) ok "GET /api/$d -> 404 (API docs not public)";;
    *)   bad "GET /api/$d -> $c — expected 404; unset STARTUPOS_ENABLE_DOCS in .env and redeploy";;
  esac
done

# 8. Security headers from the proxy.
hdrs="$(curl -sS -D - -o /dev/null --max-time 20 "$BASE/" 2>/dev/null | tr 'A-Z' 'a-z' || true)"
for h in strict-transport-security x-content-type-options referrer-policy; do
  case "$hdrs" in *"$h"*) ok "header: $h";; *) bad "header missing: $h";; esac
done

echo
if [ "$fail" -eq 0 ]; then
  echo "✓ $pass checks passed — $BASE is ready to share"
  exit 0
fi
echo "✗ $fail of $((pass+fail)) checks failed — see docs/HOSTING.md"
exit 1
