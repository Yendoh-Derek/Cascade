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

# Save original environment variables
_orig_auth_secret = os.environ.get("CASCADE_AUTH_SECRET")
_orig_max_sessions = os.environ.get("CASCADE_MAX_CONCURRENT_SESSIONS")

# Set env var before importing app so it picks it up or we can test auth config
os.environ["CASCADE_AUTH_SECRET"] = "test-secret"
os.environ["CASCADE_MAX_CONCURRENT_SESSIONS"] = "3"

# Import app
from backend.main import app  # noqa: E402
import backend.main  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def clean_env():
    yield
    # Restore original environment variables after all tests in this module run
    if _orig_auth_secret is not None:
        os.environ["CASCADE_AUTH_SECRET"] = _orig_auth_secret
    elif "CASCADE_AUTH_SECRET" in os.environ:
        del os.environ["CASCADE_AUTH_SECRET"]

    if _orig_max_sessions is not None:
        os.environ["CASCADE_MAX_CONCURRENT_SESSIONS"] = _orig_max_sessions
    elif "CASCADE_MAX_CONCURRENT_SESSIONS" in os.environ:
        del os.environ["CASCADE_MAX_CONCURRENT_SESSIONS"]


@pytest.fixture
def mock_pipeline_dependencies():
    """Mock backend dependencies so auth tests can focus on the WS handshake."""
    with (
        patch("backend.main.get_api_keys", return_value=SimpleNamespace(deepgram="dg", groq="gq")),
        patch(
            "backend.main.get_model_config",
            return_value=SimpleNamespace(
                deepgram_model="nova-2",
                groq_model="llama",
                edge_tts_voice="en-US-AriaNeural",
                deepgram_tts_model="aura-asteria-en",
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
    with client.websocket_connect("/ws") as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "challenge"
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]

    # 2. Incorrect HMAC response
    with client.websocket_connect("/ws") as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "challenge"
        websocket.send_json({"type": "auth", "response": "wrong-secret"})
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]


def test_websocket_auth_authorized(mock_pipeline_dependencies):
    """Verify WebSocket connection succeeds when correct CASCADE_AUTH_SECRET is sent."""
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        _authenticate_websocket(websocket)
        msg = websocket.receive_json()
        assert msg["type"] == "auth_ok"
        websocket.send_text("stop")


def test_websocket_auth_first_message(mock_pipeline_dependencies):
    """Verify WebSocket connection succeeds using the first-message JSON handshake."""
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
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
        with client.websocket_connect("/ws") as ws1:
            _authenticate_websocket(ws1)
            assert ws1.receive_json()["type"] == "auth_ok"
            # Establish connection 2
            with client.websocket_connect("/ws") as ws2:
                _authenticate_websocket(ws2)
                assert ws2.receive_json()["type"] == "auth_ok"
                # Connection 3 should exceed cap (capacity is 2)
                with client.websocket_connect("/ws") as ws3:
                    msg = ws3.receive_json()
                    assert msg["type"] == "busy"
                    assert "maximum capacity" in msg["message"]
                ws2.send_text("stop")
            ws1.send_text("stop")
    finally:
        # Restore original semaphore
        backend.main.session_semaphore = original_semaphore
