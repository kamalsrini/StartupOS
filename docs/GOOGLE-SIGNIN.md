# Google sign-in (operator runbook)

How to make `https://<your-domain>/` a link you can send to another person, so that they sign in with Google
and get a company brain of their own. Follow it top to bottom.

This assumes the app is already served over HTTPS on one hostname — that is **[docs/HOSTING.md](HOSTING.md)**,
and it has to be done first: Google will not send anyone back to an address that does not exist yet, and it
refuses `http://` redirect URIs for anything but `localhost`.

**Time:** about 15 minutes, plus however long you argue with yourself about the app name.

---

## What you are creating

| | |
| --- | --- |
| A Google Cloud **project** | A container. It does not cost anything and nothing runs in it. |
| An **OAuth consent screen** | What the person sees: "StartupOS wants to access your name and e-mail." |
| An **OAuth client** (type: Web application) | The `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` pair StartupOS uses. |

StartupOS asks Google for three scopes and nothing else: `openid`, `email`, `profile`. It never asks for Gmail,
Drive or Calendar, so this app **does not need Google's verification review** (see step 5 — this is the part
everybody gets wrong).

---

## 1. Create the project

1. Open <https://console.cloud.google.com/> and sign in with the Google account that should own this.
   Use a company account if you have one: whoever owns this project can turn sign-in off for everybody.
2. Project picker (top bar) → **New project** → name it `StartupOS` → **Create**.
3. Make sure the project picker now shows `StartupOS`. Everything below happens inside it.

---

## 2. Configure the consent screen

**APIs & Services → OAuth consent screen** (newer consoles: **Google Auth platform → Branding**).

1. **User type**:
   * **External** — anyone with a Google account can sign in. This is what you want if you are sending the
     link to people outside your own Google Workspace (founders, design partners, friends).
   * **Internal** — only accounts in your own Google Workspace domain. Only offered if you have Workspace.
     It is the safest choice when StartupOS is for one company, and it skips step 5 entirely.
2. **App name**: `StartupOS` (this is the name in "StartupOS wants to access your Google Account").
   **User support e-mail**: yours. **Developer contact e-mail**: yours. → **Save and continue**.
3. **Scopes**: add `.../auth/userinfo.email`, `.../auth/userinfo.profile` and `openid`. All three are
   "non-sensitive". **Do not add anything else.** → **Save and continue**.
4. **Test users**: add your own Google address, and for now anyone you plan to test with. → **Save**.

---

## 3. Create the OAuth client

**APIs & Services → Credentials → + Create credentials → OAuth client ID**.

* **Application type**: **Web application**.
* **Name**: `StartupOS web` (internal label only; nobody sees it).
* **Authorised JavaScript origins** — add exactly:

  ```
  https://<your-domain>
  ```

* **Authorised redirect URIs** — add exactly:

  ```
  https://<your-domain>/api/auth/google/callback
  ```

Then **Create**, and copy the **Client ID** and **Client secret** out of the dialog. The secret can be
re-read later from the client's page; the ID is public, the secret is not.

### Get these two strings exactly right

The redirect URI is compared by Google **character for character**. The most common mistakes:

| Wrong | Why |
| --- | --- |
| `https://<domain>/auth/google/callback` | Missing `/api`. The API is served under `/api` on the public URL (`STARTUPOS_PATH_PREFIX=/api`). |
| `https://<domain>/api/auth/google/callback/` | Trailing slash. Different URI. |
| `http://<domain>/api/...` | `http`, not `https`. Google rejects plain HTTP except for `localhost`. |
| `https://<domain>/api/auth/google/callback` as an **origin** | Origins are scheme + host only, no path. |

**Local development** (`make dev-api` + `npm run dev`, no proxy, no prefix) uses a *different* pair, which you
can add to the same client as extra entries:

```
origin:   http://localhost:3000
redirect: http://localhost:8000/auth/google/callback      (no /api — there is no prefix locally)
```

The rule, in one line: the redirect URI is always `$STARTUPOS_PUBLIC_URL/auth/google/callback`, and
`STARTUPOS_PUBLIC_URL` includes the `/api` in production and does not include it locally.

---

## 4. Put the two values in `.env`

In `startupos/.env` on the VM (copy the names exactly — they have no `STARTUPOS_` prefix):

```bash
GOOGLE_CLIENT_ID=1234567890-abcdefghijklmnop.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxx
```

They must agree with the four values the hosting runbook already set, because the redirect URI is built from
them and nothing re-checks it until somebody tries to sign in:

```bash
STARTUPOS_PUBLIC_URL=https://<your-domain>/api     # ← redirect URI = this + /auth/google/callback
STARTUPOS_WEB_URL=https://<your-domain>            # ← where the browser lands after sign-in
STARTUPOS_PATH_PREFIX=/api
STARTUPOS_COOKIE_SECURE=1
```

Then restart the API so it reads them:

```bash
cd ~/startupos && docker compose up -d --build api
```

Check it took:

```bash
curl -s https://<your-domain>/api/auth/providers      # {"google":true,"bootstrap":false}
```

`"google":false` means the API has no client ID — the `.env` was not read, or the container was not restarted.
Nothing else about this is worth debugging until that line says `true`.

> **Turn the bootstrap token off once Google works.** `STARTUPOS_BOOTSTRAP_TOKEN=` (empty) disables
> `POST /auth/bootstrap`, which is a password-shaped way into the install's own tenant. Any non-empty value
> enables it.

---

## 5. Publish the consent screen — the step that decides whether anyone else can get in

**This is almost always the answer to "I sent my friend the link and they could not get in."**

