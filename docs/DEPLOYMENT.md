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
