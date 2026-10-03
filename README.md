# dashboard-x

One login for the owner's Telegram bot dashboards, at
`https://dashboard-x.l00t.link`. The hub (this repository) holds the login,
sessions and audit log; each bot serves its own pages and actions over a
small internal API on the Docker network `hub_net`.

- Design: [docs/design.md](docs/design.md)
- Deploying and admin commands: [deploy/README.md](deploy/README.md)

## Layout

| Folder | What |
|---|---|
| `shared/hub/` | the hub, a FastAPI app (`python -m hub` for admin commands) |
| `shared/templates/`, `shared/static/` | pages and CSS; HTMX 2.0.11 self-hosted |
| `bots/<bot>/` | each bot's `config.example.toml` |
| `deploy/` | pinned Dockerfile, compose file and deploy script, installed by root |
| `tests/` | `pytest` |

## Develop

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check shared tests
```

Run it locally over plain http (from `shared/`):

```bash
HUB_SECRET_KEY=<48 random chars> HUB_PUBLIC_ORIGIN=http://localhost:8780 HUB_COOKIE_SECURE=0 \
HUB_DATA_DIR=../local/data HUB_BOTS_DIR=../local/config \
../.venv/Scripts/python -m uvicorn --factory hub.server:build --port 8780
```

and make the login with `python -m hub create-admin` under the same variables.

`htmx.min.js` is htmx 2.0.11 (sha256
`d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`, the same
from unpkg and jsDelivr).
