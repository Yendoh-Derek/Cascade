# Contributing to Cascade

Welcome to Cascade! We're building a state-of-the-art streaming AI voice tutor.

## Architecture Decision Records (ADR)
We use ADRs to document significant architectural decisions. If you are introducing a new major feature, refactoring a core system, or changing a design pattern, please write an ADR in the `docs/adr` folder.

Format:
- Title
- Context
- Decision
- Consequences

## Fix-Tracking Comment Standard
When applying a fix for a known issue (especially race conditions and streaming complexities), please add an inline comment with a fix tag, e.g., `// FIX [C1]` or `# FIX [C1]`. 

This allows us to track why certain counter-intuitive code structures (like `asyncio.shield` or atomic lock bypassing) exist so they are not accidentally removed in future refactors.

## Testing
- All core pipeline paths must be covered by integration tests in the `tests/` directory.
- For tests requiring API keys (like `test_llm.py`), ensure a mock-based test also exists in `test_mock_integrations.py` to allow CI to verify logic without keys.
- Frontend logic (like latency math) must be testable via standard Node.js without requiring full browser DOM emulation.
