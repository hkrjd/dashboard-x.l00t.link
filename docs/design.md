# dashboard-x — design (draft 3, 2026-10-03)

A web dashboard at `https://dashboard-x.l00t.link` to see and control the
owner's Telegram bots from a browser. DealOps first; LootDeals, AffiliaterXBot
and later bots plug in the same way.

Draft 2 folds in the Fable 5.1 review of draft 1 (2026-10-03). Review items
are tagged in the text as `[B1]`…`[B6]` (blockers), `[S1]`…`[S11]`
(should-fix) and `[N1]`…`[N5]` (nice-to-have), so each change can be traced.

Draft 3 records what building the hub settled (tagged `[D3]`): bots describe
their pages as JSON blocks (§2a) so the hub needs no per-bot templates; the
per-username delay is capped at 30 s; the failed-login alert goes through a
bot's API because the hub has no internet access of its own.

## Decisions already made by the owner

| Topic | Decision |
|---|---|
| Kind | A separate website (not a Telegram Mini App) |
| Shape | One central hub + a small internal API inside each bot |
| Login | Username + password + TOTP (authenticator app) |
| Users | Only the owner, always (no roles) |
| DealOps scope | Everything the Telegram admin menu can do |
| Stack | Python, FastAPI, server-rendered pages with HTMX |
| Repo | github.com/hkrjd/dashboard-x.l00t.link |
| Folders | One place on the server, one folder per bot, shared parts in `shared/` (§5) |

## 1. Pieces

```
browser ──HTTPS──> reverse proxy (TLS) ──proxy net──> hub container (FastAPI)
                                                        │  "hub_net" (internal)
                                                        ├──> dealops  :8081  /api/v1/...
                                                        ├──> lootdeals:8081  /api/v1/...
                                                        └──> ...
```

- **Hub** (`dashboard-x` repo, own container): login, sessions, OTP, the bot
  list, and every page. It holds no bot data; it asks each bot's API.
- **Bot API** (inside each bot's own repo and container): a small HTTP server
  on the bot's own event loop (never a thread — DealOps' single SQLite
  connection is `check_same_thread=True`) `[S9]`. It exposes what the
  Telegram menu does, by calling the same service functions the Telegram
  handlers call — one code path for both, so the web can never do something
  the bot's own rules would refuse.
- **hub_net**: an external Docker network created once by root
  (`docker network create --internal hub_net`), joined by the hub and by each
  bot. Bots publish no port.
- **What "internal" really means** `[S5][S6]`: `internal: true` stops traffic
  on *that* network from leaving the server; it does not limit a container's
  other networks. So:
  - With a proxy container, the hub sits only on `hub_net` and on the
    proxy's internal network, and has no egress at all (it needs none: HTMX
    is served by the hub itself). With a web server on the host, the hub
    needs one ordinary bridge network (`edge`) to publish `127.0.0.1:8790`,
    and that bridge does give it egress — one more reason to prefer the
    container, but not a blocker `[D3]`.
  - A bot's API listens on `0.0.0.0:8081` inside the container, so it is also
    reachable from the bot's other networks (DealOps: `default`,
    `price_net`). The bearer token (§2) is what protects it; `ports:` is
    never added (the DealOps deploy-policy test already asserts that). An
    optional `HUB_API_BIND` env var can narrow the bind address later.
- **Reverse proxy**: terminates HTTPS for `dashboard-x.l00t.link` and forwards
  to the hub only. Owner to decide on the server: reuse the web server that
  serves amzan.store, or an nginx/Caddy container (FastForwarderXBot already
  has an nginx-container pattern). Either way `[B2]`:
  - the hub is never published on `0.0.0.0` — `127.0.0.1:<port>` for a host
    web server, or only a proxy network for a proxy container;
  - the proxy **overwrites** `X-Forwarded-For` / `X-Forwarded-Proto` (never
    appends to what the client sent);
  - the hub trusts those headers only from the proxy's address (uvicorn
    `--forwarded-allow-ips=<proxy ip>`), from nobody else.

## 2. Hub ↔ bot contract (the part every future bot implements)

- Base URL per bot from the hub's config: `http://dealops:8081`. Every bot's
  compose service is called `bot`, so that name is ambiguous on `hub_net`;
  each bot's pinned compose gives itself an explicit alias under `hub_net`
  (`aliases: [dealops]`) `[S7]`.
