# CLAUDE_SECURITY.md — Hetzner Dashboards

Read this alongside CLAUDE_RULES.md for any work touching Hetzner, Nginx, or dashboard deployments. Claude must treat these rules as non-optional when proposing any code or configuration.

---

## 0. Default Security Mode
Assume at all times:
- The server is reachable from the public internet
- Dashboards contain sensitive home, energy, occupancy, and vehicle data
- Any unauthenticated or plaintext exposure is unacceptable
- When in doubt: ask questions and choose the secure option

## 1. HTTPS Everywhere (Critical)
- Never design a production setup that serves dashboards or APIs over plain HTTP
- Always configure:
  - `listen 443 ssl;` with Let's Encrypt / certbot: `certbot --nginx -d your.domain.name`
  - HTTP→HTTPS redirect on port 80 (301)
  - HSTS header after confirming stability: `add_header Strict-Transport-Security "max-age=31536000" always;`
- If HTTPS is not yet configured, explicitly say so and show the certbot steps before anything else
- Verify auto-renewal: `sudo certbot renew --dry-run`

## 2. Authentication on All Routes (Critical)
- Every route that serves dashboards or APIs must be protected by both:
  - Nginx `auth_basic` (first layer)
  - App-level auth in Flask/Node (defense in depth)
- Client-side PIN checks (JavaScript) are not auth — they are trivially bypassed by direct URL access
- A protected root (`/project/`) does not automatically protect its API sub-paths (`/project/api/...`) — protect each explicitly
- If the user asks for "open for now" or "quick PIN in JS", warn and propose the minimal secure alternative instead

Flask auth pattern:
```python
import os
from flask import request, Response
from functools import wraps

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")  # no default — must be set

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response("Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="Dashboard"'})
        return f(*args, **kwargs)
    return decorated
```

Add `@requires_auth` to all dashboard routes and write endpoints.

## 3. Nginx Hardening
Every Nginx config must include:
```nginx
server_tokens off;

add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "no-referrer-when-downgrade" always;
add_header Strict-Transport-Security "max-age=31536000" always;  # after HTTPS confirmed

# Rate limiting for auth and API routes
limit_req_zone $binary_remote_addr zone=login:10m rate=5r/m;
limit_req zone=login burst=10 nodelay;

client_max_body_size 10M;
```

- Bind all Flask/Node backends to `127.0.0.1` only — never `0.0.0.0`
- Only expose ports 22, 80, 443 publicly
- Always check `sites-enabled` symlinks — editing `sites-available` has no effect until symlinked

## 4. Secrets Management (Critical)
- Never hardcode or commit PINs, passwords, API tokens, or SSH keys in JavaScript, Python, config files in the repo, deployment docs, or CLAUDE.md files
- All secrets go in `.env` or `config.json` with permissions `600`, listed in `.gitignore`
- If existing code contains hardcoded passwords or PINs, flag them immediately and propose a rotation plan
- Generate strong secrets: `python3 -c "import secrets; print(secrets.token_urlsafe(16))"`
- If a secret was committed to a public repo: treat it as permanently compromised and rotate immediately
- After rotation, remove from git history if repo is public (use `git filter-repo` or BFG)

`.gitignore` baseline for secrets:
```
.env
*.env
config/config.json
data/*.json
data/*.db
*.db-wal
*.db-shm
```

## 5. System Hardening
- Never run app services as root — always create a dedicated system user:
```bash
sudo adduser --system --group appuser
sudo chown -R appuser:appuser /opt/appdir
```

- systemd unit must specify `User=` and `Group=`:
```ini
[Service]
User=appuser
Group=appuser
EnvironmentFile=/opt/appdir/.env
ExecStart=/opt/appdir/venv/bin/gunicorn -b 127.0.0.1:5000 wsgi:app
```

- Install and configure `fail2ban` to monitor Nginx auth logs
- Keep OS packages updated: `sudo apt update && sudo apt upgrade`

## 6. Deployment Security Checklist
Run this checklist for every deploy or config change:

**Network**
- [ ] Only ports 22, 80, 443 open publicly
- [ ] All backends bind to `127.0.0.1` only

**Transport**
- [ ] HTTPS enabled with valid Let's Encrypt cert
- [ ] HTTP → HTTPS redirect in place
- [ ] HSTS header set (after confirming HTTPS stable)

**Authentication**
- [ ] Nginx `auth_basic` on all dashboard/API routes
- [ ] No client-side-only PIN checks
- [ ] App-level auth on all routes and write endpoints
- [ ] CSRF protection on POST routes

**Secrets**
- [ ] No secrets hardcoded in source or docs
- [ ] `.env`/config files not committed
- [ ] Previously committed secrets rotated

**Nginx**
- [ ] `server_tokens off`
- [ ] Security headers added
- [ ] Rate limiting on auth/API routes
- [ ] Correct `sites-enabled` symlink active

**System**
- [ ] Services run as non-root user
- [ ] `fail2ban` installed and monitoring Nginx
- [ ] OS packages up to date

## 6b. Write Endpoint and API Hardening
- All write endpoints (settings changes, schedule updates, data pulls) must require authentication
- Validate all incoming data — never trust client input
- Add logging for all sensitive operations: who changed what and when
- Apply rate limiting to API endpoints to prevent abuse

## 7. Recurring Security Review Prompt
Paste this periodically to check the current state:

```
Run a security review against my current Hetzner setup (Nginx + Flask/Node):

1. Network: which ports/services are reachable from the internet?
2. HTTPS: any plain HTTP remaining? Missing redirects or certbot steps?
3. Auth: list all routes — protected by Nginx auth / app-level auth / both / neither?
4. Secrets: any hardcoded passwords, PINs, or tokens? Propose env/config replacements.
5. Git: any sensitive files tracked that should be .gitignored?
6. OS: any services running as root? Propose systemd + user changes.
7. Weakest point: if an attacker scans my VPS right now, where is the easiest entry?

For each item: short risk assessment + minimal fix.
```
