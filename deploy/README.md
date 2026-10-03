# Deploying the hub

The hub is deployed the same hardened way as DealOps and the other CI bots on
the server (`E:/Claude_2/SERVER_DEPLOY_RULES.md`): a CI key that can only run
one root-owned script, which builds the delivered commit with a root-owned
Dockerfile and runs it with a root-owned compose file.

| File | Installed at | Owner / mode |
|---|---|---|
| `deploy.sh` | `/opt/dashboard-x-deploy/deploy.sh` | root:root 0755 |
| `Dockerfile` | `/opt/dashboard-x-deploy/Dockerfile` | root:root 0644 |
| `docker-compose.yml` | `/opt/dashboard-x-deploy/docker-compose.yml` | root:root 0644 |
| — | `/var/lib/dashboard-x-deploy/` (lock, VERSION) | deploy 0750 |
| — | `/home/deploy/Bots_web_dashboard/shared/.env` | deploy 0600 |
| — | `/home/deploy/Bots_web_dashboard/shared/data/` | 10001 0700 |
| — | `/home/deploy/Bots_web_dashboard/<bot>/config.toml` | deploy 0644 |

## One-time setup (owner, as root)

1. DNS: an A record `dashboard-x.l00t.link` → the server.
2. `sudo bash deploy/install.sh` from a checkout of this repository. It
   installs the three pinned files, makes the folders above and creates the
   internal network `hub_net` if it is missing. Check that the two lists of
   sha256 sums it prints match.
3. Fill `/home/deploy/Bots_web_dashboard/shared/.env` from `.env.example`
   (a fresh `HUB_SECRET_KEY`: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`).
4. `/home/deploy/Bots_web_dashboard/dealops/config.toml` from
   `bots/dealops/config.example.toml`.
5. The CI key: `ssh-keygen -t ed25519 -C ci-dashboard-x -f ci-dashboard-x`,
   add to `/home/deploy/.ssh/authorized_keys`:

   ```text
   command="/opt/dashboard-x-deploy/deploy.sh",restrict ssh-ed25519 AAAA... ci-dashboard-x
   ```

   and put the private key in the GitHub secret `DEPLOY_SSH_PRIVATE_KEY`
   (then delete the local copy).
6. Reverse proxy (pending the owner's choice, design §8 Q1). For a web server
   on the host, proxy `https://dashboard-x.l00t.link` to `http://127.0.0.1:8790`
   and **set** (not append) the forwarded headers, e.g. for nginx:

   ```nginx
   proxy_set_header X-Forwarded-For   $remote_addr;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header Host              $host;
   add_header Strict-Transport-Security "max-age=31536000" always;
   ```

7. After the first deploy, make the login inside the container:

   ```bash
   docker exec -it dashboard-x-hub python -m hub create-admin
   ```

   Then clear the terminal's scrollback.

## Checks after the first login

- Security page → the session's address is your real IP, not `172.31.250.1`.
  If it shows the gateway, the proxy is not sending `X-Forwarded-For` or the
  trusted address in `docker-compose.yml` (`FORWARDED_ALLOW_IPS`) is wrong.
- `docker exec dashboard-x-hub python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"`
  — with a proxy container (no `edge` bridge) this must fail: no egress.

## Admin commands

```bash
docker exec -it dashboard-x-hub python -m hub reset-password
docker exec -it dashboard-x-hub python -m hub reset-totp
docker exec -it dashboard-x-hub python -m hub revoke-sessions
docker exec -it dashboard-x-hub python -m hub backup
```

`hub.db` is copied daily to `shared/data/backups/` (7 kept). Copy them off
the server now and then: they hold the only admin login and the audit log.
