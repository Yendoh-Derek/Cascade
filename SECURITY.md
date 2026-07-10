# Security Policy

## Supported Versions

Cascade is under active development on `main`. Security fixes are made
against `main` only; there are no maintained release branches yet.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities —
this includes anything related to:

- WebSocket authentication/session handling (`backend/main.py`)
- API key or secret handling (`.env`, `config.py`)
- Injection risks in transcript or LLM-generated content reaching the
  frontend
- Denial-of-service vectors in the audio/streaming pipeline

Instead, use GitHub's private vulnerability reporting:
**Security tab → "Report a vulnerability"** on this repository. This opens a
private advisory visible only to maintainers until a fix is ready.

If private reporting isn't available to you, contact the maintainer directly
through the contact method on their GitHub profile rather than filing a
public issue.

## What to Include

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal repro is very helpful)
- Any relevant logs, stack traces, or captured WebSocket messages (with
  secrets redacted)

## Response Expectations

This is a community-maintained open source project without a dedicated
security team or SLA. Reports will be acknowledged as soon as possible on a
best-effort basis, and credited in the fix's changelog entry unless you
request otherwise.

## Handling Secrets

Never commit `.env` files or real API keys. `.env.example` documents the
required variables; `.env` is already git-ignored. If you accidentally
commit a secret, rotate it immediately at the provider — removing it from
git history alone does not invalidate an already-leaked key.
