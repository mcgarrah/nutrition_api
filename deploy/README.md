# deploy/

Deployment assets for running the Nutrition API on a plain Linux host
(bare metal, VM, or LXC container) with systemd.

For the container/PaaS path instead, see the `Dockerfile` and `.do/app.yaml`
in the repository root.

## systemd

`nutrition-api.service` runs uvicorn with two workers on port 8080, restarts on
failure, and logs to the journal.

It assumes the layout this repository is normally deployed with:

| Path | Purpose |
| :--- | :--- |
| `/opt/nutrition_api` | repository checkout |
| `/opt/nutrition_api/.venv` | virtualenv with `requirements.txt` installed |
| `/opt/nutrition_api/.env` | supplies `FDC_API_KEY` (gitignored — never commit it) |
| `/opt/nutrition_api/data` | GPC SQLite cache; the only writable path |
| `mcgarrah` | the user/group the service runs as |

If your checkout, user, or port differ, edit those fields before installing —
`WorkingDirectory`, `EnvironmentFile`, `ExecStart`, `ReadWritePaths`, `User`,
and `Group`.

### Install

```bash
sudo install -m 644 deploy/nutrition-api.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/nutrition-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now nutrition-api.service
```

### Operate

```bash
sudo systemctl status nutrition-api      # current state
sudo journalctl -u nutrition-api -f      # follow logs
sudo systemctl restart nutrition-api     # pick up new code after a git pull
```

Once running: the lookup tester is at `http://<host>:8080/`, the OpenAPI docs at
`/docs`, and `/api/v1/health` reports per-source status (it returns `degraded`,
not an error, when an upstream is unreachable).

### Hardening notes

The unit runs with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, and
`ProtectHome=read-only`. `data/` is granted back via `ReadWritePaths` because
the app self-updates the GS1 GPC taxonomy there on startup — dropping that line
makes the taxonomy update fail silently, so keep it if you move the data
directory.

## Caddy (public front — TLS + landing page)

`Caddyfile` puts a [Caddy](https://caddyserver.com/) reverse proxy in front of
the uvicorn service. Caddy terminates TLS with an automatically-provisioned
certificate, serves a static landing hub at the root (`site/index.html`), and
proxies the application and API paths to the backend on `127.0.0.1:8080`:

| Path | Served by |
| :--- | :--- |
| `/` | the static landing hub (Caddy, from `site/`) |
| `/status` | the status dashboard (Caddy, from `site/`) |
| `/caddy-health` | Caddy itself (proxy liveness) |
| `/ui/` | lookup tester (backend) |
| `/gpc` | GPC browser (backend) |
| `/docs`, `/redoc`, `/openapi.json` | API docs (backend) |
| `/api/*` | JSON API (backend) |

The landing page and the status dashboard are served by Caddy itself, so they
stay up — and the dashboard reports the outage — even while the backend is
restarting. The routing lives in `caddy/site.caddy` and is imported by every
site block (the example `Caddyfile`, its `:8081` test block, and the installed
`/etc/caddy/Caddyfile`), so they cannot drift apart.

### Status dashboard

`/status` is a live health page grouping every moving part: the Caddy proxy
(checked directly via `/caddy-health`), the API backend, the on-disk cached
copies (USDA FDC, Open Food Facts, GPC, the response store) with their dataset
dates, and the external upstream APIs. It reads `/api/v1/health` through the
proxy and refreshes every 20 s. Because Caddy serves the page itself, a backend
outage shows as a red tile rather than an unreachable page.

### Install (Debian/Ubuntu, via apt)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

The package installs `caddy.service`, which reads `/etc/caddy/Caddyfile`.

### Configure

Point `/etc/caddy/Caddyfile` at the shared snippet and pick how it listens.

**By IP over plain HTTP** (a LAN box, no domain) — what this deployment uses:

```
{
	auto_https off
}
import /opt/nutrition_api/deploy/caddy/site.caddy
:80 {
	import nutrition_api
}
```

**By domain with automatic HTTPS** (public host that resolves to this machine,
ports 80/443 reachable): drop `auto_https off` and replace `:80` with your
hostname — Caddy obtains and renews the certificate itself.

If the checkout is not at `/opt/nutrition_api`, set `NUTRITION_SITE_ROOT` (or
edit the `root` in `caddy/site.caddy`) and fix the `import` path.

### Verify, then run

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy          # or restart

# Local smoke test straight from the repo — serves on http://localhost:8081/
caddy run --config deploy/Caddyfile
```

The `:8081` block in the repo `Caddyfile` shares the same snippet as the
installed config, so what you smoke-test is what you deploy.

Caddy and the `nutrition-api` service are independent — restart or update the
backend without touching Caddy, and the landing page and status dashboard never
go down with it.
