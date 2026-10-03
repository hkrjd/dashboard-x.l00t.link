"""The bots the hub knows about, and the client that talks to their APIs.

Each bot has a folder next to `shared/` on the server holding `config.toml`
(display name and API URL, no secrets). Its token comes from `shared/.env` as
`HUB_BOT_TOKEN_<NAME>`.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
API_PREFIX = "/api/v1/"
READ_TIMEOUT = httpx.Timeout(5.0)
WRITE_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass(frozen=True)
class Bot:
    name: str
    display_name: str
    api_url: str
    token: str | None

    @property
    def configured(self) -> bool:
        return bool(self.token)


def load_bots(settings: Settings) -> dict[str, Bot]:
    bots: dict[str, Bot] = {}
    if not settings.bots_dir.is_dir():
        log.warning("Bots folder %s does not exist", settings.bots_dir)
        return bots
    for folder in sorted(settings.bots_dir.iterdir()):
        config_file = folder / "config.toml"
        if folder.name == "shared" or not config_file.is_file():
            continue
        if not NAME_RE.match(folder.name):
            log.warning("Skipping bot folder with an unusable name: %r", folder.name)
            continue
        try:
            data = tomllib.loads(config_file.read_text(encoding="utf-8"))
            api_url = str(data["api_url"]).rstrip("/")
        except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
            log.warning("Skipping bot %s: bad config.toml (%s)", folder.name, exc)
            continue
        if not api_url.startswith(("http://", "https://")):
            log.warning("Skipping bot %s: api_url must be http(s)", folder.name)
            continue
        bots[folder.name] = Bot(
            name=folder.name,
            display_name=str(data.get("display_name") or folder.name),
            api_url=api_url,
            token=settings.bot_tokens.get(folder.name),
        )
    return bots


class BotError(Exception):
    """A bot call that did not succeed, with a message fit to show the owner."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @property
    def offline(self) -> bool:
        return self.code in {"offline", "timeout"}


@dataclass(frozen=True)
class BotReply:
    status: int
    data: dict[str, Any]


class BotClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def get(self, bot: Bot, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        reply = await self._call(bot, "GET", path, params=params, timeout=READ_TIMEOUT)
        return reply.data

    async def send(
        self,
        bot: Bot,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        request_id: str,
        actor: str,
    ) -> BotReply:
        headers = {"X-Request-Id": request_id, "X-Actor": actor}
        return await self._call(bot, method, path, json=body or {}, headers=headers, timeout=WRITE_TIMEOUT)

    async def _call(self, bot: Bot, method: str, path: str, *, timeout: httpx.Timeout, **kwargs) -> BotReply:
        if not bot.configured:
            raise BotError(0, "not_configured", f"No API token is set for {bot.display_name}.")
        if not path.startswith(API_PREFIX) or ".." in path:
            raise BotError(0, "bad_path", "That address is not part of the bot API.")
        headers = {"Authorization": f"Bearer {bot.token}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        try:
            response = await self.http.request(method, bot.api_url + path, headers=headers, timeout=timeout, **kwargs)
        except httpx.TimeoutException:
            raise BotError(0, "timeout", f"{bot.display_name} did not answer in time.") from None
        except httpx.HTTPError:
            raise BotError(0, "offline", f"{bot.display_name} is not reachable.") from None

        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {}

        if response.is_success:
            return BotReply(response.status_code, data)

        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        message = str(error.get("message") or f"{bot.display_name} answered with HTTP {response.status_code}.")
        code = str(error.get("code") or f"http_{response.status_code}")
        if response.status_code == 401:
            message = f"{bot.display_name} refused the hub's token. Check HUB_BOT_TOKEN_{bot.name.upper()}."
        raise BotError(response.status_code, code, message)
