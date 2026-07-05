# Contributing to Cascade

Welcome to Cascade! We are building a low-latency streaming voice tutor and welcome well-scoped documentation and code improvements.

## Development Setup

1. Create and activate a Python virtual environment.
2. Install the runtime and dev dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```
3. Copy the sample environment file and fill in your API keys:
   ```bash
   cp .env.example .env
   ```

## Testing

Run the core regression suite before opening a PR:

```bash
pytest tests/test_tutor.py tests/test_latency_metrics.py tests/test_ws_security.py tests/test_stt.py tests/test_mock_integrations.py -v
ruff check .
mypy backend/ --ignore-missing-imports
node tests/frontend/test_chart.js
```

- Core pipeline paths should be covered by integration tests under [tests](tests).
- For tests that need live API keys, add or update a mock-safe test in [tests/test_mock_integrations.py](tests/test_mock_integrations.py) so CI can exercise the same logic without credentials.
- Frontend logic that does not require a browser should remain testable with plain Node.js, as in [tests/frontend/test_chart.js](tests/frontend/test_chart.js).

## Architecture Decision Records (ADR)

We use ADRs to document significant architectural decisions. If you are introducing a major feature, refactoring a core system, or changing a design pattern, please add an ADR in [docs/adr](docs/adr).

Suggested format:

- Title
- Context
- Decision
- Consequences

## Commenting Guidance

When a non-obvious implementation detail needs context, keep the explanation brief and focused on the current behavior, inputs, and outputs.
