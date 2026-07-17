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

Once running: `http://<host>:8080/` is a landing page linking to the barcode
lookup tester (`/lookup`) and the name-search UI (`/search`), the OpenAPI docs
are at `/docs`, and `/api/v1/health` reports per-source status (it returns
`degraded`, not an error, when an upstream is unreachable).

### Hardening notes

The unit runs with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, and
`ProtectHome=read-only`. `data/` is granted back via `ReadWritePaths` because
the app self-updates the GS1 GPC taxonomy there on startup — dropping that line
makes the taxonomy update fail silently, so keep it if you move the data
directory.

`MemoryHigh`/`MemoryMax`/`CPUQuota` are sized for a 12 GB RAM / 4 vCPU host
that also runs Caddy and other services — adjust for your own box.

**uvicorn binds `0.0.0.0:8080`, not `127.0.0.1`,** so that other machines on
the LAN (and, once installed, the tailnet) can reach the backend directly for
debugging without going through Caddy. That only stays safe with a host
firewall restricting who can actually reach 8080 — Caddy's own protections
(rate limiting, TLS) don't help you if the backend is reachable by skipping
Caddy entirely. On this deployment that's enforced with `iptables`
(`iptables-persistent` keeps it across reboots):

```bash
sudo iptables -A INPUT -p tcp --dport 8080 -s 127.0.0.1 -j ACCEPT      # Caddy
sudo iptables -A INPUT -p tcp --dport 8080 -s <your-LAN-CIDR> -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8080 -s 100.64.0.0/10 -j ACCEPT  # tailnet
sudo iptables -A INPUT -p tcp --dport 8080 -j DROP
sudo apt-get install iptables-persistent && sudo netfilter-persistent save
```

If you don't need LAN/tailnet access to the raw backend, bind uvicorn to
`127.0.0.1` in `ExecStart` instead and skip the firewall rules — simpler, and
nothing but Caddy can ever reach port 8080 at all.

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
| `/lookup` | barcode (GTIN/UPC) lookup tester (backend) |
| `/search` | search by product name (backend) |
| `/gpc`, `/gpc/mappings` | GPC browser, GPC mapping viewer (backend) |
| `/data` | data browser (backend) |
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
Every variant `import`s the same `caddy/site.caddy`, so only the site line
changes.

**HTTPS by IP with Caddy's internal CA** (a LAN box, no domain) — what this
deployment uses:

```
import /opt/nutrition_api/deploy/caddy/site.caddy
https://192.168.1.50 {
	import nutrition_api
}
```

Naming the site `https://<IP>` makes Caddy issue a certificate for that address
from its own internal CA — it cannot get a *publicly*-trusted cert for a bare IP
— and redirect `http://` to `https://` automatically. Browsers warn until that
CA is trusted (next section).

**By domain with automatic, publicly-trusted HTTPS** (public host that resolves
to this machine, ports 80/443 reachable): use your hostname as the site name —
Caddy provisions and renews the cert via ACME, and there is no CA to trust.

```
import /opt/nutrition_api/deploy/caddy/site.caddy
nutrition.example.org {
	import nutrition_api
}
```

**Plain HTTP, no TLS** (throwaway/local): add a `{ auto_https off }` global
block and use `:80` as the site name.

If the checkout is not at `/opt/nutrition_api`, set `NUTRITION_SITE_ROOT` (or
edit the `root` in `caddy/site.caddy`) and fix the `import` path.

### Trusting the internal CA (self-signed by IP)

With the internal-CA variant, Caddy generates a long-lived root once per host.
Installing that root on your client machines turns the browser warning into a
green lock. The root is downloadable from the proxy itself:

```bash
# Fetch it (‑k because the connection isn't trusted yet)
curl -k https://<IP>/caddy-local-ca.crt -o caddy-local-ca.crt
# …or straight from the host
sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
```

Then trust it:

- **Linux:** `sudo cp caddy-local-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`
- **macOS:** double-click → Keychain Access → set to *Always Trust*
- **Windows:** import into *Trusted Root Certification Authorities*
- **Chrome/Firefox** (if they use their own store): import under Settings →
  Security → Manage certificates → Authorities.

The CA root is generated per host, so it is **not** committed to the repo
(`deploy/site/caddy-local-ca.crt` is git-ignored). Publishing your own root to
your own machines is fine; it authorises certs only for what this Caddy issues.

### Reusing a multi-backend Caddy config

If you already run a Caddy LXC in front of other resources, the only piece that
transfers here is the `https://<IP>` site name for the internal-CA cert. The
rest of a Proxmox/Ceph-style config does **not** apply: this backend is a single
plain-HTTP uvicorn on `127.0.0.1:8080`, so there is no load balancing
(`lb_policy`, multiple `to` upstreams), no `tls_insecure_skip_verify` (that is
for HTTPS upstreams with self-signed certs), and no WebSocket or `Location`
header rewriting. If you *do* put several backends behind it later,
`reverse_proxy`'s active health checks (`health_uri /api/v1/health`,
`health_interval`, `health_status 200`) are the pattern to copy.

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
