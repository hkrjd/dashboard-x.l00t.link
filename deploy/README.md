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
6. Reverse proxy: the nginx already on the host (owner's choice,
   2026-10-03), with the files in `deploy/nginx/`. Once the DNS record
   answers (`dig +short dashboard-x.l00t.link` shows the server):

   ```bash
   ss -ltn | grep -q ':8790 ' && echo "8790 is taken -- stop" || echo "8790 free"
   certbot certonly --nginx -d dashboard-x.l00t.link
   install -o root -g root -m 0644 deploy/nginx/dashboard-x-proxy.conf /etc/nginx/snippets/dashboard-x-proxy.conf
   install -o root -g root -m 0644 deploy/nginx/dashboard-x.l00t.link.conf /etc/nginx/sites-available/dashboard-x.l00t.link
   ln -s /etc/nginx/sites-available/dashboard-x.l00t.link /etc/nginx/sites-enabled/dashboard-x.l00t.link
   nginx -t && systemctl reload nginx
   ```

   `nginx -t` must pass before the reload; if it fails, remove the link in
   sites-enabled and nothing else is affected. The site sets (never
   appends) `X-Forwarded-For`, and rate-limits the two login posts.

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
