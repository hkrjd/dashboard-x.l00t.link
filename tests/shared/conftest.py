from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pyotp
import pytest
from fastapi.testclient import TestClient

from hub.admin import Admin
from hub.app import create_app
from hub.config import Settings
from hub.db import Store

ORIGIN = "https://testserver"
USERNAME = "owner"
PASSWORD = "correct horse battery"
BOT_TOKEN = "t" * 40


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    bots_dir = tmp_path / "config"
    (bots_dir / "dealops").mkdir(parents=True)
    (bots_dir / "dealops" / "config.toml").write_text(
        'display_name = "DealOps"\napi_url = "http://dealops:8081"\n', encoding="utf-8"
    )
    (bots_dir / "shared").mkdir()
    return Settings(
        secret_key=b"s" * 40,
        data_dir=tmp_path / "data",
        bots_dir=bots_dir,
        public_origin=ORIGIN,
        cookie_secure=True,
        alert_bot="dealops",
        bot_tokens={"dealops": BOT_TOKEN},
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    store = Store(settings.db_path)
    store.init()
    return store


@pytest.fixture
def admin(store: Store, settings: Settings) -> Admin:
    return Admin(store, settings)


@dataclass
class FakeBot:
    """Stands in for a bot API on hub_net."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    reply: tuple[int, dict[str, Any]] = (200, {"message": "Saved."})
    online: bool = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.online:
            raise httpx.ConnectError("down", request=request)
        if request.headers.get("Authorization") != f"Bearer {BOT_TOKEN}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/meta":
            return httpx.Response(
                200,
                json={
                    "name": "dealops",
                    "version": "abc123def456789",
                    "health": "healthy",
                    "pages": [{"id": "dashboard", "title": "Dashboard"}, {"id": "channels", "title": "Channels"}],
                },
            )
        if request.method == "GET" and (m := re.fullmatch(r"/api/v1/pages/(.+)", path)):
            page = self.pages.get(m.group(1))
            if page is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "No such page."}})
            return httpx.Response(200, json=page)
        if request.method == "GET" and (m := re.fullmatch(r"/api/v1/jobs/(.+)", path)):
            job = self.jobs.get(m.group(1))
            if job is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "No such job."}})
            return httpx.Response(200, json=job)
        status, body = self.reply
        return httpx.Response(status, json=body)

    def sent(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    @staticmethod
    def body(request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content or b"{}")


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def make_client(settings: Settings, fake_bot: FakeBot):
    clients = []

    def make() -> TestClient:
        app = create_app(settings, transport=httpx.MockTransport(fake_bot.handler))

        async def no_sleep(seconds: float) -> None:
            app.state.hub.slept = getattr(app.state.hub, "slept", []) + [seconds]

        app.state.hub.sleep = no_sleep
        client = TestClient(app, base_url=ORIGIN, headers={"Sec-Fetch-Site": "same-origin"})
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def enrolled(client: TestClient):
    """The admin exists; returns the TOTP generator."""
    hub = client.app.state.hub
    enrolment = hub.admin.create(USERNAME, PASSWORD)
    secret = pyotp.parse_uri(enrolment.otpauth_uri).secret
    return pyotp.TOTP(secret), enrolment.backup_codes


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field on the page"
    return match.group(1)


def next_code(totp: pyotp.TOTP, step_offset: int = 0) -> str:
    return totp.generate_otp(int(time.time() // 30) + step_offset)


def log_in(client: TestClient, totp: pyotp.TOTP, step_offset: int = 0) -> str:
    """Logs in through both steps; returns the csrf token of the session."""
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303, response.text
    page = client.get("/login/otp")
    response = client.post(
        "/login/otp",
        data={"csrf": csrf_of(page.text), "code": next_code(totp, step_offset)},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/", response.text
    home = client.get("/")
    assert home.status_code == 200
    return csrf_of(home.text)
