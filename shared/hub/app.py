"""The hub web app: login, the bot list, and every bot page."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import actions
from .actions import ActionError
from .admin import Admin
from .audit import Audit
from .backup import backup_loop
from .bots import Bot, BotClient, BotError, load_bots
from .config import Settings
from .db import Store
from .pages import PAGE_ID_RE, PageBuilder, job_view
from .sessions import (
    ALERT_AFTER_FAILURES,
    FULL_MAX,
    PREAUTH_MAX_FAILURES,
    PREAUTH_TTL,
    Session,
    Sessions,
    Throttle,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent  # shared/
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALERT_EVERY = 60 * 60

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class NeedLogin(Exception):
    pass


class NotFound(Exception):
    pass


class Hub:
    """Everything a request handler needs, built once per app."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.admin = Admin(self.store, settings)
        self.sessions = Sessions(self.store)
        self.throttle = Throttle(self.store)
        self.audit = Audit(self.store)
        self.bots: dict[str, Bot] = load_bots(settings)
        self.transport = transport
        self.http: httpx.AsyncClient | None = None
        self.signing_key = settings.subkey("actions")
        self.preauth_cookie = "__Host-preauth" if settings.cookie_secure else "preauth"
        self._tasks: set[asyncio.Task] = set()
        self.sleep = asyncio.sleep  # replaced in tests

    @property
    def client(self) -> BotClient:
        assert self.http is not None
        return BotClient(self.http)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def client_ip(request: Request) -> str:
    # uvicorn has already replaced this with X-Forwarded-For when, and only
    # when, the request came from the trusted proxy (--forwarded-allow-ips).
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    hub = Hub(settings, transport)
    tz = ZoneInfo(settings.timezone)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.store.init()
        hub.sessions.purge_expired()
        hub.http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        backup_task = asyncio.create_task(backup_loop(settings.db_path, settings.backups_dir))
        try:
            yield
        finally:
            backup_task.cancel()
            await hub.http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    def fmt_time(ts: float | None) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts, tz).strftime("%d %b %Y, %H:%M:%S")

    templates.env.filters["when"] = fmt_time

    # -- middleware ----------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in SAFE_METHODS and not _same_origin(request, settings.public_origin):
            response: Response = PlainTextResponse("Cross-site request refused.", status_code=403)
        else:
            response = await call_next(request)
        headers = response.headers
        headers["Content-Security-Policy"] = CSP
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Frame-Options"] = "DENY"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not request.url.path.startswith("/static/"):
            headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NeedLogin)
    async def need_login(request: Request, exc: NeedLogin):
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    # -- helpers -------------------------------------------------------------

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def full_session(request: Request) -> Session:
        session = hub.sessions.get(request.cookies.get(settings.session_cookie), "full")
        if session is None:
            raise NeedLogin()
        return session

    async def check_csrf(request: Request, session: Session) -> dict[str, str]:
        form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
        sent = request.headers.get("X-CSRF-Token") or form.get("csrf") or ""
        if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
            raise NeedLogin()
        return form

    def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
        )

    def clear_cookie(response: Response, name: str) -> None:
        # Browsers ignore a __Host- cookie (even one that deletes) without Secure.
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")

    def bot_or_404(name: str) -> Bot:
        bot = hub.bots.get(name)
        if bot is None:
            raise NotFound()
        return bot

    def maybe_alert(ip: str) -> None:
        failures = hub.throttle.failures_in_a_row()
        if failures < ALERT_AFTER_FAILURES or not settings.alert_bot:
            return
        last = float(hub.store.get_kv("last_login_alert") or 0)
        if time.time() - last < ALERT_EVERY:
            return
        bot = hub.bots.get(settings.alert_bot)
        if bot is None:
            return
        hub.store.set_kv("last_login_alert", str(time.time()))
        text = (
            f"dashboard-x: {failures} failed logins in a row. "
            f"Last from {ip}. If this was not you, check the Security page."
        )

        async def send() -> None:
            try:
                await hub.client.send(
                    bot,
                    "POST",
                    "/api/v1/alerts",
                    {"text": text},
                    request_id=str(uuid.uuid4()),
                    actor="dashboard-x",
                )
            except BotError as exc:
                log.warning("Login alert through %s failed: %s", bot.name, exc.message)

        hub.spawn(send())

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return PlainTextResponse("Not found.", status_code=404)

    # -- login ---------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/empty")
    async def empty() -> HTMLResponse:
        return HTMLResponse("")

    @app.get("/login")
    async def login_page(request: Request):
        if hub.sessions.get(request.cookies.get(settings.session_cookie), "full"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error=None)

    @app.post("/login")
    async def login(request: Request):
        ip = client_ip(request)
        form = await request.form()
        username = str(form.get("username") or "")[:100]
        password = str(form.get("password") or "")[:1000]

        if hub.throttle.ip_blocked(ip):
            hub.audit.record("login", "blocked", ip=ip, detail="too many failures from this address")
            return render(
                request,
                "login.html",
                429,
                error="Too many failed attempts from this address. Try again in 15 minutes.",
            )

        # Wait longer after each failure in a row, but still check: the
        # owner's right password always gets in.
        await hub.sleep(hub.throttle.delay_seconds())
        ok = await run_in_threadpool(hub.admin.check_password, username, password)
        hub.throttle.record(ip, username, "password", ok)
        if not ok:
            hub.audit.record("login", "wrong password", ip=ip, detail=f"username={username!r}")
            maybe_alert(ip)
            return render(request, "login.html", 401, error="Wrong username or password.")

        raw, _ = hub.sessions.create("preauth", ip, request.headers.get("user-agent"))
        response = RedirectResponse("/login/otp", status_code=303)
        set_cookie(response, hub.preauth_cookie, raw, PREAUTH_TTL)
        return response

    @app.get("/login/otp")
    async def otp_page(request: Request):
        pre = hub.sessions.get(request.cookies.get(hub.preauth_cookie), "preauth")
        if pre is None:
            return RedirectResponse("/login", status_code=303)
        return render(request, "otp.html", csrf=pre.csrf, error=None)

    @app.post("/login/otp")
    async def otp(request: Request):
        ip = client_ip(request)
        pre = hub.sessions.get(request.cookies.get(hub.preauth_cookie), "preauth")
        if pre is None:
            return RedirectResponse("/login", status_code=303)
        form = await check_csrf(request, pre)
        if hub.throttle.ip_blocked(ip):
            hub.sessions.delete(pre)
            return render(
                request, "login.html", 429, error="Too many failed attempts from this address. Try again in 15 minutes."
            )

        method = await run_in_threadpool(hub.admin.check_second_factor, form.get("code", "")[:40])
        hub.throttle.record(ip, None, "otp", method is not None)
        if method is None:
            failures = hub.sessions.add_failure(pre)
            hub.audit.record("login", "wrong code", ip=ip)
            if failures >= PREAUTH_MAX_FAILURES:
                response = RedirectResponse("/login", status_code=303)
                clear_cookie(response, hub.preauth_cookie)
                return response
            left = PREAUTH_MAX_FAILURES - failures
            return render(request, "otp.html", 401, csrf=pre.csrf, error=f"Wrong code. {left} tries left.")

        hub.sessions.delete(pre)
        raw, _ = hub.sessions.create("full", ip, request.headers.get("user-agent"))
        hub.audit.record("login", "ok", ip=ip, detail=f"second factor: {method}")
        response = RedirectResponse("/", status_code=303)
        clear_cookie(response, hub.preauth_cookie)
        set_cookie(response, settings.session_cookie, raw, FULL_MAX)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = full_session(request)
        await check_csrf(request, session)
        hub.sessions.delete(session)
        hub.audit.record("logout", "ok", ip=client_ip(request))
        response = RedirectResponse("/login", status_code=303)
        clear_cookie(response, settings.session_cookie)
        return response

    @app.post("/logout-all")
    async def logout_all(request: Request):
        session = full_session(request)
        await check_csrf(request, session)
        count = hub.sessions.delete_all()
        hub.audit.record("logout everywhere", "ok", ip=client_ip(request), detail=f"{count} sessions ended")
        response = RedirectResponse("/login", status_code=303)
        clear_cookie(response, settings.session_cookie)
        return response

    # -- pages ---------------------------------------------------------------

    async def bot_meta(bot: Bot) -> dict[str, Any]:
        try:
            meta = await hub.client.get(bot, "/api/v1/meta")
        except BotError as exc:
            return {"ok": False, "error": exc.message}
        pages = [
            {"id": str(p.get("id")), "title": str(p.get("title") or p.get("id"))[:100]}
            for p in meta.get("pages") or []
            if isinstance(p, dict) and PAGE_ID_RE.match(str(p.get("id", "")))
        ]
        return {
            "ok": True,
            "version": str(meta.get("version") or "")[:40],
            "health": str(meta.get("health") or "")[:200],
            "pages": pages,
        }

    @app.get("/")
    async def home(request: Request):
        session = full_session(request)
        bots = list(hub.bots.values())
        metas = await asyncio.gather(*(bot_meta(bot) for bot in bots))
        return render(request, "home.html", csrf=session.csrf, signed_in=True, bots=list(zip(bots, metas, strict=True)))

    @app.get("/security")
    async def security(request: Request):
        session = full_session(request)
        return render(
            request,
            "security.html",
            csrf=session.csrf,
            signed_in=True,
            sessions=hub.sessions.list_full(),
            current=session.id_hash,
            attempts=hub.throttle.recent(),
            backup_codes_left=hub.admin.unused_backup_codes(),
        )

    @app.get("/audit")
    async def audit_page(request: Request, page: int = 1):
        session = full_session(request)
        page = max(1, page)
        rows = hub.audit.recent(limit=51, offset=(page - 1) * 50)
        return render(
            request,
            "audit.html",
            csrf=session.csrf,
            signed_in=True,
            rows=rows[:50],
            page=page,
            has_next=len(rows) > 50,
        )

    @app.get("/bots/{name}")
    async def bot_home(request: Request, name: str):
        full_session(request)
        bot = bot_or_404(name)
        meta = await bot_meta(bot)
        first = meta["pages"][0]["id"] if meta.get("pages") else "dashboard"
        return RedirectResponse(f"/bots/{bot.name}/pages/{first}", status_code=303)

    async def load_page(bot: Bot, page_id: str) -> tuple[dict[str, Any] | None, str | None]:
        if not PAGE_ID_RE.match(page_id):
            return None, "That page does not exist."
        try:
            raw = await hub.client.get(bot, f"/api/v1/pages/{page_id}")
        except BotError as exc:
            return None, exc.message
        return PageBuilder(bot.name, hub.signing_key).build(raw), None

    @app.get("/bots/{name}/pages/{page_id}")
    async def bot_page(request: Request, name: str, page_id: str):
        session = full_session(request)
        bot = bot_or_404(name)
        meta, (page, error) = await asyncio.gather(bot_meta(bot), load_page(bot, page_id))
        return render(
            request,
            "bot_page.html",
            csrf=session.csrf,
            signed_in=True,
            bot=bot,
            meta=meta,
            page_id=page_id,
            page=page,
            error=error,
        )

    @app.get("/bots/{name}/pages/{page_id}/blocks")
    async def bot_blocks(request: Request, name: str, page_id: str):
        session = full_session(request)
        bot = bot_or_404(name)
        page, error = await load_page(bot, page_id)
        return render(request, "_blocks.html", csrf=session.csrf, bot=bot, page_id=page_id, page=page, error=error)

    @app.post("/bots/{name}/act")
    async def act(request: Request, name: str):
        session = full_session(request)
        form = await check_csrf(request, session)
        bot = bot_or_404(name)
        ip = client_ip(request)
        try:
            action = actions.verify(hub.signing_key, form.get("action", ""))
            if action.bot != bot.name:
                raise ActionError("action belongs to another bot")
            body = actions.with_form_values(action, form)
        except ActionError as exc:
            hub.audit.record("action", "refused", ip=ip, bot=bot.name, detail=str(exc))
            return render(request, "_flash.html", 400, kind="bad", message=f"Not sent: {exc}.")

        if action.confirm and form.get("confirmed") != "1":
            return render(
                request,
                "_confirm.html",
                csrf=session.csrf,
                bot=bot,
                action=action,
                token=form["action"],
                values={f["name"]: body.get(f["name"]) for f in action.fields},
            )

        request_id = str(uuid.uuid4())
        what = f"{action.label}: {action.method} {action.path}"
        try:
            reply = await hub.client.send(bot, action.method, action.path, body, request_id=request_id, actor="owner")
        except BotError as exc:
            hub.audit.record(
                what, f"failed ({exc.code})", ip=ip, bot=bot.name, request_id=request_id, detail=exc.message
            )
            return render(request, "_flash.html", kind="bad", message=exc.message)

        job_id = str(reply.data.get("job_id") or "")
        if reply.status == 202 and JOB_ID_RE.match(job_id):
            hub.audit.record(what, "job started", ip=ip, bot=bot.name, request_id=request_id, detail=f"job {job_id}")
            job = {"id": job_id, "state": "queued", "kind": reply.data.get("kind")}
            return render(request, "_job.html", bot=bot, job=job_view(job))

        message = str(reply.data.get("message") or "Done.")[:2000]
        hub.audit.record(what, "ok", ip=ip, bot=bot.name, request_id=request_id, detail=message)
        response = render(request, "_flash.html", kind="good", message=message)
        response.headers["HX-Trigger"] = "page-refresh"
        return response

    @app.get("/bots/{name}/jobs/{job_id}")
    async def job(request: Request, name: str, job_id: str):
        full_session(request)
        bot = bot_or_404(name)
        if not JOB_ID_RE.match(job_id):
            raise NotFound()
        try:
            data = await hub.client.get(bot, f"/api/v1/jobs/{job_id}")
        except BotError as exc:
            if exc.status == 404:
                view = {
                    "id": job_id,
                    "state": "failed",
                    "finished": True,
                    "lost": True,
                    "message": "The bot restarted; the outcome is unknown. Check the bot's logs page.",
                }
            else:
                # A hiccup while polling: keep polling.
                view = {"id": job_id, "state": "running", "finished": False, "message": exc.message}
            return render(request, "_job.html", bot=bot, job=view)
        view = job_view(data)
        response = render(request, "_job.html", bot=bot, job=view)
        if view["finished"]:
            response.headers["HX-Trigger"] = "page-refresh"
        return response

    return app


def _same_origin(request: Request, public_origin: str) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("origin")
    if origin is not None:
        return origin.rstrip("/") == public_origin
    return False
