# Public hosting (operator runbook)

How to put StartupOS on one HTTPS URL you can send to another person. Follow it top to bottom; every command is
meant to be copied as written. You need a terminal, the repo checked out, and the Azure CLI signed in.

**What you end up with**

```
https://<your-domain>/            the app  (Next.js)
https://<your-domain>/api/...     the API  (FastAPI)
```

One hostname, one certificate, one cookie. That is deliberate: two hostnames would force cross-site cookies and
CORS, which browsers increasingly refuse. Caddy sits in front, gets a free Let's Encrypt certificate, and hands
`/api/...` to the API **without stripping the `/api`** — the API mounts itself under it (`STARTUPOS_PATH_PREFIX`).

**Time:** about 20 minutes, most of it waiting for DNS and the first certificate.

---

## 0. What you need before you start

| | |
| --- | --- |
| The VM | `azureuser@20.106.244.178`, repo at `~/startupos`. You can `ssh azureuser@20.106.244.178`. |
| Azure CLI | `az login` works. Check with `az account show`. |
| A hostname | Step 1 gives you a free one. Skip to step 1b if you own a domain. |
| `.env` | A filled-in `startupos/.env` (copy from `.env.example`). |

Find the VM's resource group and public IP name once; the later commands use them:

```bash
az vm list -d --query "[?publicIps=='20.106.244.178'].{name:name, rg:resourceGroup, ip:publicIps}" -o table
```

Note the `name` and `rg`. Everywhere below, replace `<RG>` with that resource group and `<VM>` with that VM name.

---

## 1a. Give the VM a DNS name (free, no domain purchase)

Azure will give any public IP a name like `startupos-unitone.eastus.cloudapp.azure.com`. The label must be
globally unique within the region and may contain letters, digits and hyphens only.

Find the public IP resource attached to the VM:

```bash
az network public-ip list -g <RG> --query "[?ipAddress=='20.106.244.178'].{name:name, fqdn:dnsSettings.fqdn}" -o table
```

Take the `name` (call it `<IPNAME>`) and set the label:

```bash
az network public-ip update -g <RG> -n <IPNAME> --dns-name startupos-unitone
```

Read back the hostname it created — this is your domain:

```bash
az network public-ip show -g <RG> -n <IPNAME> --query dnsSettings.fqdn -o tsv
# startupos-unitone.eastus.cloudapp.azure.com
```

The IP must be **Static**, or the name will point at the wrong machine after a reboot:

```bash
az network public-ip update -g <RG> -n <IPNAME> --allocation-method Static
```

Check it resolves (may take a minute):

```bash
dig +short startupos-unitone.eastus.cloudapp.azure.com     # must print 20.106.244.178
```

## 1b. Or point your own domain at the VM (`os.unitone.ai`)

Nothing in StartupOS cares which you use — a domain you own just looks better on a link you send to an investor.

At whoever hosts DNS for `unitone.ai` (Cloudflare, Namecheap, Route 53, Vercel…), add **one A record**:

| Type | Name | Value | TTL | Proxy |
| --- | --- | --- | --- | --- |
| A | `os` | `20.106.244.178` | 300 (5 min) | **off** |

Two things people get wrong here:

- **Name is `os`, not `os.unitone.ai`.** Most DNS UIs append the domain for you. If yours wants the full name,
  use the full name — check the preview it shows you.
- **Cloudflare users: set the proxy toggle to "DNS only" (grey cloud), at least for the first deploy.** With
  the orange cloud on, Cloudflare terminates TLS itself and Caddy's certificate request fails.

Wait until this prints the VM's IP before deploying:

```bash
dig +short os.unitone.ai      # must print 20.106.244.178
```

DNS must be correct **before** the first deploy. Let's Encrypt verifies by connecting to the hostname; if it
points somewhere else, the certificate fails and you burn one of five weekly attempts (see step 6).

---

## 2. Open ports 80 and 443 in the network security group

