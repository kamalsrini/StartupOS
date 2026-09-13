# Slack app setup (operator runbook)

How to create and configure the one Slack app that serves a StartupOS install, end to end, without reading code.

**One app, many workspaces.** StartupOS does not use a per-company bot token in `.env`. The operator creates a
single Slack app and sets three credentials on the server; every company then installs that app into its own
workspace from Settings → Connect Slack, and its bot token is stored encrypted per tenant in the database. A
workspace belongs to exactly one company (`slack_installations.team_id` is UNIQUE).

Everything below is either a Slack UI step or a value the code actually requires — the scopes, routes and
variable names come from `startupos/auth/slack_install.py`, `startupos/api/routers/slack.py` and
`startupos/auth/config.py`.

---

## 0. Requirements before you start

**Slack must be able to reach the StartupOS API over HTTPS, on a public hostname, with a valid certificate.**
Slack sends a `challenge` POST to your Event Subscriptions request URL and refuses to save the URL unless it gets
a 200 back within 3 seconds; it does the same for Interactivity, and the OAuth redirect URL must be reachable in
the founder's browser.

As things stand today this is **not yet true for the Azure VM** (`azureuser@20.106.244.178`, `scripts/deploy_azure.sh`):

- the API container publishes plain HTTP on port **8000** (`docker-compose.yml`: `ports: ["8000:8000"]`, uvicorn,
  no TLS), and
- port 8000 is **closed in the VM's network security group** — the deploy script says so itself ("open port 8000
  in the NSG to reach it remotely").

So Slack cannot verify the request URL, the OAuth callback cannot complete, and `/slack/events` and
`/slack/interactivity` will never be called until that is fixed. There is no way around it from inside the app:
Slack only calls public HTTPS endpoints with a certificate it trusts, and it will not call an IP address or a
plain-HTTP URL. Pick one of:

| Option | What it takes | Good for |
| --- | --- | --- |
| **Reverse proxy + certificate (recommended)** | Point a DNS name (e.g. `api.startupos.example`) at the VM, open **443** in the NSG, run Caddy or nginx in front of the API container, terminate TLS with a Let's Encrypt certificate, proxy to `api:8000`. Keep 8000 itself closed to the internet. | Production, and the only option for real founders |
| **Tunnel** | `ngrok http 8000` / `cloudflared tunnel` on the VM (or a laptop running the API). Gives an HTTPS hostname immediately, no NSG or certificate work. | Testing and the first end-to-end walk. The hostname changes every restart on free plans, and every change means editing three URLs in the Slack app |

Whichever you choose, that public origin is `STARTUPOS_PUBLIC_URL`, and **every Slack URL below is derived from
it**. Also have ready:

