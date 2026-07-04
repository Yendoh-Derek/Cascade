"""
cascade/tests/test_ws_security.py

Integration tests to verify CORS credentials, WebSocket authentication, and concurrent session limit.
"""

import sys
import os
import asyncio
import hmac
import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import app
from backend.main import app  # noqa: E402
import backend.main  # noqa: E402

# Starlette TestClient uses host "testserver" and sends no Origin by default.
WS_TEST_HEADERS = {"origin": "http://testserver"}

@pytest.fixture(scope="module", autouse=True)
def mock_server_config():
    """Patch the already-imported server_config so it applies regardless of test execution order."""
    import backend.main
    from backend.config import ServerConfig
    orig = backend.main.server_config
    new_config = ServerConfig(
        host=orig.host,
        port=orig.port,
        max_concurrent_sessions=3,
        cors_origins=orig.cors_origins,
        auth_secret="test-secret",
        idle_timeout_sec=orig.idle_timeout_sec
    )
    with patch("backend.main.server_config", new_config):
        with patch("backend.main.MAX_CONCURRENT_SESSIONS", 3):
            yield


@pytest.fixture
def mock_pipeline_dependencies():
    """Mock backend dependencies so auth tests can focus on the WS handshake."""
    with (
        patch("backend.main.get_api_keys", return_value=SimpleNamespace(deepgram="dg", groq="gq")),
        patch(
            "backend.main.get_model_config",
            return_value=SimpleNamespace(
                deepgram_model="nova-3",
                deepgram_language="en-US",
                sample_rate=16000,
                channels=1,
                stt_endpointing_ms=300,
                vad_threshold=0.5,
                vad_silence_ms=200,
                vad_min_speech_frames=3,
                speculative_grace_ms=180,
                interim_trigger_words=6,
                speculative_stability_matches=2,
                enable_speculative_llm=False,
                groq_model="llama-3.1-8b-instant",
                max_history_turns=10,
                edge_tts_voice="en-US-AriaNeural",
                deepgram_tts_model="aura-2-asteria-en",
            ),
        ),
        patch("backend.main.PipelineSession.initialize", new=AsyncMock()),
        patch("backend.main.PipelineSession.close", new=AsyncMock()),
    ):
        yield


def _authenticate_websocket(websocket, secret: str = "test-secret"):
    """Complete the HMAC challenge-response handshake used by the server."""
    challenge = websocket.receive_json()
    assert challenge["type"] == "challenge"
    response = hmac.new(secret.encode(), challenge["nonce"].encode(), "sha256").hexdigest()
    websocket.send_json({"type": "auth", "response": response})


def test_cors_allow_credentials():
    """Verify CORS headers do not allow credentials when allow_origins is '*'."""
    client = TestClient(app)
    response = client.get("/health", headers={
        "Origin": "http://example.com",
        "Access-Control-Request-Method": "GET"
    })
    assert response.status_code == 200
    # Credentials should not be allowed
    assert "access-control-allow-credentials" not in response.headers


def test_websocket_auth_unauthorized():
    """Verify WebSocket connection fails when CASCADE_AUTH_SECRET is set and no/wrong secret is sent."""
    client = TestClient(app)

    # 1. No auth response after challenge
    with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "challenge"
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]

    # 2. Incorrect HMAC response
    with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "challenge"
        websocket.send_json({"type": "auth", "response": "wrong-secret"})
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]


@pytest.mark.usefixtures("mock_pipeline_dependencies")
def test_websocket_auth_authorized():
    """Verify WebSocket connection succeeds when correct CASCADE_AUTH_SECRET is sent."""
    client = TestClient(app)
    with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as websocket:
        _authenticate_websocket(websocket)
        msg = websocket.receive_json()
        assert msg["type"] == "auth_ok"
        websocket.send_text("stop")


@pytest.mark.usefixtures("mock_pipeline_dependencies")
def test_websocket_auth_first_message():
    """Verify WebSocket connection succeeds using the first-message JSON handshake."""
    client = TestClient(app)
    with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as websocket:
        _authenticate_websocket(websocket)
        msg = websocket.receive_json()
        assert msg["type"] == "auth_ok"
        websocket.send_text("stop")


def test_websocket_concurrency_limit(mock_pipeline_dependencies):
    """Verify concurrent connections limit semaphore works."""
    # Temporarily set semaphore capacity to 2 for test predictability
    original_semaphore = backend.main.session_semaphore
    backend.main.session_semaphore = asyncio.Semaphore(2)

    client = TestClient(app)
    try:
        # Establish connection 1
        with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as ws1:
            _authenticate_websocket(ws1)
            assert ws1.receive_json()["type"] == "auth_ok"
            # Establish connection 2
            with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as ws2:
                _authenticate_websocket(ws2)
                assert ws2.receive_json()["type"] == "auth_ok"
                # Connection 3 should exceed cap (capacity is 2)
                with client.websocket_connect("/ws", headers=WS_TEST_HEADERS) as ws3:
                    msg = ws3.receive_json()
                    assert msg["type"] == "busy"
                    assert "maximum capacity" in msg["message"]
                ws2.send_text("stop")
            ws1.send_text("stop")
    finally:
        # Restore original semaphore
        backend.main.session_semaphore = original_semaphore


# ─── RateLimiter unit tests ───────────────────────────────────────────────────

