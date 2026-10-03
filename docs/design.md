# dashboard-x — design (draft 1, 2026-10-03)

A web dashboard at `https://dashboard-x.l00t.link` to see and control the
owner's Telegram bots from a browser. DealOps first; LootDeals, AffiliaterXBot
and later bots plug in the same way.

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

## 1. Pieces

```
browser ──HTTPS──> reverse proxy (TLS) ──> hub container (FastAPI)
                                              │  internal Docker network "hub_net"
                                              │  (internal: true, no route out)
                                              ├──> dealops-bot   :8081  /api/v1/...
                                              ├──> lootdeals-bot :8081  /api/v1/...
                                              └──> ...
```

- **Hub** (`dashboard-x` repo, own container): login, sessions, OTP, the bot
  list, and every page. It holds no bot data; it asks each bot's API.
- **Bot API** (inside each bot's own repo and container): a small HTTP server
  on the bot's existing event loop, listening only on `hub_net`. It exposes
  what the Telegram menu does, by calling the same service functions the
  Telegram handlers call -- one code path for both, so the web can never do
  something the bot's own rules would refuse.
- **hub_net**: an external Docker network created once by root
  (`docker network create --internal hub_net`), joined by the hub and by each
  bot. Bots publish no port; nothing of a bot is reachable from the internet.
- **Reverse proxy**: terminates HTTPS for `dashboard-x.l00t.link` and forwards
  to the hub only. To be decided on the server: reuse the web server already
  serving amzan.store, or a Caddy container with automatic certificates.

## 2. Hub ↔ bot contract (the part every future bot implements)

- Base URL per bot from the hub's config: `http://dealops-bot:8081`.
- Auth: a per-bot random token, `Authorization: Bearer <token>`, set in both
  `.env` files (hub: `BOT_TOKENS_JSON` or one var per bot; bot:
  `HUB_API_TOKEN`). The bot API refuses every request without it, and refuses
  to start listening if the token is unset (the API is then simply off).
- Versioned: `/api/v1/...`. JSON in and out.
- Every bot implements a common core:
  - `GET /api/v1/meta` → name, version (git sha), health, the pages it offers
    (so the hub builds its menu from the bot, not from hard-coded lists).
  - `GET /api/v1/dashboard` → the summary cards.
- Bot-specific endpoints below the core (DealOps list in §4).
- Every mutating call carries `X-Request-Id` and `X-Actor` (the hub user);
  the bot writes it to its own action log, like a Telegram admin action
  (`source: "web"`).
- Mutations are idempotent where they can be (a toggle sends the target state,
  not "flip"), so a double click or a retry never undoes itself.

Adding a bot later = implement `meta` + `dashboard` + its own endpoints, join
`hub_net`, add one entry (name, URL, token) to the hub config. The hub renders
generic pages (cards, tables, toggle lists, action buttons with confirm) from
what `meta` describes; a bot that needs a special page gets a hub template.

## 3. Login and security (hub)

- One user, stored in the hub's own SQLite: username, password hash
  (argon2id), TOTP secret (encrypted at rest with a key from `.env`).
- First run: a CLI command on the server (`python -m hub create-admin`)
  prints the OTP QR as text/URI once; no sign-up page exists.
- Login = password, then 6-digit TOTP. 10 backup codes, shown once.
- Sessions: random id in an `HttpOnly; Secure; SameSite=Strict` cookie,
  server-side, 12 h idle / 7 days max, revocable ("log out everywhere").
- Lockout: 5 failed attempts → 15 min lock per username and per IP;
  every login attempt logged.
- CSRF token on every form/HTMX POST; strict Content-Security-Policy; no
  third-party scripts (HTMX served from the hub itself).
- Audit log in the hub: who, when, what, which bot, result.
- Dangerous actions (cleanup all, delete-capable switches like Fast Delete,
  removing a channel, restore) ask for a second confirm on the page, as the
  Telegram menu does.
- Backups: restore stays Telegram/server only in v1 (uploading a database
  through the web is a large attack surface); create/list backups is fine.

## 4. DealOps API (v1 = everything the Telegram menu does)

Read:
- dashboard (today's counts, stock check, API calls, AI) — same numbers as
  `dashboard_view.py`
- channels: list with state (active/paused/pending), labels
- per channel: settings panel, status, feature controls (19 switches with
  configured/effective/inactive reason), health, logs (filters, pages)
- API calls page (today, yesterday, 7 days, months, limits)
- stock check: last report, Fast Delete rows, Only Notify reports with votes
  and comments
- backups list; AI decision pages (usage, rules)

Write (each through the same service code the Telegram callbacks use):
- feature toggle per channel (incl. Fast Delete's snapshot/restore and the
  "turn Fast Delete off first" refusal)
- duplicate time limit (global and per channel), auto-delete, pause/resume
- Cleanup all channels / one channel (long-running → job with progress)
- Stock Check now (long-running → job with progress)
- Create backup
- channels: add, rename, remove (scheduled purge, as today)
- Amazon daily limit
- Only Notify votes and comments
- AI: master toggle, rule toggles/limits/confidence, test connection

Long-running actions return a job id; the hub polls `GET /api/v1/jobs/<id>`
(HTMX every 2 s) and shows the same progress the Telegram message shows.

Prerequisite refactor in DealOps: several actions live inside
`callbacks.py` handlers today (e.g. `_toggle_fast_delete`, cleanup progress).
They move into plain service functions that both the Telegram callback and
the API call, so the two can never drift.

## 5. Folder layout (owner's rule, 2026-10-03: one place, one folder per bot, clean)

On the server, everything of the dashboard lives under one folder; each
bot's part in a folder named after the bot; what all bots share in `shared`:

```
/home/deploy/Bots_web_dashboard/
├── shared/                  hub: what every bot dashboard uses
│   ├── .env                 hub secrets (session key, OTP key, bot tokens) mode 600
│   ├── data/                hub.db (admin, sessions, audit log), backups/
│   └── logs/                (only if the container writes files; else docker logs)
├── dealops/                 DealOps' part of the dashboard
│   └── config.toml          display name, API URL, which pages; no secrets
└── <next-bot>/              same shape for every later bot
```

The repo mirrors it, so a file's place on the server and in git is obvious:

```
dashboard-x.l00t.link/            (git repo)
├── shared/                        the hub
│   ├── app/                       FastAPI app: auth/, sessions/, audit/, bots/ (client + registry)
│   ├── templates/                 base layout, login, OTP, generic bot pages
│   └── static/                    css, htmx.min.js (served by the hub itself)
├── bots/
│   └── dealops/                   DealOps pages: templates/ + views.py + config.example.toml
├── deploy/                        Dockerfile, pinned compose, deploy.sh (installed by root to /opt)
├── tests/
│   ├── shared/
│   └── bots/dealops/
├── docs/                          this design, runbook
├── README.md · DEPLOYMENT.md · pyproject.toml · .gitignore · .env.example
```

Rules: no file at the top that belongs in a folder; one bot = one folder in
`bots/` and one on the server; secrets only in `shared/.env` (never in a
bot folder, never in git); names lowercase, the bot's own name
(`dealops`, `lootdeals`); the DealOps-side API lives in the DealOps repo
(`dealops/app/web_api/`), not here.

## 6. Deployment

- Hub: its own image and pinned compose under `/opt/dashboard-x-deploy`
  (same hardened pattern as DealOps: forced-command CI key, root-owned
  compose, read-only container, non-root user, memory limit ~192 MB).
- DealOps: the pinned compose gains `hub_net` (second extra network; the
  deploy-policy test is extended) and an env var for the API token. Root work.
- DNS: `dashboard-x.l00t.link` A record → the server. Owner.
- Reverse proxy + certificate. Root work, with the owner.
- RAM estimate: hub ~80–120 MB; each bot API ~15–30 MB on top of the bot.

## 7. Order of work

1. This design, reviewed (Fable) and agreed with the owner.
2. DealOps: move menu actions into service functions; add the API (off
   unless `HUB_API_TOKEN` is set); tests. Deployable on its own, changes
   nothing for Telegram.
3. Hub: skeleton, login + TOTP + sessions + lockout + audit; the generic bot
   pages; DealOps pages. Tests.
4. Server (with the owner, root): DNS, `hub_net`, reverse proxy + HTTPS, hub
   deploy, DealOps compose update.
5. Live: read-only pages first, then switches and buttons.

## Appendix A. Telegram menu → DealOps API (inventory from the code, 2026-10-03)

Where each action lives today (`dealops/app/bot/…`) and the endpoint it
becomes. "Move" = the logic sits inside a Telegram handler and moves into a
service function both use.

| Telegram (callback / command) | Today | API v1 | Move? |
|---|---|---|---|
| Dashboard `menu:main`, `/start` | `dashboard_view.dashboard_text` | `GET /dashboard` | split numbers from text |
| Channels `menu:channels`, `settings:channels` | `callbacks._show_channels_menu` | `GET /channels` | yes |
| Add channel `menu:add_channel` + text input | `handlers_admin._handle_pending_add_channel` | `POST /channels` | yes |
| Rename `/renamechannel` | `handlers_admin` | `PATCH /channels/{id}` | yes |
| Remove `channel:…:remove` → confirm | `callbacks._handle_channel_callback`, removal service | `DELETE /channels/{id}` (confirm token) | small |
| Cancel remove `cancel_remove` | same | `POST /channels/{id}/cancel-remove` | small |
| Settings panel / status `settings:{id}:panel|status` | `_settings_panel_text`, `status_text` | `GET /channels/{id}` | split data from text |
| Check Health `health:{id}` | `channel_health_service.check` | `POST /channels/{id}/health-check` | no |
| Feature Controls `feature:{id}:menu|toggle` | `_handle_feature_callback`, `_toggle_fast_delete` | `GET/PUT /channels/{id}/features/{key}` | yes (Fast Delete snapshot logic) |
| Window per channel / global `settings:…:window:*` | `_handle_settings_callback`, `_update_all_duplicate_windows` | `PUT /channels/{id}/window`, `PUT /settings/window` | yes |
| Auto-delete / pause `settings:…:auto_delete|pause` | `_set_feature_configured` | `PUT /channels/{id}/features/…` | yes |
| Logs `logs:…` | `ActionLogRepository.page` | `GET /channels/{id}/logs` | no |
| Cleanup one `settings:{id}:cleanup` | `cleanup_service.run_channel` | `POST /channels/{id}/cleanup` → job | job wrapper |
| Cleanup all `menu:cleanup_all_run` | `cleanup_service.run_all_channels` + progress | `POST /cleanup` → job | job wrapper |
| Stock Check `menu:stock_check_run` | `_start_stock_check` (background task + progress) | `POST /stock-check` → job | yes |
| API Calls `menu:api_calls`, `/api_calls` | `api_calls_view.api_calls_page` | `GET /api-calls` | split |
| Amazon limit `menu:api_limit` + input | `set_daily_limit` | `PUT /api-calls/limits/amazon` | no |
| Backup `backup:*` | `backup_service` | `GET /backups`, `POST /backups` (restore: Telegram only) | no |
| Retry delete `retry:{id}` | `DeletionService.retry_failed` | `POST /failed-deletes/{id}/retry` | no |
| Duplicate review buttons `review:{id}:…` | `_handle_review_callback` | `POST /reviews/{id}` | yes |
| Only Notify votes `snv:{id}:…` + comment | `handlers_stock_review` | `PUT /review-notes/{id}` | small |
| AI Decision `ai:*`, `air:*` | `handlers_ai_decision` | `GET/PUT /ai/...` | yes |
| AI feedback `aif:*` | `handlers_ai_feedback` | `PUT /ai/feedback/{id}` | small |
| `/combine` | `post_combine_service` | `POST /combine` (dry-run flag) | no |
| `/pause`, `/resume` | `_set_monitoring_state` | `PUT /channels/{id}/features/monitoring` | yes |
| Help pages | `help_content` | not needed (hub has its own help) | — |

## 8. Open questions for the owner

- Reverse proxy: reuse the amzan.store web server, or a separate Caddy?
  (Needs a look at the server.)
- Should web sessions also log out when the owner changes the password? (Yes
  by default.)
- Any action that must stay Telegram-only? (Draft: restore from backup.)
