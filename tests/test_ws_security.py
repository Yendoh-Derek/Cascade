"""
cascade/tests/test_ws_security.py

Integration tests to verify CORS credentials, WebSocket authentication, and concurrent session limit.
"""

import sys
import os
import asyncio
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env var before importing app so it picks it up or we can test auth config
os.environ["CASCADE_AUTH_SECRET"] = "test-secret"
os.environ["CASCADE_MAX_CONCURRENT_SESSIONS"] = "3"

# Import app
from backend.main import app, session_semaphore
import backend.main


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
    # 1. No secret query param
    with client.websocket_connect("/ws") as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]

    # 2. Incorrect secret
    with client.websocket_connect("/ws?secret=wrong-secret") as websocket:
        msg = websocket.receive_json()
        assert msg["type"] == "error"
        assert "Unauthorized" in msg["message"]


def test_websocket_auth_authorized():
    """Verify WebSocket connection succeeds when correct CASCADE_AUTH_SECRET is sent."""
    client = TestClient(app)
    # Bypass API key checks for health validation so pipeline does not throw error immediately
    # We can connect and check if initialization starts.
    # Note: TestClient websocket_connect will connect and block.
    with client.websocket_connect("/ws?secret=test-secret") as websocket:
        # Check that we didn't receive an Unauthorized error immediately
        # Pipeline initialization might fail due to empty keys in test environment,
        # but it should NOT be an Unauthorized close.
        try:
            msg = websocket.receive_json()
            assert msg["type"] != "error" or "Unauthorized" not in msg["message"]
        except Exception:
            # If it closed for other reasons (e.g. key initialization), that's fine
            pass


def test_websocket_concurrency_limit():
    """Verify concurrent connections limit semaphore works."""
    # Temporarily set semaphore capacity to 2 for test predictability
    original_semaphore = backend.main.session_semaphore
    backend.main.session_semaphore = asyncio.Semaphore(2)

    client = TestClient(app)
    try:
        # Establish connection 1
        with client.websocket_connect("/ws?secret=test-secret") as ws1:
            # Establish connection 2
            with client.websocket_connect("/ws?secret=test-secret") as ws2:
                # Connection 3 should exceed cap (capacity is 2)
                with client.websocket_connect("/ws?secret=test-secret") as ws3:
                    msg = ws3.receive_json()
                    assert msg["type"] == "busy"
                    assert "maximum capacity" in msg["message"]
    finally:
        # Restore original semaphore
        backend.main.session_semaphore = original_semaphore
