"""Settings, read once from the environment (`shared/.env` on the server)."""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_PREFIX = "HUB_BOT_TOKEN_"  # noqa: S105 -- a variable-name prefix, not a secret


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    secret_key: bytes
    data_dir: Path
    bots_dir: Path
    public_origin: str
    cookie_secure: bool = True
    alert_bot: str | None = None
    timezone: str = "Asia/Kolkata"
    bot_tokens: Mapping[str, str] = field(default_factory=dict)

    def subkey(self, label: str) -> bytes:
        """A separate key per purpose, all derived from the one secret."""
        return hmac.new(self.secret_key, label.encode(), hashlib.sha256).digest()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def session_cookie(self) -> str:
        # The __Host- prefix makes the browser refuse the cookie unless it is
        # Secure, has Path=/ and no Domain. Plain http (local dev) can't use it.
        return "__Host-session" if self.cookie_secure else "session"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    secret = env.get("HUB_SECRET_KEY", "")
    if len(secret) < 32:
        raise ConfigError("HUB_SECRET_KEY must be set to at least 32 random characters.")

    origin = env.get("HUB_PUBLIC_ORIGIN", "").rstrip("/")
    if not origin.startswith(("https://", "http://")):
        raise ConfigError("HUB_PUBLIC_ORIGIN must be the site's origin, e.g. https://dashboard-x.l00t.link")

    tokens = {
        key[len(TOKEN_PREFIX) :].lower(): value for key, value in env.items() if key.startswith(TOKEN_PREFIX) and value
    }

    return Settings(
        secret_key=secret.encode(),
        data_dir=Path(env.get("HUB_DATA_DIR", "/data")),
        bots_dir=Path(env.get("HUB_BOTS_DIR", "/config")),
        public_origin=origin,
        cookie_secure=env.get("HUB_COOKIE_SECURE", "1") != "0",
        alert_bot=(env.get("HUB_ALERT_BOT") or "").strip().lower() or None,
        timezone=env.get("HUB_TIMEZONE") or "Asia/Kolkata",
        bot_tokens=tokens,
    )
