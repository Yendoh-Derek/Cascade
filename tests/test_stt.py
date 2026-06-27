import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.config import get_api_keys
from backend.stt import STTHandler, MAX_RECONNECT_ATTEMPTS


@pytest.mark.asyncio
async def test_stt_reconnect_backoff():
    """Test that STT reconnects with exponential backoff on connection drop."""
    on_transcript = MagicMock()
    on_error = MagicMock()
    on_status = MagicMock()

    handler = STTHandler(
        api_key="test_key",
        on_transcript=on_transcript,
        on_error=on_error,
        on_status=on_status
    )

    with patch("aiohttp.ClientSession") as mock_session_class, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session_class.return_value = mock_session

        # First connection fails, second fails, third succeeds
        call_count = 0
        async def mock_ws_connect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("Connection failed")
            return AsyncMock()

        mock_session.ws_connect = mock_ws_connect

        # Establish connection manually trigger the reconnect loop
        # We simulate the _listen loop failing
        handler._closing_intentionally = False
        await handler._reconnect_loop()

        # Should have attempted MAX_RECONNECT_ATTEMPTS or until success
        # We configured 2 failures then 1 success.
        assert call_count == 3
        
        # Verify sleep was called with backoff
        # attempt 1: delay 1
        # attempt 2: delay 2
        # attempt 3: delay 4 (but attempt 3 succeeded so we only sleep before attempt 1 and 2, wait, the loop sleeps BEFORE attempt!)
        # The code does:
        # for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        #   delay = 2 ** (attempt - 1)
        #   await asyncio.sleep(delay)
        
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)
        mock_sleep.assert_any_call(4)

        # Status should have been called
        on_status.assert_any_call("stt_reconnecting", {"attempt": 1, "max": MAX_RECONNECT_ATTEMPTS})
        on_status.assert_any_call("stt_reconnecting", {"attempt": 2, "max": MAX_RECONNECT_ATTEMPTS})
        on_status.assert_any_call("stt_reconnecting", {"attempt": 3, "max": MAX_RECONNECT_ATTEMPTS})
        on_status.assert_any_call("stt_reconnected", {})


@pytest.mark.asyncio
async def test_stt_reconnect_exhausted():
    """Test that STT emits error when all reconnect attempts fail."""
    on_transcript = MagicMock()
    on_error = MagicMock()
    on_status = MagicMock()

    handler = STTHandler(
        api_key="test_key",
        on_transcript=on_transcript,
        on_error=on_error,
        on_status=on_status
    )

    with patch("aiohttp.ClientSession") as mock_session_class, \
         patch("asyncio.sleep", new_callable=AsyncMock) as _:

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session_class.return_value = mock_session

        # All connections fail
        async def mock_ws_connect(*args, **kwargs):
            raise Exception("Connection failed")

        mock_session.ws_connect = mock_ws_connect

        handler._closing_intentionally = False
        await handler._reconnect_loop()

        assert on_error.call_count == 1
        on_error.assert_called_with("STT connection lost — please start a new session")


# --- Live Verification for verify_all.py ---

async def _test_deepgram_connection(api_key: str) -> dict:
    """
    Test Deepgram connection using our STTHandler.
    """
    result = {"success": False, "error": None}
    handler = None
    try:
        def dummy_transcript(_):
            pass
        
        def dummy_error(_):
            pass
        
        handler = STTHandler(
            api_key=api_key,
            on_transcript=dummy_transcript,
            on_error=dummy_error
        )
        await handler.connect()
        await asyncio.sleep(0.5)  # Give it a moment to connect
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        if handler:
            await handler.close()
    return result


def run() -> bool:
    """
    Run all STT verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n-- Deepgram STT Verification -----------------------------")

    # Step 1: API key present
    print("  [1/2] Checking API key...")
    try:
        keys = get_api_keys()
        masked = keys.deepgram[:8] + "..." + keys.deepgram[-4:]
        print(f"        v Key found: {masked}")
    except EnvironmentError as e:
        print(f"        x {e}")
        return False

    # Step 2: Live connection
    print("  [2/2] Testing live connection handshake...")
    result = asyncio.run(_test_deepgram_connection(keys.deepgram))
    if not result["success"]:
        print(f"        x Connection failed: {result['error']}")
        return False
    print("        v Live connection established successfully")
    print("  v Deepgram STT -- ALL CHECKS PASSED\n")
    return True

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