- Auth: a per-bot random token (32+ bytes), `Authorization: Bearer <token>`,
  compared with `hmac.compare_digest`. Hub: in `shared/.env` only; bot:
  `HUB_API_TOKEN` in the bot's `.env`. The bot API refuses every request
  without it, and does not start at all if the token is unset (the API is
  then simply off). If the API fails to start, the bot logs it and keeps
  running — a dashboard problem must never take the bot down `[S9]`.
- Versioned: `/api/v1/...`. JSON in and out.
- Every bot implements a common core:
  - `GET /api/v1/meta` → name, version (git sha), health, and the list of
    page ids it offers (`[{"id", "title"}]`, the first is the bot's home).
    The hub builds its menu from this `[N1]`.
  - `GET /api/v1/pages/{page_id}` → one page in the block format of §2a.
    `dashboard` is the summary page `[D3]`.
  - `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` → long-running actions (below).
  - `POST /api/v1/alerts {"text"}` → optional; the bot sends the text to its
    owner on Telegram. Only the bot named in `HUB_ALERT_BOT` needs it `[D3]`.
- Bot-specific endpoints below the core (DealOps list in §4).
- Every mutating call carries `X-Request-Id` and `X-Actor` (the hub user);
  the bot writes them to its own action log, like a Telegram admin action
  (`source: "web"`). The hub stores the same request id in its audit row so
  the two logs can be joined `[N4]`.
- **Idempotency** `[B3][S3]`:
  - Toggles and settings send the target state, never "flip". The service
    function returns "unchanged" (HTTP 200) when the target is already the
    current state, and does nothing else — in particular Fable's B3: a
    repeated `Fast Delete on` must not re-snapshot the part switches.
  - Actions that are not state-setting (start cleanup, start stock check,
    create backup, retry a delete) are de-duplicated by `X-Request-Id`: the
    bot keeps the ids it has seen for 10 minutes and answers a repeat with
    the first result.
- **Errors** `[S3]`: body `{"error": {"code": "...", "message": "..."}}`.
  401 bad/missing token · 404 unknown thing · 409 refused by a rule (e.g.
  "turn Fast Delete off first", or "a cleanup is already running") · 422
  bad input · 503 not available now (restore running, service not ready).
  The `message` is shown to the owner as-is, so it is written for a person.
- **Timeouts** `[S3]`: hub → bot 5 s for reads, 15 s for writes. A bot whose
  `meta` does not answer shows as "offline" on its card; the rest of the
  page still renders.
- **Jobs** `[B4][B5]`: a long-running action returns `202 {job_id}`. A job is
  `{id, kind, state: queued|running|done|failed, started_at, finished_at,
  progress: {done, total, current}, result | error}` — structured data, not
  Telegram text. One job per `kind` at a time per bot: a second start gets
  409 with the running job's id, whether the first was started from the web,
  Telegram or the scheduler. Finished jobs are kept in memory for 1 hour.
  Jobs are in-memory: after a bot restart `GET /jobs/{id}` is 404 and the hub
  says "the bot restarted; the outcome is unknown — check the logs page".
  The hub polls a running job every 2 s with HTMX.
- **Confirmation** `[N3]`: confirming a dangerous action is a hub page step
  only. The bot API trusts the token and does what it is told; it has no
  confirm tokens.

Adding a bot later = implement `meta` + `pages` + `jobs` + its own
mutating endpoints, join `hub_net` with an alias, add its folder with
`config.toml` next to `shared/` on the server and one token to `shared/.env`.
Nothing changes in the hub's code.

### 2a. Page format `[D3]`

A page is `{"title": "...", "blocks": [...]}`. The bot decides what is on
it; the hub only knows these block types and renders each the same way for
every bot. Every string is escaped; anything off-format is dropped.

| `type` | Fields | Renders as |
|---|---|---|
| `text` | `title?`, `text` | a paragraph (line breaks kept) |
| `cards` | `items: [{label, value, note?, tone?: good / warn / bad, page?}]` | number cards, optionally linking to a page |
| `table` | `columns`, `rows: [[cell…] or {cells, page?}]`, `empty?`, `pager?: {prev?, next?}` | a table; a row's first cell links to `page` |
| `switches` | `items: [{label, on, note?, turn_on?: action, turn_off?: action}]` | ON/OFF rows with one button for the opposite state |
| `actions` | `items: [action]` | buttons |
| `links` | `items: [{label, page, note?}]` | a list of links to other pages |
| `form` | `fields: [{name, label, kind: text / number / select, value?, options?, help?}]`, `submit: action` | inputs + a submit button |

An **action** is `{label, method: POST / PUT / PATCH / DELETE, path: "/api/v1/…",
body?, confirm?, danger?}`. The hub signs every action it renders (HMAC) and
only sends a correctly signed one, to the bot it came from, so a request to
the hub can replay what a bot offered but never reach another bot address.
`confirm` makes the hub ask on the page first; `danger` paints the button red.
A form's submit body is the signed `body` plus the declared fields (numbers
converted, nothing else accepted).

