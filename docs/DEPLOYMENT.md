# Deployment Guide

## Docker Compose

The repository includes two compose entry points:

- [docker-compose.yml](../docker-compose.yml) — production-style deployment.
- [docker-compose.dev.yml](../docker-compose.dev.yml) — development workflow with bind mounts for live reload.

### Start the stack

```bash
docker compose up --build
```

The app is served on port `8000` and the health endpoint is available at `/health`.

### Environment requirements

- Copy [.env.example](../.env.example) to `.env` and fill in your API keys.
- Keep the `.env` file mounted into the container so the runtime can read the same values as local development.
- For production deployments, prefer a single Uvicorn worker because the concurrent-session cap is process-local.

## Production notes & Security Configuration

When deploying Cascade to production, you MUST configure the following environment variables to ensure security:

- **Auth Handshake:** Set `CASCADE_AUTH_SECRET` if you want the WebSocket gateway to require an auth handshake.
- **CORS:** Restrict `CASCADE_CORS_ORIGINS=https://your-domain.com` instead of leaving it as `*` for public deployments.

### Proxy Trust & IP Hashing (Critical)
- `CASCADE_TRUST_PROXY_HEADERS=true`: Set this ONLY if you are running behind a trusted reverse proxy (e.g., Cloudflare) that overwrites `CF-Connecting-IP` or `X-Forwarded-For`. If your Cascade instance is directly exposed to the internet, leave this `false` (default) to prevent IP spoofing attacks via crafted HTTP headers.
- `CASCADE_IP_HASH_SECRET=<your-stable-secret>`: Set this to a long, random string. Cascade HMAC-salts client IPs before storing them in the quota database. If this is unset, Cascade generates an ephemeral key on startup, meaning all IP hashes change every time you restart the server (resetting per-IP rate limits).

## Rate-Limited Testing (Quota System)

For the public beta/testing phase, Cascade supports a quota system to enforce a strict time limit per user and a capacity limit for concurrent beta users.

### Configuration

You must set these in your deployment environment or `.env` file:

```bash
# Enable the quota system for the public test
CASCADE_QUOTA_ENABLED=true

# Database location (ensure the directory exists and is writable)
CASCADE_QUOTA_DB_PATH=./data/quota.db

# Limits
CASCADE_MAX_TESTERS=100
CASCADE_IP_REGISTRATION_LIMIT=5
CASCADE_TESTER_BUDGET_SEC=300
```

- **Billing Model**: Testers are only billed for time spent *actively speaking* or while the *AI is speaking*. Idle time and thinking pauses do not deduct from their budget.
- **NAT Support**: The `CASCADE_IP_REGISTRATION_LIMIT` defaults to 5 to avoid aggressively blocking legitimate testers sharing a single IP via Carrier-Grade NAT (CGNAT, common in mobile networks).

### Database and State

- The SQLite database is created automatically at `CASCADE_QUOTA_DB_PATH`.
- Usage is saved at the end of the session, and periodically while active, to prevent data loss on crashes.
- Do not check the `data/` folder into version control. Ensure it is mounted to a persistent volume in Docker.

## Proxy-Level Rate Limiting (Recommended)

Cascade has application-layer concurrency caps, but it is highly recommended to add infrastructure-level rate limiting to protect the WebSocket endpoint from abuse:

**Example NGINX config:**
```nginx
limit_req_zone $binary_remote_addr zone=ws_limit:10m rate=10r/s;

server {
    location /ws {
        limit_req zone=ws_limit burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        # Required if CASCADE_TRUST_PROXY_HEADERS=true
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```
