"""Environment-backed settings. The ONLY module that reads secrets. Never log values."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path

with contextlib.suppress(Exception):  # dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class SecretRefForbidden(ValueError):
    """An `env:` secret_ref was resolved for a tenant other than the install tenant. Never carries a value."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", "postgresql://postgres@localhost:5432/startupos")
    )
    test_dsn: str = field(
        default_factory=lambda: _env("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test")
    )
    # Services connect as the RLS-bound app role when this is set; superuser DATABASE_URL stays for migrations.
    app_dsn: str | None = field(default_factory=lambda: _env("STARTUPOS_APP_DSN"))
    # Auth
    session_secret: str | None = field(default_factory=lambda: _env("STARTUPOS_SESSION_SECRET"))
    session_ttl_hours: int = field(default_factory=lambda: int(_env("STARTUPOS_SESSION_TTL_HOURS", "336") or 336))
    google_client_id: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    public_url: str = field(default_factory=lambda: _env("STARTUPOS_PUBLIC_URL", "http://localhost:8000"))
    web_url: str = field(default_factory=lambda: _env("STARTUPOS_WEB_URL", "http://localhost:3000"))
    bootstrap_token: str | None = field(default_factory=lambda: _env("STARTUPOS_BOOTSTRAP_TOKEN"))
    cookie_secure: bool = field(default_factory=lambda: (_env("STARTUPOS_COOKIE_SECURE", "auto") or "auto") != "false")
    tenant_id: str = field(default_factory=lambda: _env("TENANT_ID", "unitone"))
    tier2_model: str = field(default_factory=lambda: _env("STARTUPOS_TIER2_MODEL", "claude-sonnet-4-5"))
    tier1_model: str = field(default_factory=lambda: _env("STARTUPOS_TIER1_MODEL", "claude-haiku-4-5"))
    slack_channels: tuple[str, ...] = field(
        default_factory=lambda: tuple(c for c in (_env("SLACK_CHANNELS", "") or "").split(",") if c)
    )
    slack_digest_channel: str | None = field(default_factory=lambda: _env("SLACK_DIGEST_CHANNEL"))
    vercel_team_id: str | None = field(default_factory=lambda: _env("VERCEL_TEAM_ID"))

    # Secrets: resolved on demand so they never sit on the dataclass repr.
    def master_key(self) -> str | None:
        """STARTUPOS_MASTER_KEY — 32 random bytes, urlsafe base64, wrapping every per-tenant secret (common/secrets.py).

        Generate with:  python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
        Read at call time (not cached on the dataclass) so it never appears in a repr and so tests can set it.
        """
        return _env("STARTUPOS_MASTER_KEY")

    # --- Slack app (Sprint 3b, Track I) --------------------------------------------------------------
    # ONE Slack app serves the whole install — that is how Slack distribution works. These three are
    # install-level operator values, never per tenant; the per-tenant bot tokens live in `tenant_secrets`
    # (`kv:slack_bot_token`) and are never environment variables. Read at call time so tests can set them.

    def slack_client_id(self) -> str | None:
        """STARTUPOS_SLACK_CLIENT_ID — Slack app → Basic Information → App Credentials."""
        return _env("STARTUPOS_SLACK_CLIENT_ID")

    def slack_client_secret(self) -> str | None:
        """STARTUPOS_SLACK_CLIENT_SECRET — exchanged for a bot token at oauth.v2.access. Never logged."""
        return _env("STARTUPOS_SLACK_CLIENT_SECRET")

    def slack_signing_secret(self) -> str | None:
        """STARTUPOS_SLACK_SIGNING_SECRET — HMAC key for every inbound Slack request (auth/slack_sig.py)."""
        return _env("STARTUPOS_SLACK_SIGNING_SECRET")

    def secret(self, ref: str, *, conn=None, tenant_id: str | None = None) -> str | None:
        """Resolve a secret_ref.

        - 'env:NAME'  → the process environment (operator-managed keys; unchanged since v1). These are the
                        install tenant's (TENANT_ID) own credentials: when `tenant_id` names any other tenant the
                        ref is refused (SecretRefForbidden) — a self-serve tenant must never ingest or act with
                        the operator's keys (PE review, Sprint 3a).
        - 'kv:NAME'   → the caller's tenant row in `tenant_secrets`, decrypted by common/secrets.py. Requires a
                        connection and a tenant_id; missing either is a programming error (ValueError), never a
                        silent fallback to another tenant or to the environment.
        """
        scheme, _, name = ref.partition(":")
        if scheme == "env":
            if tenant_id and tenant_id != self.tenant_id:
                raise SecretRefForbidden(f"env: secret refs belong to the install tenant, not {tenant_id!r}")
            return _env(name)
        if scheme == "kv":
            if conn is None or not tenant_id:
                raise ValueError("kv: secret refs need conn= and tenant_id= (per-tenant secrets are tenant-bound)")
            from common import secrets  # local import: secrets.py reads the master key from this module

            return secrets.get(conn, tenant_id, name)
        raise NotImplementedError(f"secret scheme not supported: {scheme}")

    def __repr__(self) -> str:  # pragma: no cover
        return f"Settings(tenant_id={self.tenant_id!r}, database_url=<redacted>)"


settings = Settings()