A freshly created **External** consent screen is in **Testing** mode, and in Testing mode Google admits
**only the accounts listed under "Test users"** — up to 100 of them. Everyone else gets, before StartupOS is
ever reached:

> **Access blocked: StartupOS has not completed the Google verification process**
> `Error 403: access_denied`

Nothing in StartupOS can fix that, and nothing in StartupOS ever sees the attempt. You have two choices:

1. **Add them as test users.** OAuth consent screen → **Audience** / **Test users** → **Add users** → their
   exact Gmail/Workspace address. Fine for a handful of design partners. Takes effect immediately.
2. **Publish the app.** OAuth consent screen → **Publish app** → confirm. Now any Google account can sign in.

**Publishing does not put you in a review queue.** Google's verification (the multi-week brand-and-scope
review) is required for *sensitive* and *restricted* scopes — Gmail, Drive, Calendar and friends. StartupOS
asks only for `openid email profile`, which are non-sensitive, so "In production" is effective at once and
users do not even see the "Google hasn't verified this app" interstitial. If the publish dialog starts talking
about verification, something has added a scope that does not belong — go back to step 2.3 and remove it.

**Internal** (Workspace) consent screens skip this entirely: everyone in your domain can sign in, nobody
outside can, and there is no Testing mode.

---

## 6. Verify, as a person

1. Open `https://<your-domain>/` in a **private window**. You should land on **/login** with one
   **Continue with Google** button.
2. Press it. Google asks which account, then asks you to allow name + e-mail.
3. You come back to `https://<your-domain>/cockpit`, signed in, with your e-mail at the bottom of the sidebar.
4. Press **Logout**. You are back at **/login** and the cockpit is not reachable again without signing in.
5. Now do the one that actually matters: send the URL to somebody else and watch what they get (next section).

---

## 7. What each kind of person gets

| Who signs in | What happens |
| --- | --- |
| Someone whose address already has a StartupOS user | Signed in **to their own company** — the tenant that user belongs to, never yours. |
| Someone Google verifies who has **no** account anywhere | Offered **a new company of their own**: they name it and get an empty brain, their own tenant, their own data. This is what makes sharing the URL work. |
| Someone whose account exists but is **disabled or never activated** | Refused, with "that account is not active — ask that company's owner to re-invite you". They are *not* given a second, empty company. |
| Someone whose Google address is unverified | Refused. Google itself will not vouch for the address, so neither will we. |

**Joining an existing company is invite-only.** There is no "request to join": the owner has to create the user
with that exact e-mail address. So the sign-up screen says, in as many words, that it is creating a *new, empty*
company and that anyone who was invited to a colleague's company should stop, get invited to that address, and
sign in again. If a colleague of yours ends up alone in an empty tenant, that copy is the thing to fix.

---

## 8. When something is wrong

| What you see | What it is | What to do |
| --- | --- | --- |
| Google: `Error 400: redirect_uri_mismatch` | The URI StartupOS sent is not in the client's list. The error page shows the exact string it sent — compare it letter by letter. | Add that exact string under **Authorised redirect URIs**, or fix `STARTUPOS_PUBLIC_URL`. Changes can take a few minutes. |
| Google: `Error 403: access_denied` / "has not completed the Google verification process" | The consent screen is still in **Testing** and this person is not a test user. | Step 5. |
| Google: `Error 401: invalid_client` | Wrong `GOOGLE_CLIENT_ID`, or the secret does not belong to that ID. | Re-copy both from **Credentials**, restart the API. |
| StartupOS: **"This sign-in link has expired"** | The state cookie did not come back: the attempt is older than 10 minutes, it started in another browser or tab, someone opened the callback URL directly, or the browser is blocking cookies. | Press "Back to sign-in" and start again. If it happens every time, see the two rows below. |
| **"This sign-in link has expired"**, always, right after deploying | `STARTUPOS_COOKIE_SECURE=1` while the site is served over plain HTTP, so the browser refuses to store the cookie. Or `STARTUPOS_PATH_PREFIX` and `STARTUPOS_PUBLIC_URL` disagree, so the cookie is written under one path and asked for under another. | Serve HTTPS (HOSTING.md), and keep the six public-hosting variables in step 4 in step with each other. The API logs a warning at start-up when the last two disagree. |
| StartupOS: **"Google has not verified that address"** | `email_verified` was false in Google's answer — usually an unverified alias or a very old account. | The person verifies the address with Google, or signs in with a different account. |
| StartupOS: **"Google sign-in is not configured"** (503) | No `GOOGLE_CLIENT_ID` in the API's environment. | Step 4, then restart the API. `curl https://<domain>/api/auth/providers`. |
| Signed in, but it is the wrong company | Two StartupOS users share one e-mail address across two tenants. | Decide which one is real and delete the other user row; the address should exist once. |
| Everything works for you, nobody else | You are the only test user. | Step 5. Honestly: step 5. |

Nothing in this flow is logged: no codes, no id_tokens, no cookies. When you need to see what Google actually
sent, read the error page in the browser — that is the whole diagnostic surface, on purpose.

---

## 9. Later

* **Rotating the secret.** Credentials → your client → **Add secret**, put the new one in `.env`, restart the
  API, then delete the old one. Sessions already issued are unaffected: they are StartupOS's own cookies, not
  Google's.
* **Moving to a different domain.** Add the new origin and redirect URI to the *same* client before you move,
  change the four URL variables, redeploy, and only then remove the old entries. See HOSTING.md §7.
* **Removing somebody's access.** Delete or disable their user row. Revoking it in Google only stops future
  sign-ins; the StartupOS session cookie lives up to `STARTUPOS_SESSION_TTL_HOURS` (14 days by default) and is
  killed by disabling the user, which takes effect on their next request.
