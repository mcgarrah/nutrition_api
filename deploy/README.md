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
| `/ui/` | lookup tester (backend) |
| `/gpc` | GPC browser (backend) |
| `/docs`, `/redoc`, `/openapi.json` | API docs (backend) |
| `/api/*` | JSON API (backend) |

The landing page is served by Caddy itself, so the front page stays up even
while the backend is restarting.

### Set your domain

Edit the `nutrition.example.org` block in `Caddyfile` to your real hostname —
Caddy obtains and renews the certificate for it automatically (the host must
resolve to this machine and ports 80/443 must be reachable). If the checkout is
not at `/opt/nutrition_api`, set `NUTRITION_SITE_ROOT` or edit the `root`
directive.

### Verify, then run

```bash
caddy validate --config deploy/Caddyfile
caddy fmt --overwrite deploy/Caddyfile       # optional: canonical formatting

# Local smoke test — no domain, no TLS — serves on http://localhost:8081/
caddy run --config deploy/Caddyfile
```

The `:8081` block at the bottom of the `Caddyfile` is that local test listener;
it shares one config snippet with the production block, so what you test is what
you deploy.

### As a service

Run Caddy from its own systemd unit (the distro's `caddy` package installs
`caddy.service` and reads `/etc/caddy/Caddyfile`):

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # with your domain set
sudo cp -r deploy/site /opt/nutrition_api/deploy/site
sudo systemctl reload caddy
```

Caddy and the `nutrition-api` service are independent — restart or update the
backend without touching Caddy, and the landing page never goes down with it.
