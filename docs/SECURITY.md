# Security Policy

## Supported versions

The project currently provides security updates on the latest main branch.

## Reporting a vulnerability

Please report suspected vulnerabilities privately by emailing the maintainers or opening a private security advisory through the repository's reporting channel.

## Authentication and deployment notes

- The WebSocket gateway supports optional HMAC-based authentication via `CASCADE_AUTH_SECRET`.
- Restrict `CASCADE_CORS_ORIGINS` in production instead of leaving it open to all origins.
- Keep Deepgram and Groq API keys server-side only and avoid exposing them in frontend code or URL parameters.
- Prefer a reverse proxy and single-worker deployment for production environments.