class TestRateLimiter:
    """Unit tests for the token-bucket RateLimiter in pipeline.py."""

    def _make_limiter(self, bps=32_000, burst=2.0):
        from backend.pipeline import RateLimiter
        return RateLimiter(bytes_per_sec=bps, burst_sec=burst)

    def test_allows_within_burst(self):
        rl = self._make_limiter()
        # Full burst should be allowed immediately
        assert rl.allow(32_000 * 2) is True

    def test_blocks_above_capacity(self):
        rl = self._make_limiter()
        rl.allow(32_000 * 2)       # drain
        # Request a large chunk (1 second worth) so microsecond test delays don't refill enough to pass
        assert rl.allow(32_000) is False

    def test_refills_over_time(self):
        import time
        rl = self._make_limiter(bps=32_000, burst=0.1)
        rl.allow(3_200)     # drain
        time.sleep(0.05)            # ~50ms → refills ~1600B
        assert rl.allow(1_000) is True

    def test_zero_bytes_always_allowed(self):
        rl = self._make_limiter()
        rl.allow(32_000 * 2)       # drain
        assert rl.allow(0) is True

    def test_exact_capacity_boundary(self):
        rl = self._make_limiter(bps=1_000, burst=1.0)
        assert rl.allow(1_000) is True
        assert rl.allow(1) is False


# ─── Origin hostname validation unit tests ────────────────────────────────────

class TestOriginValidation:
    """Tests for the hostname-equality origin check in main.py.

    Validates that the urlsplit-based check properly isolates the hostname
    and prevents partial-match bypasses.
    """

    def _check(self, origin: str | None, host: str) -> bool:
        from backend.main import is_websocket_origin_allowed
        return is_websocket_origin_allowed(origin, host)

    def test_same_host_allowed(self):
        assert self._check("http://myapp.com", "myapp.com:8000") is True

    def test_localhost_always_allowed(self):
        assert self._check("http://localhost:3000", "myapp.com:8000") is True

    def test_127_allowed(self):
        assert self._check("http://127.0.0.1:5173", "myapp.com") is True

    def test_missing_origin_allowed_on_localhost(self):
        assert self._check(None, "localhost:8000") is True
        assert self._check(None, "127.0.0.1:8000") is True

    def test_missing_origin_rejected_on_public_host(self):
        assert self._check(None, "myapp.com:8000") is False
        assert self._check(None, "myapp.com") is False

    def test_evil_suffix_bypass_blocked(self):
        # Prevent bypass via domain suffix matching
        assert self._check("https://evil-myapp.com", "myapp.com") is False

    def test_path_injection_bypass_blocked(self):
        # Prevent bypass via path injection
        assert self._check("https://b.com/a.com", "a.com") is False

    def test_subdomain_rejected(self):
        assert self._check("https://evil.myapp.com", "myapp.com") is False

    def test_https_same_host_allowed(self):
        assert self._check("https://myapp.com", "myapp.com:443") is True


# ─── Pre-auth buffer cap tests ────────────────────────────────────────────────

class TestPreAuthBuffer:
    """Verify the 256KB cumulative cap logic for pre-auth audio (fix 0.3)."""

    def _simulate(self, chunks):
        accepted, rejected = [], []
        MAX_TOTAL = 256 * 1024
        MAX_CHUNK = 10_000_000
        for chunk in chunks:
            total = sum(len(c) for c in accepted)
            if len(chunk) <= MAX_CHUNK and total + len(chunk) <= MAX_TOTAL:
                accepted.append(chunk)
            else:
                rejected.append(chunk)
        return accepted, rejected

    def test_small_chunks_accepted(self):
        chunks = [b"x" * 1024] * 5
        acc, rej = self._simulate(chunks)
        assert len(acc) == 5 and len(rej) == 0

    def test_cumulative_cap_enforced(self):
        chunks = [b"x" * (64 * 1024)] * 5   # 5 × 64KB; cap is 256KB
        acc, rej = self._simulate(chunks)
        assert len(acc) == 4 and len(rej) == 1

    def test_oversized_chunk_rejected(self):
        chunks = [b"x" * 10_000_001]
        acc, rej = self._simulate(chunks)
        assert len(acc) == 0 and len(rej) == 1


# ─── TurnMetrics unit tests ───────────────────────────────────────────────────

class TestTurnMetrics:
    """Verify TurnMetrics dataclass defaults and mutation."""

    def test_defaults(self):
        from backend.pipeline import TurnMetrics
        m = TurnMetrics()
        assert m.utterance_end_time is None
        assert m.last_stt_tail_ms == 0
        assert m.stt_endpointing_ms == 0
        assert m.tts_metrics_sent is False

    def test_mutation(self):
        from backend.pipeline import TurnMetrics
        m = TurnMetrics()
        m.last_llm_ms = 250
        assert m.last_llm_ms == 250


# ─── History trim pair correctness tests ─────────────────────────────────────

class TestHistoryTrimming:
    """Verify trim_history never orphans an assistant message at history[0]."""

    def _make_tutor(self):
        from backend.tutor import TutorSession
        return TutorSession()

    def test_first_message_is_always_user_after_trim(self):
        tutor = self._make_tutor()
        for i in range(20):
            tutor.add_user_message(f"Question {i}: " + "word " * 50)
            tutor.add_assistant_message(f"Answer {i}: " + "word " * 50)
        tutor.trim_history(max_turns=10)
        if tutor.history:
            assert tutor.history[0]["role"] == "user"

    def test_pairs_preserved_after_trim(self):
        tutor = self._make_tutor()
        for i in range(10):
            tutor.add_user_message(f"Q{i} " + "a" * 200)
            tutor.add_assistant_message(f"A{i} " + "a" * 200)
        tutor.trim_history(max_turns=5)
        # Even-indexed messages should be user, odd should be assistant
        for i in range(0, len(tutor.history) - 1, 2):
            assert tutor.history[i]["role"] == "user"
            assert tutor.history[i + 1]["role"] == "assistant"

