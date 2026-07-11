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

## Production notes

- Set `CASCADE_AUTH_SECRET` if you want the WebSocket gateway to require an auth handshake.
- Restrict `CASCADE_CORS_ORIGINS` instead of leaving it as `*` for public deployments.
- Use a reverse proxy in front of the container if you need TLS termination or additional gateway controls.

## Rate-Limited Testing (Quota System)

For the public beta/testing phase, Cascade supports a quota system to enforce a strict time limit per user and a capacity limit for concurrent beta users.

### Requirements

To correctly rate-limit users by IP, the application relies on the `CF-Connecting-IP` header. If you are not using Cloudflare, you must configure your reverse proxy (e.g., Nginx) to forward the client's true IP in either `CF-Connecting-IP` or `X-Forwarded-For`.

### Configuration

You must set these in your deployment environment or `.env` file:

```bash
# Enable the quota system for the public test
CASCADE_QUOTA_ENABLED=true

# Database location (ensure the directory exists and is writable)
# The default is ./data/quota.db
CASCADE_QUOTA_DB_PATH=./data/quota.db

# Limits
CASCADE_MAX_TOTAL_REGISTRATIONS=100
CASCADE_MAX_REGISTRATIONS_PER_IP=3
CASCADE_TESTER_BUDGET_SEC=300
```

### Database and State

- The SQLite database is created automatically at `CASCADE_QUOTA_DB_PATH`.
- Usage is saved every 30 seconds of active speaking time to prevent data loss on crashes.
- Do not check the `data/` folder into version control. Ensure it is mounted to a persistent volume in Docker.

### Monitoring

You can monitor real-time usage by querying the SQLite database directly:

```bash
sqlite3 ./data/quota.db "SELECT * FROM quota_stats;"
```
