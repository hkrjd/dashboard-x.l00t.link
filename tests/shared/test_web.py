import re
import time

from hub import actions
from hub.sessions import ALERT_AFTER_FAILURES, IP_MAX_FAILURES

from .conftest import PASSWORD, USERNAME, FakeBot, csrf_of, log_in, next_code

SWITCH_PAGE = {
    "title": "RAS Deals",
    "blocks": [
        {"type": "text", "text": "<script>alert(1)</script>"},
        {
            "type": "switches",
            "title": "Features",
            "items": [
                {
                    "label": "Fast Delete",
                    "on": False,
                    "turn_on": {
                        "method": "PUT",
                        "path": "/api/v1/channels/1/features/stock_fast_delete",
                        "body": {"on": True},
                        "confirm": "Fast Delete deletes posts. Turn it on?",
                        "danger": True,
                    },
                },
                {
                    "label": "Auto-delete",
                    "on": True,
                    "turn_off": {
                        "method": "PUT",
                        "path": "/api/v1/channels/1/features/auto_delete",
                        "body": {"on": False},
                    },
                },
            ],
        },
        {
            "type": "actions",
            "items": [{"label": "Cleanup this channel", "method": "POST", "path": "/api/v1/channels/1/cleanup"}],
        },
        {
            "type": "form",
            "title": "Duplicate time limit",
            "fields": [{"name": "minutes", "label": "Minutes", "kind": "number", "value": "60"}],
            "submit": {"label": "Save", "method": "PUT", "path": "/api/v1/channels/1/window"},
        },
    ],
}


def action_token(client, html: str, label: str) -> str:
    key = client.app.state.hub.signing_key
    for token in re.findall(r'name="action" value="([^"]+)"', html):
        signed = actions.verify(key, token).label
        if signed == label or signed.endswith(f": {label}"):
            return token
    raise AssertionError(f"no action {label!r}")


# -- login ---------------------------------------------------------------------