The VM's firewall (its NSG) blocks these today. Let's Encrypt needs **80**, browsers need **443**.

Find the NSG:

```bash
az network nsg list -g <RG> --query "[].name" -o tsv
```

Then, with `<NSG>` as that name:

```bash
az network nsg rule create -g <RG> --nsg-name <NSG> --name AllowHTTP \
  --priority 1000 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes '*' --source-port-ranges '*' \
  --destination-address-prefixes '*' --destination-port-ranges 80

az network nsg rule create -g <RG> --nsg-name <NSG> --name AllowHTTPS \
  --priority 1010 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes '*' --source-port-ranges '*' \
  --destination-address-prefixes '*' --destination-port-ranges 443
```

Confirm — you should see 22, 80 and 443 and nothing else public:

```bash
az network nsg rule list -g <RG> --nsg-name <NSG> -o table
```

**Portal instead of the CLI:** portal.azure.com → search your VM → **Networking** → **Network settings** →
**Add inbound port rule**. Source `Any`, Source port ranges `*`, Destination `Any`, Service `HTTP`, Action
`Allow`, Priority `1000`, Name `AllowHTTP`. Repeat with Service `HTTPS`, Priority `1010`, Name `AllowHTTPS`.

**Port 8000 stays closed.** It used to be the only way in; now the proxy is. The API container binds 8000 to the
VM's loopback only, so it is reachable from an `ssh` session and from nowhere else. If you previously added an
`AllowPort8000` rule, delete it:

```bash
az network nsg rule delete -g <RG> --nsg-name <NSG> --name AllowPort8000
```

---

## 3. Fill in `.env`

In `startupos/.env`, set these six together. `<domain>` is what step 1 gave you, with no `https://` and no
trailing slash.

```bash
STARTUPOS_DOMAIN=startupos-unitone.eastus.cloudapp.azure.com
STARTUPOS_ACME_EMAIL=you@unitone.ai
STARTUPOS_PATH_PREFIX=/api
STARTUPOS_PUBLIC_URL=https://startupos-unitone.eastus.cloudapp.azure.com/api
STARTUPOS_WEB_URL=https://startupos-unitone.eastus.cloudapp.azure.com
STARTUPOS_COOKIE_SECURE=1
```

Why each one:

| Variable | What it does | Getting it wrong looks like |
| --- | --- | --- |
| `STARTUPOS_DOMAIN` | Turns on the public profile and is the hostname Caddy gets a certificate for. | Empty → nothing is published at all. |
| `STARTUPOS_ACME_EMAIL` | Where Let's Encrypt sends expiry warnings. | Missing → the deploy refuses to run. |
| `STARTUPOS_PATH_PREFIX` | Mounts every API route **and every cookie** under `/api`. | Wrong → `/api/health` is a 404, or sign-in loops forever. |
| `STARTUPOS_PUBLIC_URL` | The full public API base. Google's and Slack's redirect URLs are built from it. | Missing the `/api` → Google says `redirect_uri_mismatch`. |
| `STARTUPOS_WEB_URL` | Where the browser is sent after sign-in. | Wrong → you sign in and land on localhost. |
| `STARTUPOS_COOKIE_SECURE` | Session cookie is HTTPS-only. | `0` in production → the session cookie can leak over plain HTTP. |

`scripts/deploy_azure.sh` checks all six before it touches the VM and tells you the exact line to add if one is
off, so a typo costs you a few seconds, not a broken deploy.

Google sign-in has its own two variables and a Google Cloud setup — that is `docs/GOOGLE-SIGNIN.md`. The
authorised redirect URI it asks for is `https://<domain>/api/auth/google/callback`.

### What a shared URL costs you, and how to cap it

A public URL plus Google sign-in means **anybody who has the link can create a company**, and the daemon then
spends Tier-2 tokens on that company's behalf. Rate limiting caps how fast strangers arrive (5 sign-ups per
source per minute); these two decide what each one can spend, and whether they arrive at all.

