# Project Forms App (FastAPI + Postgres + Vanilla Frontend + Nginx)

A small, self-contained web application for managing **Projects**, composing **Forms** (question sets + custom questions), and collecting **Records** (submitted answers). It ships as a Docker Compose stack with:

- **Postgres** for persistence
- **FastAPI** backend (Python)
- **Static frontend** (HTML/CSS/JS served by Nginx)
- **Edge reverse proxy** (Nginx) routing `/` to the frontend and `/api/` to the backend

---

## Contents

- [Architecture](#architecture)
- [Services and ports](#services-and-ports)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Bootstrap admin account](#bootstrap-admin-account)
- [How to use the app](#how-to-use-the-app)
- [API overview](#api-overview)
- [Configuration](#configuration)
- [Deploying](#deploying)
- [Maintaining](#maintaining)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Repo structure](#repo-structure)

---

## Architecture

### High-level flow

```

Browser
|
|  [http://localhost:8082/](http://localhost:8082/)
v
proxy (nginx)
|-- /        -> frontend (nginx serving static HTML/JS/CSS)
|
|-- /api/    -> backend (FastAPI)
|
v
Postgres

````

### Why there are two Nginx configs

There are **two Nginx containers**, each with a separate responsibility:

1. `frontend/nginx.conf`  
   Serves the static site (HTML/JS/CSS) from `/usr/share/nginx/html`.

2. `proxy/nginx.conf`  
   Acts as the single entrypoint for the stack:
   - routes `/` to `frontend`
   - routes `/api/` to `backend`
   - enforces `client_max_body_size`
   - rewrites cookie attributes to keep same-origin browser sessions working cleanly

This adds one extra hop for frontend requests (proxy → frontend), but keeps the design simple: one public port, same-origin cookies, and no CORS complexity.

---

## Services and ports

From `docker-compose.yml`:

- **db**: `postgres:16-alpine`
  - persistent data: `./pgdata` → `/var/lib/postgresql/data`

- **backend**: built from `./backend`
  - internal port: `8000` (not exposed on host)
  - connects to Postgres using:
    - `DATABASE_URL=postgresql+psycopg2://app:app@db:5432/app`

- **frontend**: built from `./frontend`
  - internal port: `80` (not exposed on host)
  - serves static files via Nginx

- **proxy**: built from `./proxy`
  - **host port**: `8082` → container port `80`
  - this is what you browse locally: `http://localhost:8082`

---

## Quick start (Docker Compose)

### Prerequisites
- Docker + Docker Compose (v2)
- Ports available: `8082` (or adjust compose fit to your setup)

### Run the stack
From the repo root where `docker-compose.yml` lives:

```bash
docker compose up --build
````

Open the app:

* `http://localhost:8082`

Stop:

```bash
docker compose down
```

---

## Bootstrap admin account

This project uses **invite-based registration**. To make initial setup painless, the backend prints a **bootstrap invite** on first run **if no admin user exists** in the database.

### What to do

1. Start the stack:

   ```bash
   docker compose up --build
   ```

2. Check backend logs for a block like:

   * “BOOTSTRAP ADMIN REGISTRATION”
   * a `register.html?key=...` link
   * a `Secret: ...`

3. Open the registration page in your browser:

   * If the log shows `http://localhost:8080/...` or use your proxy port (whatever you define it to be) instead:

     * `http://localhost:8082/register.html?key=...`

4. Enter:

   * the invite **secret**
   * your email
   * your password + confirm

The first successfully registered user becomes an **admin**.

> The bootstrap invite prints **only when there is no admin** in the database.

---

## How to use the app

### Login

* Use the login page (`index.html`) to sign in.
* The backend sets an **HTTP-only session cookie**.
* Requests then authenticate via that cookie automatically.

### Inviting users (admin)

Admins can create invites in the UI (or via API). An invite returns:

* a link containing the invite **key**: `/register.html?key=...`
* a one-time **secret** that must be provided during registration

Typical admin process:

1. Create invite
2. Share link + secret to the user (preferably via separate channels)

### Projects, forms, and records

* **Projects** represent a logical unit that contains:

  * one or more **question sets**
  * optional **custom questions**
* A project’s **form** is the merged view the frontend uses for submission.
* A **record** is a saved submission of answers for a project.

---

## API overview

The backend is a FastAPI app behind the proxy. The browser talks to it at:

* `http://localhost:8082/api/...`

Core endpoints:

* `POST /api/login` — start a session (sets cookie)
* `POST /api/logout` — revoke session (clears cookie)
* `POST /api/register?key=...` — register using invite key + secret
* `GET /api/me` — current user info
* `POST /api/me/api_token/regenerate` — creates/regenerates API token

Projects / records:

* `GET /api/projects`
* `POST /api/projects`
* `GET /api/projects/{project_id}/form`
* `POST /api/projects/{project_id}/records`
* `GET /api/projects/{project_id}/records`
* `GET /api/projects/{project_id}/last_record`
* `GET /api/records/{record_id}`
* `PUT /api/records/{record_id}`
* `POST /api/records/{record_id}/review`

Admin endpoints (require admin):

* `POST /api/admin/invites`
* `GET /api/admin/users`
* `PATCH /api/admin/users/{user_id}` (access control)
* `GET /api/admin/question_sets`
* `POST /api/admin/question_sets`
* `PUT /api/admin/question_sets/{question_set_id}`
* `DELETE /api/admin/question_sets/{question_set_id}`
* `GET /api/admin/projects`
* `POST /api/admin/projects`
* `PATCH /api/admin/projects/{project_id}`
* `DELETE /api/admin/projects/{project_id}`
* `PUT /api/admin/projects/import`
* `POST /api/admin/projects/question_sets_batch`
* `GET/PUT /api/admin/projects/{project_id}/question_sets`
* `GET/POST /api/admin/projects/{project_id}/custom_questions`
* `PUT/DELETE /api/admin/projects/{project_id}/custom_questions/{question_id}`

### Authentication modes

* **Browser**: cookie-based session
* **Programmatic**: API token via `Authorization: Bearer <token>`

---

## Configuration

### Backend environment variables

Set in `docker-compose.yml` (backend service):

* `DATABASE_URL`
* `APP_ENV`
* `COOKIE_SECURE` (recommended `1` behind HTTPS)

Additional supported settings (see `backend/config.py`):

* `MAX_BODY_BYTES` (default 1 MiB)
* `SESSION_COOKIE_NAME` (default `session_token`)
* `SESSION_TTL_HOURS` (default 48)
* `RATE_LIMIT_RPM` (default 600)

### Request/body limits

* Proxy Nginx enforces: `client_max_body_size 1m`
* Backend middleware enforces `MAX_BODY_BYTES`

Keep these aligned if you change limits.

---

## Deploying

### Recommended deployment pattern

This Compose stack is production-friendly with a few changes:

1. Run the stack behind a real TLS terminator:

   * cloud load balancer, Traefik, Caddy, or a front Nginx
2. Set:

   * `COOKIE_SECURE=1` (so cookies are Secure on HTTPS)
3. Lock down Postgres:

   * use a strong password
   * restrict network exposure (don’t publish db port)
4. Configure persistence and backups:

   * `./pgdata` should be a durable volume
   * implement backups (pg_dump or snapshot)

### Production compose tips

* Pin image versions and build args explicitly
* Use `.env` files (and avoid committing secrets)
* Add container restart policies if desired:

  ```yaml
  restart: unless-stopped
  ```
* Consider logging/monitoring integration

### External domain routing

If you put this behind another reverse proxy:

* Route `/` and `/api/` to the **proxy** container (not directly to backend/frontend)
* Keep same-origin behavior unless you intentionally add CORS

---

## Maintaining

### Database persistence

* Data lives in `./pgdata`
* To migrate hosts, back up `pgdata` or use `pg_dump`/`pg_restore`

### Schema updates / migrations

The backend includes a lightweight “auto-add missing columns” migration routine at startup:

* It **adds missing columns** if models change
* It **does not** modify existing column types/constraints
* It’s great for MVP evolution, but for mature production usage consider adding Alembic migrations.

### Dependency updates

* Keep Python dependencies pinned (requirements file / lock)
* Regularly update base images:

  * `postgres:16-alpine`
  * nginx images used in frontend/proxy builds

### Operational checks

* Watch logs:

  ```bash
  docker compose logs -f backend
  docker compose logs -f proxy
  ```
* Confirm health:

  * Postgres uses `pg_isready` healthcheck
  * Backend availability: `GET /api/me` (after login)

### User and access management

* Admin users can:

  * invite users
  * assign project access
  * manage question sets and project configuration
* API tokens should be rotated if leaked.

---

## Troubleshooting

### “I can’t register—invite key invalid”

* Invites are single-use.
* Make sure you’re using:

  * the correct `key` in the URL (`register.html?key=...`)
  * the correct invite `secret` in the form

### “Bootstrap invite shows localhost:8080”

That's affected by *my local* setup, adjust docker-compose.yml to *your setup*.
When using *this compose*, use:

* `http://localhost:8082/register.html?key=...`

### “Upload fails / request too large”

Update both:

* `proxy/nginx.conf`: `client_max_body_size`
* backend env: `MAX_BODY_BYTES`

### “Cookie/session doesn’t stick”

If you are on HTTPS but `COOKIE_SECURE=0`, browsers may reject Secure expectations.
For HTTPS deployments, set:

* `COOKIE_SECURE=1`

---

## Security notes

* Cookies are HTTP-only and SameSite=Strict (via proxy cookie path rewrite).
* For production:

  * Use TLS
  * Set `COOKIE_SECURE=1`
  * Use strong Postgres credentials
  * Restrict network access to internal services
  * Rotate API tokens if compromised
  * Consider adding auditing/log shipping

### What is missing in terms of security, reliability, stability, maintainability?

* [ ] **Unauthenticated rate limiting is effectively missing**: the rate limiter key is derived only from an existing session cookie or bearer token, so endpoints like /api/login and /api/register can be brute-forced without hitting the limiter. 

* [ ] **Rate limiting won’t work when scaling horizontally**: it’s in-memory per backend instance (explicitly noted), so multiple replicas bypass limits unless you move it to Redis/gateway. 

* [ ] **Invite hardening is minimal**: invites are single-use, but there’s no expiry/TTL field or enforcement, so unused invites can live forever unless manually managed. 

* [ ] **Session “fingerprinting” can hurt stability**: sessions are bound to IP + User-Agent and will be revoked on mismatch, which can log users out if their IP changes (mobile networks, corporate proxies). 

* [ ] **Missing standard security headers**: both Nginx configs mainly add Cache-Control and don’t set CSP, X-Frame-Options/frame-ancestors, X-Content-Type-Options, Referrer-Policy, etc. 

* [ ] **Operational reliability gaps**: Postgres has a healthcheck, but there are no healthchecks for backend/proxy/frontend; also API logging writes to DB on every request and failures are swallowed (good for uptime, but can hide issues and grow the ApiLog table without retention). 

* [ ] **Maintainability/migrations**: the schema approach is lightweight; there’s no visible formal migration tooling (e.g., Alembic), which becomes risky as schema changes accumulate over time.


---

## Repo structure

Typical layout based on Compose build context:

```
.
├─ docker-compose.yml
├─ backend/
│  ├─ main.py
│  ├─ auth.py
│  ├─ crud.py
│  ├─ db.py
│  ├─ models.py
│  ├─ schemas.py
│  ├─ middleware.py
│  ├─ utils.py
│  ├─ config.py
│  └─ (Dockerfile, requirements, etc.)
├─ frontend/
│  ├─ index.html
│  ├─ register.html
│  ├─ app.html
│  ├─ app.js
│  ├─ common.js
│  ├─ styles.css
│  ├─ nginx.conf
│  └─ (Dockerfile, etc.)
└─ proxy/
   ├─ nginx.conf
   └─ (Dockerfile, etc.)
```

---
## License / Ownership

MIT License