def test_pages_need_login(client):
    for path in ["/", "/security", "/audit", "/bots/dealops/pages/dashboard"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login", path
    htmx = client.get("/", headers={"HX-Request": "true"})
    assert htmx.headers["HX-Redirect"] == "/login"


def test_full_login_flow_and_cookie_flags(client, enrolled):
    totp, _ = enrolled
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-preauth=")
    for flag in ["HttpOnly", "Secure", "SameSite=strict", "Path=/"]:
        assert flag.lower() in cookie.lower()
    # The password alone opens nothing.
    assert client.get("/", follow_redirects=False).status_code == 303

    page = client.get("/login/otp")
    response = client.post(
        "/login/otp", data={"csrf": csrf_of(page.text), "code": next_code(totp)}, follow_redirects=False
    )
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-session=") for c in cookies)
    [cleared] = [c for c in cookies if c.startswith("__Host-preauth=")]
    assert "Secure" in cleared and "Max-Age=0" in cleared
    home = client.get("/")
    assert home.status_code == 200 and "DealOps" in home.text and "Online" in home.text


def test_wrong_password_and_unknown_user_look_the_same(client, enrolled):
    a = client.post("/login", data={"username": USERNAME, "password": "nope"})
    b = client.post("/login", data={"username": "ghost", "password": PASSWORD})
    assert a.status_code == b.status_code == 401
    assert "Wrong username or password." in a.text and "Wrong username or password." in b.text


def test_failures_slow_down_but_never_lock_the_owner_out(client, enrolled):
    totp, _ = enrolled
    hub = client.app.state.hub
    for _ in range(8):
        # Many addresses, so the per-IP block never triggers: a distributed attack.
        hub.throttle.record(f"10.0.0.{_}", USERNAME, "password", ok=False)
    log_in(client, totp)
    assert hub.slept[-1] == 30  # the owner waited, but got in


def test_ip_block(client, enrolled):
    for _ in range(IP_MAX_FAILURES):
        client.post("/login", data={"username": USERNAME, "password": "nope"})
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 429 and "Too many failed attempts" in response.text


def test_wrong_codes_end_the_preauth_session(client, enrolled):
    client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    csrf = csrf_of(client.get("/login/otp").text)
    for left in [4, 3, 2, 1]:
        response = client.post("/login/otp", data={"csrf": csrf, "code": "000000"})
        assert f"{left} tries left" in response.text
    response = client.post("/login/otp", data={"csrf": csrf, "code": "000000"}, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert client.get("/login/otp", follow_redirects=False).headers["location"] == "/login"


def test_otp_code_cannot_be_replayed(make_client, enrolled):
    totp, _ = enrolled
    # One code for both: computing it twice could straddle a 30 s step and
    # give a new, valid code (that made this test flaky in CI, 2026-10-03).
    code = next_code(totp)
    first = make_client()
    log_in(first, totp, code=code)
    second = make_client()
    second.post("/login", data={"username": USERNAME, "password": PASSWORD})
    csrf = csrf_of(second.get("/login/otp").text)
    response = second.post("/login/otp", data={"csrf": csrf, "code": code})
    assert response.status_code == 401


def test_backup_code_logs_in(client, enrolled):
    _, codes = enrolled
    client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    csrf = csrf_of(client.get("/login/otp").text)
    response = client.post("/login/otp", data={"csrf": csrf, "code": codes[3]}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_cross_site_post_is_refused(client, enrolled):
    response = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    no_headers = client.post(
        "/login", data={"username": USERNAME, "password": PASSWORD}, headers={"Sec-Fetch-Site": ""}
    )
    assert no_headers.status_code == 403


def test_security_headers(client):
    response = client.get("/login")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_logout_needs_csrf_and_logout_all(make_client, enrolled):
    totp, _ = enrolled
    client = make_client()
    csrf = log_in(client, totp)
    client.post("/logout", data={"csrf": "wrong"})
    assert client.get("/", follow_redirects=False).status_code == 200  # still logged in

    other = make_client()
    log_in(other, totp, step_offset=1)
    client.post("/logout-all", data={"csrf": csrf})
    assert client.get("/", follow_redirects=False).status_code == 303
    assert other.get("/", follow_redirects=False).status_code == 303


def test_login_alert_goes_through_the_bot(client, enrolled, fake_bot: FakeBot):
    for _ in range(ALERT_AFTER_FAILURES):
        client.post("/login", data={"username": USERNAME, "password": "nope"})
    deadline = time.monotonic() + 5  # the alert is sent in the background
    while not (alerts := [r for r in fake_bot.sent() if r.url.path == "/api/v1/alerts"]):
        assert time.monotonic() < deadline, "no alert sent"
        client.get("/healthz")
    assert len(alerts) == 1 and "failed logins" in FakeBot.body(alerts[0])["text"]
    client.post("/login", data={"username": USERNAME, "password": "nope"})
    assert len([r for r in fake_bot.sent() if r.url.path == "/api/v1/alerts"]) == 1  # once an hour at most


# -- bot pages -----------------------------------------------------------------


def test_bot_page_renders_escaped(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    log_in(client, enrolled[0])
    page = client.get("/bots/dealops/pages/channel:1")
    assert page.status_code == 200
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text
    assert "Fast Delete" in page.text and "Turn on" in page.text and "Turn off" in page.text
    requests = [r for r in fake_bot.requests if r.url.path == "/api/v1/pages/channel:1"]
    assert requests[0].headers["Authorization"] == "Bearer " + "t" * 40


def test_unknown_bot_and_missing_page(client, enrolled):
    log_in(client, enrolled[0])
    assert client.get("/bots/nope/pages/dashboard").status_code == 404
    assert "No such page." in client.get("/bots/dealops/pages/missing").text


def test_offline_bot(client, enrolled, fake_bot):
    log_in(client, enrolled[0])
    fake_bot.online = False
    home = client.get("/")
    assert "Offline" in home.text and "not reachable" in home.text
    assert "not reachable" in client.get("/bots/dealops/pages/dashboard").text


def test_dangerous_switch_asks_first_then_sends(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    csrf = log_in(client, enrolled[0])
    html = client.get("/bots/dealops/pages/channel:1").text
    token = action_token(client, html, "Turn on")

    confirm = client.post("/bots/dealops/act", data={"csrf": csrf, "action": token})
    assert "Fast Delete deletes posts" in confirm.text and 'name="confirmed" value="1"' in confirm.text
    assert "Fast Delete: Turn on" in confirm.text
    assert fake_bot.sent() == []

    done = client.post("/bots/dealops/act", data={"csrf": csrf, "action": token, "confirmed": "1"})
    assert "Saved." in done.text and done.headers["HX-Trigger"] == "page-refresh"
    [sent] = fake_bot.sent()
    assert sent.method == "PUT" and sent.url.path == "/api/v1/channels/1/features/stock_fast_delete"
    assert FakeBot.body(sent) == {"on": True}
    assert sent.headers["X-Actor"] == "owner" and len(sent.headers["X-Request-Id"]) == 36

    audit = client.get("/audit").text
    assert "stock_fast_delete" in audit and sent.headers["X-Request-Id"] in audit


def test_action_needs_csrf(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    log_in(client, enrolled[0])
    token = action_token(client, client.get("/bots/dealops/pages/channel:1").text, "Turn off")
    response = client.post("/bots/dealops/act", data={"csrf": "bad", "action": token}, follow_redirects=False)
    assert response.status_code == 303 and fake_bot.sent() == []


def test_forged_or_cross_bot_action_is_refused(client, enrolled, fake_bot, settings):
    csrf = log_in(client, enrolled[0])
    other_key = actions.sign(b"z" * 32, actions.action_from_block("dealops", {"method": "POST", "path": "/api/v1/x"}))
    response = client.post("/bots/dealops/act", data={"csrf": csrf, "action": other_key})
    assert response.status_code == 400

    key = client.app.state.hub.signing_key
    for_other_bot = actions.sign(key, actions.action_from_block("lootdeals", {"method": "POST", "path": "/api/v1/x"}))
    response = client.post("/bots/dealops/act", data={"csrf": csrf, "action": for_other_bot})
    assert response.status_code == 400 and fake_bot.sent() == []


def test_form_sends_typed_value(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    csrf = log_in(client, enrolled[0])
    token = action_token(client, client.get("/bots/dealops/pages/channel:1").text, "Save")
    client.post("/bots/dealops/act", data={"csrf": csrf, "action": token, "minutes": "45"})
    assert FakeBot.body(fake_bot.sent()[0]) == {"minutes": 45}


def test_bot_refusal_is_shown(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    fake_bot.reply = (409, {"error": {"code": "refused", "message": "Turn Fast Delete off first."}})
    csrf = log_in(client, enrolled[0])
    token = action_token(client, client.get("/bots/dealops/pages/channel:1").text, "Turn off")
    response = client.post("/bots/dealops/act", data={"csrf": csrf, "action": token})
    assert "Turn Fast Delete off first." in response.text and "flash bad" in response.text


def test_job_progress_and_lost_job(client, enrolled, fake_bot):
    fake_bot.pages["channel:1"] = SWITCH_PAGE
    fake_bot.reply = (202, {"job_id": "job-1", "kind": "cleanup"})
    csrf = log_in(client, enrolled[0])
    token = action_token(client, client.get("/bots/dealops/pages/channel:1").text, "Cleanup this channel")
    started = client.post("/bots/dealops/act", data={"csrf": csrf, "action": token})
    assert 'hx-get="/bots/dealops/jobs/job-1"' in started.text

    fake_bot.jobs["job-1"] = {"id": "job-1", "kind": "cleanup", "state": "running", "progress": {"done": 1, "total": 4}}
    running = client.get("/bots/dealops/jobs/job-1")
    assert "1 / 4" in running.text and 'value="25"' in running.text and "hx-get" in running.text

    fake_bot.jobs["job-1"] = {"id": "job-1", "state": "done", "result": {"message": "3 duplicates deleted"}}
    done = client.get("/bots/dealops/jobs/job-1")
    assert "3 duplicates deleted" in done.text and "hx-get" not in done.text
    assert done.headers["HX-Trigger"] == "page-refresh"

    del fake_bot.jobs["job-1"]
    lost = client.get("/bots/dealops/jobs/job-1")
    assert "bot restarted" in lost.text and "hx-get" not in lost.text


def test_security_page(client, enrolled):
    log_in(client, enrolled[0])
    page = client.get("/security")
    assert "(this one)" in page.text and "10 of 10 unused" in page.text