| Variable | Default | What it does |
| --- | --- | --- |
| `STARTUPOS_SIGNUP_TIER2_TOKENS` | `100000` | Monthly Tier-2 token allowance for a company created by self-serve sign-up. It is the allowance of the `self_serve` tier, so it applies every month with nothing to remember. An operator-created company (bootstrap token) is unchanged on the founder tier, 1.5M. |
| `STARTUPOS_ALLOW_SIGNUP` | `1` (open) | `0` closes self-serve sign-up: the Google callback says so on a page instead of offering a company, `POST /onboarding/tenant` refuses with the same wording, and the sign-in page says new companies are closed. Signing in still works for everyone who already has an account, and you can still create companies yourself with `STARTUPOS_BOOTSTRAP_TOKEN`. |

To promote a company you actually want to serve, change its tier — that is the only thing an allowance reads:

```bash
ssh azureuser@<vm> "cd ~/startupos && sudo docker compose exec -T db \
  psql -U postgres -d startupos -c \"UPDATE tenants SET tier = 'founder' WHERE id = '<slug>';\""
```

A company can raise its own monthly budget in the cadence form, but only up to its tier's allowance; asking for
more is a 422 that tells the founder to ask you.

### The API documentation is not served here

`/docs`, `/redoc` and `/openapi.json` are off automatically on a public deployment (anything with
`STARTUPOS_DOMAIN` or `STARTUPOS_PATH_PREFIX` set) and on locally. They expose no tenant data, but they hand
anyone with the URL the complete route and schema map of the product, and `scripts/smoke_public.sh` fails the
deploy check if they answer. `STARTUPOS_ENABLE_DOCS=1` forces them back on while you debug a deploy — take it
out again afterwards.

---

## 4. Deploy

From the repo on your laptop:

```bash
cd startupos
./scripts/deploy_azure.sh
```

It syncs the repo, builds the images on the VM and starts **db, migrate, ingest, daemon, api, web, caddy**. The
first run takes several minutes (it builds the Next.js app). It prints your public URL at the end.

Watch the certificate being issued — this is the step that fails if DNS or the NSG is wrong:

```bash
ssh azureuser@20.106.244.178 "cd ~/startupos && sudo docker compose logs -f caddy"
```

You want a line containing `certificate obtained successfully`. `Ctrl-C` to stop following.

---

## 5. Verify

From your laptop, not the VM:

```bash
./scripts/smoke_public.sh https://<domain>
```

It checks the certificate is real (not self-signed), that `/` serves the app and `/api/health` the API, that
`/api/slack/events` rejects an unsigned request, that a private API route is 401 without a session, and that the
security headers are present. Everything green means the URL is ready to send to someone.

To look at the certificate yourself:

```bash
echo | openssl s_client -servername <domain> -connect <domain>:443 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

`issuer=C=US, O=Let's Encrypt, CN=E5` (or similar) is what you want. `issuer=...Caddy Local Authority...` means
ACME failed and Caddy fell back to a self-signed certificate — go back to steps 1 and 2.

---

## 6. Let's Encrypt rate limits — read this before retrying

Let's Encrypt allows **5 certificates per exact hostname per 7 days** (and 5 failed validations per hostname per
hour). If you keep redeploying while DNS is wrong, you can lock yourself out of that hostname for a week.

Two rules:

1. **Fix DNS and the NSG first, deploy second.** Confirm `dig +short <domain>` prints the VM's IP and that
   `curl -I http://<domain>` reaches the VM before running the deploy again.
2. **Never run `docker compose down -v` on the VM.** The `-v` deletes volumes — which means the database *and*
   `caddy_data`, where the certificate and the ACME account key live. Losing it re-issues on the next start and
   eats one of your five. Stop with:

   ```bash
   ssh azureuser@20.106.244.178 "cd ~/startupos && sudo docker compose --profile public down"
   ```

If you are experimenting and expect failures, test against the staging CA first: add
`acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` inside the site block in `Caddyfile`, deploy,
confirm the flow works end to end, then remove the line and deploy once more. Staging certificates are untrusted
by browsers (expect a warning) but have far higher limits.