The answer to an action is either `200 {"message"}` (shown, then the page
reloads its blocks) or `202 {"job_id", "kind"}` (the hub shows the job's
progress until it ends), or an error from the error model above.

Page ids are `[a-z0-9][a-z0-9_.:-]*`, so a page can carry its own arguments
(`channel:-1001234`, `logs:-1001234:2`).

## 3. Login and security (hub)

Storage:
- One user in the hub's own SQLite: username, password hash (argon2id), TOTP
  secret (encrypted at rest with a key from `shared/.env`), the last accepted
  TOTP time-step, and 10 backup codes stored hashed, each single-use `[S1]`.

Admin CLI on the server (no sign-up page exists) `[S1]`:
- `create-admin` — asks for a password, prints the `otpauth://` URI and a
  text QR once, and the 10 backup codes once. Run it in a fresh shell and
  clear the scrollback afterwards.
- `reset-password`, `reset-totp` (new secret + new backup codes),
  `revoke-sessions`. A lost phone is fixed with `reset-totp`, not by editing
  the database.

Login flow `[S1]`:
1. Username + password. On success the hub creates a short-lived (5 min)
   server-side **pre-auth** session; nothing else is unlocked.
2. 6-digit TOTP (or one backup code). Accepted window ±1 step (±30 s); a
   time-step already used is refused (no replay). On success the pre-auth
   session is deleted and a brand-new session id is issued (no session
   fixation). 5 wrong codes kill the pre-auth session; the user starts again.

Brute force `[B1][B2]` — never a hard lock on the username, because on a
one-user site that would let anyone lock the owner out:
- Per IP (the real client IP, §1): 10 failed attempts in 15 min → that IP is
  blocked for 15 min.