- `STARTUPOS_WEB_URL` — where the browser is sent after the install (the web app's origin).
- `STARTUPOS_MASTER_KEY` — set, or the install cannot store the bot token and the callback returns
  `?slack=error&reason=secrets`.
- A Slack workspace where you are allowed to create and install apps.

---

## 1. Create the app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it (e.g. `StartupOS`), pick your own workspace as the development workspace, **Create App**.
3. **Basic Information → Display Information**: set the name, a short description ("Your company's chief of
   staff: the morning pulse, the evening digest and approvals, in Slack") and an app icon. This is what founders
   see on the install screen.

## 2. The three credentials and the `.env` variables

**Basic Information → App Credentials** has all three:

| Slack field | Environment variable | Notes |
| --- | --- | --- |
| Client ID | `STARTUPOS_SLACK_CLIENT_ID` | Looks like `1111111111.2222222222` |
| Client Secret | `STARTUPOS_SLACK_CLIENT_SECRET` | Shown once per regeneration; treat as a password |
| Signing Secret | `STARTUPOS_SLACK_SIGNING_SECRET` | Verifies every inbound request; not the same as the client secret |

Put them in `startupos/.env` (see `.env.example`), then restart the API and the daemon:

```
STARTUPOS_SLACK_CLIENT_ID=1111111111.2222222222
STARTUPOS_SLACK_CLIENT_SECRET=…
STARTUPOS_SLACK_SIGNING_SECRET=…
STARTUPOS_PUBLIC_URL=https://api.startupos.example
STARTUPOS_WEB_URL=https://app.startupos.example
```

If any of the three is missing, the API logs one warning at start and `/slack/install`, `/slack/oauth/callback`,
`/slack/events` and `/slack/interactivity` answer **503**. Nothing else in StartupOS degrades, and
`/slack/status` still answers (with `configured: false`) so the Settings page can explain the situation.

Never put a workspace bot token (`xoxb-…`) in `.env` for a customer: per-tenant tokens are created by the install
and stored encrypted in `tenant_secrets`. (The legacy `SLACK_BOT_TOKEN` in `.env.example` is for the operator's
own dev workspace only.)

## 3. Redirect URL (OAuth)

**OAuth & Permissions → Redirect URLs → Add New Redirect URL**:

```
<STARTUPOS_PUBLIC_URL>/slack/oauth/callback
```

e.g. `https://api.startupos.example/slack/oauth/callback`. **Save URLs.** It must match byte for byte — the API
sends this exact string as `redirect_uri` in both the authorize link and the token exchange, and Slack rejects
the exchange (`bad_redirect_uri`) if it differs by so much as a trailing slash.

## 4. Bot token scopes

**OAuth & Permissions → Scopes → Bot Token Scopes**. Add exactly these seven — StartupOS requests this list and
nothing else (`auth/slack_install.py::SCOPES`):

| Scope | Why |
| --- | --- |
| `chat:write` | Post the pulse, the digest, signals and approvals |
| `chat:write.public` | Post in a **public** channel the bot was not invited to |
| `commands` | Slash-command entry point |
| `im:history` | Read the DMs a founder sends the bot |
| `app_mentions:read` | Read messages that @-mention the bot |
| `users:read` | Map a Slack user to a StartupOS user (who pressed Approve) |
| `channels:read` | Resolve public channel names to ids |

Do not add user token scopes; StartupOS never asks for a user token. Adding or removing a scope later means every
already-installed workspace must reinstall before the new scope works.

## 5. Event Subscriptions

**Event Subscriptions → Enable Events: On.**

- **Request URL:** `<STARTUPOS_PUBLIC_URL>/slack/events`
  Slack immediately POSTs a `url_verification` challenge; the route echoes it and the field turns
  **Verified**. If it does not, stop and fix section 0 — nothing downstream works until this is green.
- **Subscribe to bot events** — add exactly these four:

| Event | What StartupOS does |
| --- | --- |
| `app_mention` | Answers a mention through the chief-of-staff gateway |
| `message.im` | Answers a DM to the bot |
| `app_uninstalled` | Revokes the install: deletes the bot token, disables the connection |
| `tokens_revoked` | Same as above |

**Save Changes**, and reinstall the app in any workspace that already had it (Slack asks for this whenever events
or scopes change).

Inbound events are acknowledged inside Slack's 3-second budget: the route queues the work and the daemon posts
the reply, so the daemon must be running for mentions and DMs to be answered.

## 6. Interactivity

**Interactivity & Shortcuts → Interactivity: On.**

- **Request URL:** `<STARTUPOS_PUBLIC_URL>/slack/interactivity`

This is what the Approve / Decline buttons call. Without it the buttons render but do nothing.

## 7. App Home

**App Home → Show Tabs:**

- **Messages Tab: On**, and tick **"Allow users to send Slash commands and messages from the messages tab"**.

Without this the founder cannot type to the bot at all, and `message.im` never fires.

## 8. Distribution (so other companies can install)

**Manage Distribution → Share Your App with Other Workspaces:**

1. Work through the checklist Slack shows (remove hard-coded information, redirect URL configured — all four
   items must be green).
2. **Activate Public Distribution.**

Until this is on, only your own development workspace can install the app; a founder from another company gets
an error on the Slack consent screen. The "Add to Slack" button Slack offers is not used — StartupOS builds the
authorize URL itself, with a signed `state` that ties the install to the right company.

## 9. The founder side

1. The founder signs in to StartupOS and opens **Settings**.
2. **Connect Slack** → Slack's consent screen lists the seven scopes → **Allow**.
3. Slack returns them to Settings with a green "Slack connected" banner. The bot token is now stored, encrypted,
   for that company alone.
4. **Where should StartupOS post?** — on the same page. A channel name (`#ops`) or a channel id (`C0123ABCD`);
   leave it empty to use the channel the install recorded. A bad value is refused with a message, not saved.
5. For a **private** channel, invite the bot first: `/invite @StartupOS` in that channel. `chat:write.public`
   covers public channels only.
6. Whether anything is posted at all is the cadence: onboarding → *Set the cadence* → `slack` or `both`. `web`
   means StartupOS never posts, whatever the channel says.

To disconnect: Slack → Apps → StartupOS → Configuration → **Remove App**. Slack sends `app_uninstalled`, and
StartupOS drops the token, marks the install revoked and disables the connection.

> **Note on the install-time channel.** Slack only returns the channel a founder picks during install when the
> app requests the `incoming-webhook` scope, which StartupOS deliberately does not (it posts with `chat.write`,
> not a webhook). So `slack_installations.default_channel` falls back to `#general`, and the channel that
> actually matters is the one the founder sets in Settings (step 4). Set it during the first install rather than
> discovering `#general` later.

---

## 10. Verification checklist

Work down the list; each line depends on the one above it.

- [ ] **Challenge verified.** Event Subscriptions shows **Verified** next to
      `<STARTUPOS_PUBLIC_URL>/slack/events`. (`curl -s <STARTUPOS_PUBLIC_URL>/health` from outside the VM should
      also answer 200 over HTTPS.)
- [ ] **Interactivity saved** with `<STARTUPOS_PUBLIC_URL>/slack/interactivity`.
- [ ] **Install completes.** Settings → Connect Slack → Allow lands back on Settings with the green banner and
      "Connected to <workspace>". A row exists in `slack_installations`, an encrypted `slack_bot_token` in
      `tenant_secrets`, and a `connected` `slack` row in `connections` — and no token anywhere else.
- [ ] **A test post lands.** Set the channel in Settings, set the cadence to `slack` or `both`, then wait for the
      next thing the daemon delivers: the morning pulse at the company's `pulse_hour`, the evening digest, or a
      high-severity signal on the next 15-minute tick. The message appears in the channel, and a `deliveries` row
      for that company is written with `status='sent'` and a message `ts`. A row with `status='failed'` carries
      Slack's own error in `error` — take it to the table below.
- [ ] **A DM is answered.** Send the bot a direct message; a reply arrives within a few seconds (the daemon must
      be running).
- [ ] **A button press executes.** When an approval is proposed it arrives with Approve / Decline. Press one: the
      message updates in place to the decided state and the buttons disappear; the `approvals` row shows the
      decision and, for Approve, the executor's result. Pressing again changes nothing.
- [ ] **Uninstall is clean.** Remove the app in Slack; `slack_installations.revoked_at` is set, the secret is
      gone, the connection is `disabled`, and nothing is posted afterwards.

## 11. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Slack says **"Your URL didn't respond with the value of the challenge parameter"** | The API is not reachable over public HTTPS (see section 0), or the three `STARTUPOS_SLACK_*` variables are unset so the route answers 503 | Put the API behind TLS on a public hostname (or a tunnel), set the variables, restart the API, then click **Retry** in Slack |
| Post fails with **`not_in_channel`** | The bot is not a member of that channel. `chat:write.public` lets it post in **public** channels it was never invited to, but a **private** channel always needs an invite | In that channel: `/invite @StartupOS`. Or point Settings at a public channel. The failure is recorded on the `deliveries` row, so the pulse itself does not break |
| Post fails with **`channel_not_found`** | The channel name is misspelled, was renamed or archived, or is a private channel the bot cannot see | Fix the channel in Settings (a name must start with `#`; an id looks like `C0123ABCD`), or invite the bot |
| Everything fails with **`invalid_auth`** / **`account_inactive`** after someone removed the app | The bot token was revoked in Slack. StartupOS normally learns this from `app_uninstalled`/`tokens_revoked` — unless those events are not subscribed, in which case it keeps trying with a dead token | Subscribe the two uninstall events (section 5), then have the founder run **Connect Slack** again. Reinstalling issues a fresh token and clears `revoked_at` |
| Inbound requests get **403 "invalid Slack signature"** | `STARTUPOS_SLACK_SIGNING_SECRET` does not match the app (wrong app, secret regenerated, or the client secret pasted by mistake), or a proxy rewrites the request body | Copy the Signing Secret from Basic Information again, restart the API, retry. Make sure the reverse proxy passes the body through unmodified |
| 403s that come and go, or all requests fail after a VM reboot | Clock skew: the signature covers a timestamp and anything more than **300 seconds** from the server's clock (in either direction) is rejected as a replay | Fix time sync on the host (`timedatectl set-ntp true`; `chronyc tracking` to check) |
| Inbound requests get **503 "Slack is not configured on this install"** | The signing secret is missing entirely | Set `STARTUPOS_SLACK_SIGNING_SECRET` and restart |
| Settings shows **"That Slack workspace is already connected to another StartupOS company"** (`?slack=error&reason=workspace_taken`) | One workspace maps to exactly one company, and this workspace already belongs to another one. Nothing was written | Use a different workspace, or have the first company remove the app in Slack (which revokes its install) before the second one installs |
| `?slack=error&reason=state` | The install link is good for 10 minutes and was reused or expired | Click **Connect Slack** again |
| `?slack=error&reason=exchange` | Slack refused the code: wrong Client ID/Secret, or a `redirect_uri` that does not match the one registered | Re-check sections 2 and 3 (exact string, no trailing slash) |
| `?slack=error&reason=secrets` | `STARTUPOS_MASTER_KEY` is not set, so the bot token cannot be stored | Set it and restart; then reinstall |
| Buttons render but nothing happens when pressed | Interactivity is off or points at the wrong URL | Section 6 |
| The bot never answers a DM | Messages Tab off, or "Allow users to send…" unticked, or `message.im` not subscribed, or the daemon is not running | Sections 5 and 7; `docker compose logs -f daemon` |
| Slack retries the same message and the bot answers once | Working as intended — inbound events are deduplicated on Slack's `event_id` | Nothing |