---

## 7. Moving to a different domain later

Say you start on `startupos-unitone.eastus.cloudapp.azure.com` and later move to `os.unitone.ai`. In order:

1. Point the new name at the VM (step 1b) and wait for `dig +short os.unitone.ai` to show the IP.
2. Update three values in `.env`: `STARTUPOS_DOMAIN`, `STARTUPOS_PUBLIC_URL` (keep the `/api`) and
   `STARTUPOS_WEB_URL`.
3. **Update Google before you deploy.** In Google Cloud → APIs & Services → Credentials → your OAuth client, add
   `https://os.unitone.ai/api/auth/google/callback` to Authorised redirect URIs and `https://os.unitone.ai` to
   Authorised JavaScript origins. Leave the old ones in place until you are sure, then remove them.
4. Update Slack the same way if Slack is connected: the OAuth redirect URL, the Events request URL and the
   Interactivity request URL all start from `STARTUPOS_PUBLIC_URL` (see `docs/SLACK-SETUP.md`).
5. Re-run `./scripts/deploy_azure.sh`. Caddy gets a certificate for the new name; the old one stays in
   `caddy_data` and simply goes unused.
6. Re-run `./scripts/smoke_public.sh https://os.unitone.ai`.

Anyone signed in on the old hostname is signed out — session cookies belong to a hostname. They sign in again.

Keeping both names working at once is possible (Caddy accepts a comma-separated site address), but only one can
be `STARTUPOS_WEB_URL`, so sign-in would always land people on that one. Not worth it; move, then retire.

---

## Running without a domain

With `STARTUPOS_DOMAIN` empty, `./scripts/deploy_azure.sh` deploys exactly what it always did — db, migrate,
ingest, daemon, api — and publishes nothing. The API answers on the VM's loopback only:

```bash
ssh azureuser@20.106.244.178 "curl -s http://localhost:8000/health"
```

The same is true locally: `docker compose up -d` in `startupos/` brings up the old five services on plain HTTP
with no prefix, and `make check` never needs any of this. The web app and Caddy live behind a compose profile,
so they start only when asked:

```bash
docker compose --profile public up -d --build      # local edge on http://localhost (no TLS, no ACME)
```

---

## When something is wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| Browser: "Your connection is not private" | ACME failed; Caddy served its own self-signed certificate. | `docker compose logs caddy`. Almost always DNS pointing elsewhere or port 80 closed. Steps 1 and 2. |
| `curl https://<domain>` times out | 443 closed in the NSG. | Step 2, then `az network nsg rule list` to confirm. |
| `/` loads, `/api/health` is 404 | `STARTUPOS_PATH_PREFIX` is not `/api`. | Step 3, then redeploy. |
| Google: "Error 400: redirect_uri_mismatch" | The URI in Google is not `https://<domain>/api/auth/google/callback`. | `docs/GOOGLE-SIGNIN.md`. The `/api` is easy to miss. |
| Sign-in loops back to `/login` | The session cookie is not coming back. Check the API logs for the `STARTUPOS_PUBLIC_URL does not end with the prefix` warning. | Step 3 — `PUBLIC_URL` and `PATH_PREFIX` must agree. |
| You can sign in, nobody else can | The Google OAuth client is still in Testing mode. | `docs/GOOGLE-SIGNIN.md` — publish the consent screen. |
| Certificate stopped renewing | `caddy_data` was deleted, or 80 was closed after the first issue. | Keep the volume, keep 80 open. Caddy renews at 2/3 of the lifetime, on its own. |

Logs, all on the VM (`ssh azureuser@20.106.244.178 "cd ~/startupos && ..."`):

```bash
sudo docker compose logs -f caddy      # certificates and routing
sudo docker compose logs -f web        # Next.js
sudo docker compose logs -f api        # FastAPI, including the startup config warnings
sudo docker compose --profile public ps
```
