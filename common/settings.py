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


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", "postgresql://postgres@localhost:5432/startupos")
    )
    test_dsn: str = field(
        default_factory=lambda: _env("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test")
    )
    tenant_id: str = field(default_factory=lambda: _env("TENANT_ID", "unitone"))
    tier2_model: str = field(default_factory=lambda: _env("STARTUPOS_TIER2_MODEL", "claude-sonnet-4-5"))
    tier1_model: str = field(default_factory=lambda: _env("STARTUPOS_TIER1_MODEL", "claude-haiku-4-5"))
    slack_channels: tuple[str, ...] = field(
        default_factory=lambda: tuple(c for c in (_env("SLACK_CHANNELS", "") or "").split(",") if c)
    )
    slack_digest_channel: str | None = field(default_factory=lambda: _env("SLACK_DIGEST_CHANNEL"))
    vercel_team_id: str | None = field(default_factory=lambda: _env("VERCEL_TEAM_ID"))

    # Secrets: resolved on demand so they never sit on the dataclass repr.
    def secret(self, ref: str) -> str | None:
        """Resolve a secret_ref like 'env:LINEAR_API_KEY'. Key Vault refs ('kv:...') are a v2 concern."""
        scheme, _, name = ref.partition(":")
        if scheme == "env":
            return _env(name)
        raise NotImplementedError(f"secret scheme not supported in v1: {scheme}")

    def __repr__(self) -> str:  # pragma: no cover
        return f"Settings(tenant_id={self.tenant_id!r}, database_url=<redacted>)"


settings = Settings()