- Per username: growing delay after each failure (1 s, 2 s, 4 s … capped at
  30 s, under the proxy's usual 60 s timeout `[D3]`), no lock. The delay is
  served before the password is checked, and the check still happens, so
  the owner's right password always gets in.
- After 5 failures in a row the owner gets a Telegram message, at most once
  an hour. The hub has no internet access, so it asks a bot to send it
  (`POST /api/v1/alerts` on the bot named by `HUB_ALERT_BOT`; off if unset)
  `[D3]`.
- Every attempt is logged (time, IP, user agent, step, result).

Sessions:
- Random 256-bit id in a `__Host-session` cookie: `HttpOnly; Secure;
  SameSite=Strict; Path=/`. Server-side rows; 12 h idle / 7 days max.
- "Log out everywhere" on the page and as a CLI command. Changing the
  password or the TOTP secret also ends every session.
- Authenticated pages are sent with `Cache-Control: no-store`.

Browser hardening `[S2]`:
- CSRF token on every POST/PUT/DELETE (HTMX sends it as a header), compared
  with `hmac.compare_digest`; and the `Origin` / `Sec-Fetch-Site` header must
  say the request came from the site itself.
- CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src
  'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none';
  base-uri 'none'`. No inline scripts or styles.
- HTMX self-hosted with `allowEval=false`, `allowScriptTags=false`,
  `selfRequestsOnly=true` (set from a static JS file, not inline).
- Also `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  HSTS (from the proxy).

Audit log in the hub: when, IP, what, which bot, request id, result.

Dangerous actions (cleanup all, delete-capable switches like Fast Delete,
removing a channel) ask for a second confirm on the page, as the Telegram
menu does.

Backups:
- Restore stays Telegram/server only in v1 (uploading a database through the
  web is a large attack surface); create/list backups is fine.
- The hub backs up its own `hub.db` once a day with SQLite's online backup
  into `shared/data/backups/`, keeping 7 `[S10]`. It holds the only admin
  login and the audit log; the owner copies it off the server now and then.

## 4. DealOps API (v1 = everything the Telegram menu does)

Rules for the DealOps side:
- aiohttp server (one new, exact-pinned dependency) started in the bot's
  `_post_init` and stopped in `_post_shutdown` (`dispatcher.py`) `[S9]`.
- Every request reads the connection and services from
  `application.bot_data` at request time, never cached at startup: a
  Telegram restore swaps the connection and rebuilds every service. While
  the restore gate is set the API answers 503 `[B6]`.
- Cleanup gets one lock (as Stock Check already has): a second caller —
  Telegram, web or the scheduler — gets "already running" `[B4]`.

Read — each is a page in the §2a format, built from the same data
functions the Telegram views use `[D3]`:
- dashboard (today's counts, stock check, API calls, AI) — same numbers as
  `dashboard_view.py`
- channels: list with state (active/paused/pending removal), labels
- per channel: settings panel, status, feature controls (19 switches with
  configured/effective/inactive reason), health, logs (filters, pages)
- API calls page (today, yesterday, 7 days, months, limits)
- stock check: last report, Fast Delete rows, Only Notify reports with votes
  and comments
- lists that the Telegram buttons hang off notification messages `[S4]`:
  pending duplicate reviews, failed deletes, AI feedback items
- backups list; AI decision pages (usage, rules)

Write (each through the same service code the Telegram callbacks use):
- feature switch per channel, as target state (incl. Fast Delete's
  snapshot/restore, no-op when already in that state, and the "turn Fast
  Delete off first" refusal as 409)
- duplicate time limit (global and per channel), auto-delete, pause/resume
- Cleanup all channels / one channel → job
- Stock Check now → job
- Create backup → job
- channels: add (also restores a channel pending removal), rename, remove
  (scheduled purge, as today)
- Amazon daily limit
- duplicate review decisions, retry failed delete
- Only Notify votes and comments
- AI: master toggle, rule toggles/limits/confidence, test connection, feedback

Prerequisite refactor in DealOps `[S4]`: several actions live inside
Telegram handlers today. They move into plain service functions that take an
`actor` (who did it, and `source: telegram|web`) instead of the Telegram
`query`, and both the Telegram callback and the API call them. Where the
same logic already exists twice it is merged into one, not copied a third
time:
- `callbacks._set_feature_configured` and `handlers_admin._set_monitoring_state`
- `callbacks._update_all_duplicate_windows` and its copy in `handlers_admin`

Order inside DealOps (lowest risk first) `[Fable feasibility]`:
1. API skeleton (token, errors, `meta`, `dashboard`, `jobs`) + read endpoints
   — these already have data-returning functions.
2. One feature service for switches, window, pause/resume (merging the
   duplicates above), Fast Delete with the B3 no-op.
3. Jobs with locks: cleanup, stock check, backup.
4. Channels add/rename/remove, health, retry, reviews, Only Notify.
5. AI settings and feedback (the largest move: `handlers_ai_decision.py`).

Deferred to v1.1: `/combine` (rare; needs a dry-run page).

Telegram's own Cleanup button today waits for the cleanup inside the
handler, which holds up every channel post for the whole run. Once the
cleanup job exists, the Telegram button uses it too and returns at once,
showing progress the way Stock Check already does. This changes how the
live bot behaves — **owner to confirm** (open question 3).

## 5. Folder layout (owner's rule, 2026-10-03: one place, one folder per bot, clean)

On the server, everything of the dashboard lives under one folder; each
bot's part in a folder named after the bot; what all bots share in `shared`:

```
/home/deploy/Bots_web_dashboard/
├── shared/                  hub: what every bot dashboard uses
│   ├── .env                 hub secrets (session key, OTP key, bot tokens) mode 600
│   └── data/                hub.db (admin, sessions, audit log), backups/  (owned by uid 10001)
├── dealops/                 DealOps' part of the dashboard
│   └── config.toml          display name, API URL; no secrets
└── <next-bot>/              same shape for every later bot
```

Logs go to `docker logs`; the container's filesystem is read-only.

The repo mirrors it, so a file's place on the server and in git is obvious:

```
dashboard-x.l00t.link/            (git repo)
├── shared/                        the hub
│   ├── hub/                       the Python package: app, admin, sessions, audit, bots, actions, pages, cli
│   ├── templates/                 base layout, login, OTP, generic bot pages
│   └── static/                    css, htmx.min.js, htmx-config.js
├── bots/
│   └── dealops/                   config.example.toml (pages come from the bot itself, §2a)
├── deploy/                        Dockerfile, pinned compose, deploy.sh, install.sh, README (installed by root to /opt)
├── tests/
│   ├── shared/
│   └── bots/dealops/
├── docs/                          this design, runbook
├── README.md · pyproject.toml · .gitignore · .env.example
```

The Python package is named `hub` and lives in `shared/hub/` `[N2]`, so the
admin commands are `python -m hub create-admin` etc. with `shared/` on the
path; the repo folder keeps the owner's name `shared/`.

Rules: no file at the top that belongs in a folder; one bot = one folder in
`bots/` and one on the server; secrets only in `shared/.env` (never in a
bot folder, never in git); names lowercase, the bot's own name
(`dealops`, `lootdeals`); the DealOps-side API lives in the DealOps repo
(`dealops/app/web_api/`), not here.

## 6. Deployment

- Hub: its own image and pinned compose under `/opt/dashboard-x-deploy`,
  same hardened pattern as DealOps and SERVER_DEPLOY_RULES: forced-command
  CI key `ci-dashboard-x`, root-owned files, read-only container, non-root
  uid 10001, `cap_drop: ALL`, `no-new-privileges`, memory ~192 MB, healthcheck,
  rollback if unhealthy. `deploy/` carries `install.sh` and a README as the
  rules ask `[S11]`.
- DealOps: the pinned compose gains `hub_net` (with the `dealops` alias) and
  `HUB_API_TOKEN` in `/home/deploy/dealops/.env` `[S8]`. The deploy-policy
  test (`tests/test_deploy_policy.py`, which today allows only `default` +
  `price_net`) changes in the same commit as `ops/dealops-deploy/`, and the
  owner installs the new compose as root before that commit is pushed, so
  the live file never drifts from the repo. The token takes effect on the
  next deploy (force-recreate).
- RAM estimate: hub ~80–120 MB; each bot API ~15–30 MB on top of the bot.

Root / owner work, in order `[S11]`:
1. DNS: `dashboard-x.l00t.link` A record → the server.
2. `docker network create --internal hub_net`.
3. `/home/deploy/Bots_web_dashboard/{shared/data,dealops}`; `shared/data`
   owned by uid 10001; `shared/.env` mode 600.
4. `/opt/dashboard-x-deploy/` (deploy.sh, Dockerfile, docker-compose.yml)
   root:root via `install.sh`; `/var/lib/dashboard-x-deploy/`.
5. `authorized_keys` line `command="/opt/dashboard-x-deploy/deploy.sh",restrict … ci-dashboard-x`;
   GitHub secret `DEPLOY_SSH_KEY`.
6. Reverse proxy vhost + certificate.
7. DealOps pinned compose with `hub_net`; `HUB_API_TOKEN` in DealOps `.env`.
8. `python -m hub create-admin` inside the hub container.

## 7. Order of work

1. This design, reviewed (Fable, done) and agreed with the owner.
2. Hub: skeleton, login + TOTP + sessions + lockout + audit + admin CLI,
   generic bot pages. Tests. (Changes nothing on the live bot.) — **done
   2026-10-03**, with the deploy files in `deploy/`.
3. DealOps: service-function moves and the API in the order of §4, off
   unless `HUB_API_TOKEN` is set; tests. Deployable on its own, changes
   nothing for Telegram (except the Cleanup button, if the owner agrees). — **steps 1–3 of §4
   done and deployed 2026-10-03** (DealOps `23f2c3d`): API skeleton, the
   six menu pages, FeatureService (switches, B3), time limits, channels
   add/rename/remove, health check, Amazon limit, backup (synchronous, it
   takes under a second), Cleanup and Stock Check as jobs with the B4 lock;
   the Telegram Cleanup button now runs as a job (owner: yes). Still to do:
   reviews, failed deletes, Only Notify, AI settings and feedback (owner:
   AI in v1).
4. Hub: DealOps pages.
5. Server (with the owner, root): the list in §6.
6. Live: read-only pages first, then switches and buttons.

## Appendix A. Telegram menu → DealOps API (inventory from the code, 2026-10-03)

Where each action lives today (`dealops/app/bot/…`) and the endpoint it
becomes. "Move" = the logic sits inside a Telegram handler and moves into a
service function both use. All paths below are under `/api/v1`. Since draft 3
the `GET` rows are served as pages (§2a), e.g. `GET /channels/{id}` is page
`channel:{id}`; the mutating rows stay as listed.

| Telegram (callback / command) | Today | API v1 | Move? |
|---|---|---|---|
| Dashboard `menu:main`, `/start` | `dashboard_view.dashboard_text` | `GET /dashboard` | split numbers from text |
| Channels `menu:channels`, `settings:channels` | `callbacks._show_channels_menu` | `GET /channels` | yes |
| Add channel `menu:add_channel` + text input | `handlers_admin._handle_pending_add_channel`; restores a pending removal via `ChannelRemovalService.add_or_restore` | `POST /channels` | yes |
| Rename `/renamechannel` | `handlers_admin` | `PATCH /channels/{id}` | yes |
| Remove `channel:…:remove` → confirm | `callbacks._handle_channel_callback`, removal service | `DELETE /channels/{id}` | small |
| Settings panel / status `settings:{id}:panel\|status` | `_settings_panel_text`, `status_text` | `GET /channels/{id}` | split data from text |
| Check Health `health:{id}` | `channel_health_service.check` | `POST /channels/{id}/health-check` | no |
| Feature Controls `feature:{id}:menu\|toggle` | `_handle_feature_callback`, `_toggle_fast_delete` (takes `query` only for the user id) | `GET /channels/{id}/features`, `PUT /channels/{id}/features/{key}` | yes (Fast Delete snapshot, B3 no-op) |
| Window per channel / global `settings:…:window:*` | `_handle_settings_callback`, `_update_all_duplicate_windows` (twice) | `PUT /channels/{id}/window`, `PUT /settings/window` | yes, merge copies |
| Auto-delete / pause `settings:…:auto_delete\|pause` | `_set_feature_configured` | `PUT /channels/{id}/features/…` | yes |
| `/pause`, `/resume` | `handlers_admin._set_monitoring_state` (copy of `_set_feature_configured`) | `PUT /channels/{id}/features/monitoring` | yes, merge copies |
| Logs `logs:…` | `ActionLogRepository.page` | `GET /channels/{id}/logs` | no |
| Cleanup one `settings:{id}:cleanup` | `cleanup_service.run_channel` | `POST /channels/{id}/cleanup` → job | job + lock |
| Cleanup all `menu:cleanup_all_run` | `cleanup_service.run_all_channels` + progress, awaited in the handler | `POST /cleanup` → job | job + lock |
| Stock Check `menu:stock_check_run` | `_start_stock_check` (background task + lock + progress) | `POST /stock-check` → job | yes |
| API Calls `menu:api_calls`, `/api_calls` | `api_calls_view.api_calls_page` | `GET /api-calls` | split |
| Amazon limit `menu:api_limit` + input | validation in `handlers_admin`; `stock_repository.set_daily_limit` | `PUT /api-calls/limits/amazon` | small |
| Backup `backup:*` | `backup_service` | `GET /backups`, `POST /backups` → job (restore: Telegram only) | no |
| Retry delete `retry:{id}` | `DeletionService.retry_failed` | `GET /failed-deletes`, `POST /failed-deletes/{id}/retry` | no |
| Duplicate review buttons `review:{id}:…` | `_handle_review_callback` | `GET /reviews?status=pending`, `POST /reviews/{id}` | yes |
| Only Notify votes `snv:{id}:…` + comment | `handlers_stock_review`; `StockReviewRepository.comment` | `PUT /review-notes/{id}` | no (comment flow is Telegram glue only) |
| AI Decision `ai:*`, `air:*` | `handlers_ai_decision` (`_apply_rule_value`, `_audit_ai_setting`, `_cancel_ai_actions_if_off`, `_next_rule_value`) | `GET/PUT /ai/...` | yes — the largest move |
| AI feedback `aif:*` | `handlers_ai_feedback` | `GET /ai/feedback`, `PUT /ai/feedback/{id}` | small |
| `/combine` | `post_combine_service` | v1.1 | — |
| Help pages | `help_content` | not needed (hub has its own help) | — |

Removed from draft 1: `cancel_remove` → `POST /channels/{id}/cancel-remove`.
The Telegram "cancel" button only redraws the settings panel; it does not
undo a removal. Re-adding a channel before its purge is what restores it.

## 8. Open questions for the owner

1. ~~Reverse proxy~~ — decided 2026-10-03: the nginx already on the host
   (it holds port 443 for the shortener sites) gets one more site file for
   dashboard-x; the hub publishes `127.0.0.1:8790`. Waiting for the owner's
   `nginx -T` listing to check for clashes.
2. ~~Login alert~~ — yes (default kept).
3. ~~Cleanup button as a job~~ — owner: yes, done 2026-10-03.
4. ~~AI pages~~ — owner: in v1 ("abhi").
5. Web sessions end when the password changes — yes by default.
6. Any action that must stay Telegram-only? (Draft: restore from backup.)
