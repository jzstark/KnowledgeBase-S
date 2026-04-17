# Deployment & Local Development Guide

---

## VPS Deployment (AWS EC2, Ubuntu, Docker installed)

### 1. Install Docker Compose plugin

```bash
sudo apt-get update
sudo apt-get install -y docker-compose-plugin git
docker compose version   # verify
```

### 2. Authenticate with GitHub Container Registry

Create a GitHub Personal Access Token with `read:packages` scope, then:

```bash
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### 3. Clone the repo

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/KnowledgeBase-S.git
cd KnowledgeBase-S
```

### 4. Create `.env`

```bash
cp .env.example .env
nano .env
```

Fill in every field:

```env
DB_PASSWORD=a_strong_password
DATABASE_URL=postgresql://postgres:a_strong_password@postgres:5432/app

CLAUDE_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

AUTH_PASSWORD=your_login_password
AUTH_SECRET=any_random_32_char_string

NEXTAUTH_URL=http://YOUR_EC2_PUBLIC_IP   # or https://yourdomain.com
GITHUB_OWNER=your_github_username
```

> **DB_PASSWORD** and the password inside **DATABASE_URL** must be identical.

### 5. Open EC2 Security Group ports

In the AWS console, add inbound rules to the instance's Security Group:

| Type  | Port | Source    |
|-------|------|-----------|
| HTTP  | 80   | 0.0.0.0/0 |
| HTTPS | 443  | 0.0.0.0/0 |

### 6. Pull images and start

```bash
# Pull all pre-built images from ghcr.io
docker compose pull

# Start core services (postgres, api, web, nginx, scheduler, rsshub, watchtower)
docker compose up -d

# Start workers (ingestion, summarizer, feedback)
docker compose --profile workers up -d
```

Check everything is running:

```bash
docker compose ps
```

All services should show `Up`. The `api` container waits for postgres to pass its healthcheck before starting.

### 7. Verify

Open `http://YOUR_EC2_PUBLIC_IP` in a browser — you should see the login page.  
Log in with the `AUTH_PASSWORD` from your `.env`.

### 8. (Optional) Custom domain + HTTPS

If you have a domain pointed at the EC2 IP:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d yourdomain.com
```

Then update `nginx/nginx.conf` to add an HTTPS server block and mount the certs.  
Alternatively, put Cloudflare in front (proxy mode, orange cloud) — the existing nginx config already works behind Cloudflare with HTTP only on port 80.

---

### Ongoing operations

| Task | Command |
|------|---------|
| View all logs | `docker compose logs -f` |
| View one service | `docker compose logs -f api` |
| Restart a service | `docker compose restart api` |
| Pull & redeploy | `./deploy.sh` |
| Check service status | `docker compose ps` |
| Stop everything | `docker compose down` |
| Backup user data + DB | `./scripts/backup.sh` |

**Watchtower** runs inside the stack and polls `ghcr.io` every hour. Once a new image is pushed by GitHub Actions, it is pulled and the service restarted automatically — no manual redeploy needed.

---

### Notes

- **AWS regions are not blocked by Anthropic** — Claude API calls work without a proxy. (Unlike Aliyun HK, which returns 403 for Anthropic traffic.)
- **Data directories** (`./data/postgres/` and `./user_data/`) are created automatically on first run. Back them up regularly with `scripts/backup.sh`.
- **Workers profile**: `ingestion-worker`, `summarizer-worker`, and `feedback-worker` are under the `workers` Docker Compose profile. They do not start with plain `docker compose up -d` — always add `--profile workers`.

---

## Local Development

Local dev uses `docker-compose.dev.yml` on top of the base compose file. Key differences from production:
- Images are **built locally** (not pulled from ghcr.io)
- `api` and `web` run with **hot reload** (`--reload` / `npm run dev`)
- Source directories are **bind-mounted** into containers — code changes take effect immediately without rebuilding
- `postgres` uses a **named volume** (`postgres_dev`) instead of a bind mount, avoiding Docker Desktop permission issues
- `web/.next` build cache is in a named volume (`web_next`) so it survives container restarts
- `scheduler` and `watchtower` are disabled in dev mode

### First-time setup

```bash
# Build all dev images (required on first run or after Dockerfile changes)
make build-dev
```

### Daily workflow

```bash
# Start everything in the foreground (see all logs inline)
make dev

# Or start in background
make dev-d

# Stop
make down
```

### Rebuild after Dockerfile or dependency changes

```bash
# Rebuild a specific service (e.g. after changing requirements.txt)
docker compose -f docker-compose.yml -f docker-compose.dev.yml build --no-cache api

# Rebuild web after adding npm packages
docker compose -f docker-compose.yml -f docker-compose.dev.yml build --no-cache web
```

When adding a new npm package, also update `package-lock.json` on the host before rebuilding:

```bash
cd services/web && npm install --package-lock-only && cd ../..
make build-dev
```

### Run ingestion once (process pending files / RSS)

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  run --rm ingestion-worker python main.py --once
```

### Trigger briefing generation manually

```bash
# Get a session cookie first (replace password)
curl -c /tmp/kb_cookies.txt -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password":"YOUR_AUTH_PASSWORD"}'

# Generate today's briefing
curl -X POST http://localhost/api/briefing/generate -b /tmp/kb_cookies.txt
```

### Trigger maintenance manually

```bash
curl -X POST http://localhost/api/kb/maintenance/run -b /tmp/kb_cookies.txt
```

### Access the database directly

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  exec postgres psql -U postgres -d app
```

### Useful dev URLs

| Service | URL |
|---------|-----|
| Web frontend | http://localhost |
| API (direct) | http://localhost:8000/docs |
| API health | http://localhost:8000/api/kb/wiki/status |

### Common problems

**`next: not found` on web container start**  
Old cached image is being used. Force a clean rebuild:
```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build --no-cache web
```

**Postgres permission error on first run**  
Use `make build-dev` then `make dev` — the named volume (`postgres_dev`) avoids bind-mount permission issues on Docker Desktop (Mac/Windows).

**API changes not reflecting**  
The api container runs with `--reload`, so Python file changes apply automatically. If you changed `requirements.txt`, rebuild the image:
```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build api
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d api
```

**DB_PASSWORD mismatch**  
If postgres refuses connections, check that `DB_PASSWORD` in `.env` exactly matches the password in `DATABASE_URL`. To reset: `docker compose down -v` (destroys the dev volume) then `make dev`.
