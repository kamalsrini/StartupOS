"""The deploy shape, asserted from the files, because `docker compose config` needs a daemon and CI has none.

Sprint 3d put this deployment on the public internet. Three properties decide whether that is safe, and all
three live in files nothing else tests:

  1. `docker compose up` with STARTUPOS_DOMAIN unset must still be the pre-Sprint-3d stack — same services, same
     published ports — or every existing VM changes shape the next time someone deploys.
  2. Nothing but the proxy may be published on 0.0.0.0. The raw API and the database are what the VM's NSG used
     to protect; now the compose file does.
  3. No `.env` key may carry a trailing comment on an EMPTY value. Track H found this the expensive way:
     `docker compose`'s dotenv parser read `KEY=   # note` as the VALUE, so a freshly copied `.env` shipped a
     non-empty STARTUPOS_BOOTSTRAP_TOKEN and quietly enabled POST /auth/bootstrap with a token published in this
     repository. This test is the generalisation, so the footgun cannot come back on the next key somebody adds.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
CADDYFILE = (ROOT / "Caddyfile").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()

# What a bare `docker compose up -d` has always started, and must keep starting.
BASE_SERVICES = {"db", "migrate", "ingest", "daemon", "api"}
PUBLIC_PROFILE_SERVICES = {"web", "caddy"}


def profiles(service: str) -> list[str]:
    return COMPOSE["services"][service].get("profiles") or []


def test_a_bare_compose_up_is_the_stack_that_is_already_deployed():
    assert set(COMPOSE["services"]) == BASE_SERVICES | PUBLIC_PROFILE_SERVICES
    for name in BASE_SERVICES:
        assert profiles(name) == [], f"{name} must start without a profile"
    for name in PUBLIC_PROFILE_SERVICES:
        assert profiles(name) == ["public"], f"{name} must be behind the `public` profile"


def test_only_the_proxy_is_published_on_every_interface():
    """The API's and the database's published ports are loopback by default; 22/80/443 is the whole attack surface."""
    for name, expected in (("api", "8000"), ("db", "5432")):
        published = COMPOSE["services"][name]["ports"]
        assert len(published) == 1
        assert published[0].startswith("${STARTUPOS_"), published  # an operator can opt in, deliberately
        assert ":-127.0.0.1}" in published[0], published  # …but never by leaving something empty
        assert published[0].endswith(f"{expected}:{expected}")
    assert COMPOSE["services"]["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]
    assert "ports" not in COMPOSE["services"]["web"]  # the app is reachable only through the proxy


def test_every_compose_variable_treats_an_empty_value_as_unset():
    """`${VAR-default}` uses the default only when VAR is UNSET; `${VAR:-default}` also when it is EMPTY.

    A `.env` is full of keys an operator leaves blank. Anything reading one with `-` instead of `:-` would take
    the empty string — a database with no password, a proxy with no site address — rather than the default.
    """
    raw = (ROOT / "docker-compose.yml").read_text()
    for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}", raw):
        name, rest = match.group(1), match.group(2)
        assert rest == "" or rest.startswith(":-"), f"${{{name}{rest}}} must use ':-', not '{rest[:2]}'"


def test_with_no_domain_the_proxy_serves_plain_http_and_never_asks_for_a_certificate():
    env = COMPOSE["services"]["caddy"]["environment"]
    assert env["SITE_ADDRESS"] == "${STARTUPOS_DOMAIN:-:80}"  # no domain -> ":80" -> no ACME
    # Caddy's `email` global needs an argument, and an empty-but-set env var does not fall back in the Caddyfile.
    assert env["STARTUPOS_ACME_EMAIL"].endswith(":-acme@localhost}")
    assert "{$SITE_ADDRESS::80}" in CADDYFILE
    # Certificates must survive a redeploy or Let's Encrypt's rate limit becomes the outage.
    assert "caddy_data:/data" in COMPOSE["services"]["caddy"]["volumes"]
    assert "caddy_data" in COMPOSE["volumes"]


def test_the_proxy_passes_the_api_prefix_through_unchanged():
    """`uri strip_prefix /api` here would break every Set-Cookie Path the API writes. It must never appear."""
    directives = [ln.strip() for ln in CADDYFILE.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any("strip_prefix" in ln for ln in directives), directives
    assert "@api path /api /api/*" in CADDYFILE  # exactly /api and below — not /apifoo
    assert "reverse_proxy api:8000" in CADDYFILE and "reverse_proxy web:3000" in CADDYFILE
    assert COMPOSE["services"]["api"]["environment"]["STARTUPOS_PATH_PREFIX"] == "${STARTUPOS_PATH_PREFIX:-}"


def test_the_proxy_sets_the_security_headers_and_hides_itself():
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options"):
        assert header in CADDYFILE, header
    assert "-Server" in CADDYFILE
    # Caddy's admin API (localhost:2019 inside the container) must never be published or bound outward.
    live = [ln.strip() for ln in CADDYFILE.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any(ln.startswith("admin") for ln in live), live
    assert not any(str(p).startswith(("2019", "0.0.0.0:2019")) for p in COMPOSE["services"]["caddy"]["ports"])


def test_no_env_example_key_hides_a_comment_in_an_empty_value():
    """The Track H footgun, generalised: `KEY=   # note` is parsed as a VALUE by docker compose's dotenv reader.

    On STARTUPOS_BOOTSTRAP_TOKEN that turned POST /auth/bootstrap on, with the value being a comment printed in
    this repository. Any key an operator is meant to leave blank has to keep its explanation on its own line.
    """
    offenders = []
    for lineno, line in enumerate(ENV_EXAMPLE.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if "#" in value and not value.split("#", 1)[0].strip():
            offenders.append(f"{lineno}: {key.strip()}")
    assert offenders == [], f".env.example keys with an empty value and a trailing comment: {offenders}"


def test_env_example_documents_every_variable_the_deploy_reads():
    """A public deploy that is missing one of these is either insecure or broken; deploy_azure.sh checks them too."""
    keys = {
        line.split("=", 1)[0].strip() for line in ENV_EXAMPLE.splitlines() if "=" in line and not line.startswith("#")
    }
    required = {
        "STARTUPOS_SESSION_SECRET",
        "STARTUPOS_PUBLIC_URL",
        "STARTUPOS_WEB_URL",
        "STARTUPOS_DOMAIN",
        "STARTUPOS_ACME_EMAIL",
        "STARTUPOS_PATH_PREFIX",
        "STARTUPOS_COOKIE_SECURE",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "STARTUPOS_MASTER_KEY",
        # Sprint 3d PE review: the two switches that decide what a shared URL costs, and the docs override.
        "STARTUPOS_ALLOW_SIGNUP",
        "STARTUPOS_SIGNUP_TIER2_TOKENS",
        "STARTUPOS_ENABLE_DOCS",
    }
    assert required <= keys, required - keys
    # Cookies must be Secure by default in the shipped example; only a local override turns that off.
    assert re.search(r"^STARTUPOS_COOKIE_SECURE=1\b", ENV_EXAMPLE, re.M)


def test_the_shipped_example_ships_the_open_but_capped_sign_up_defaults():
    """Sprint 3d PE review, fix 1. Sharing the link is the point, so sign-up ships OPEN — but a self-serve
    company must ship with an allowance far below the founder tier, and the example is where an operator
    discovers both facts. The docs override ships EMPTY: a value here would follow a `cp .env.example .env`
    onto a public host and put /docs back on it."""
    from daemon import budget

    assert re.search(r"^STARTUPOS_ALLOW_SIGNUP=1\b", ENV_EXAMPLE, re.M)
    match = re.search(r"^STARTUPOS_SIGNUP_TIER2_TOKENS=(\d+)\s*$", ENV_EXAMPLE, re.M)
    assert match, "STARTUPOS_SIGNUP_TIER2_TOKENS must ship with a concrete, conservative value"
    shipped = int(match.group(1))
    assert shipped == budget.DEFAULT_SIGNUP_TIER2_TOKENS  # the file and the code agree
    assert shipped < budget.ALLOWED_BY_TIER["founder"] / 4
    assert re.search(r"^STARTUPOS_ENABLE_DOCS=\s*$", ENV_EXAMPLE, re.M)
