"""
cascade/tests/test_stt.py

Verifies the Deepgram Speech-to-Text API key and connection.

Tests:
  1. API key is present in environment
  2. Deepgram client initialises without error
  3. A live transcription connection can be opened and closed cleanly
"""

import sys
import asyncio
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
)


VERIFICATION_AUDIO_SECONDS = 2  # How long to hold the connection open


async def _open_live_connection(api_key: str) -> dict:
    """
    Open a Deepgram live transcription WebSocket, confirm it connects,
    then close it cleanly. Returns a result dict.
    """
    result = {"connected": False, "error": None}
    connection_event = asyncio.Event()

    try:
        options = DeepgramClientOptions(verbose=False)
        client = DeepgramClient(api_key, options)

        dg_connection = client.listen.asyncwebsocket.v("1")

        async def on_open(self, open_event, **kwargs):
            result["connected"] = True
            connection_event.set()

        async def on_error(self, error, **kwargs):
            result["error"] = str(error)
            connection_event.set()

        dg_connection.on(LiveTranscriptionEvents.Open, on_open)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        live_options = LiveOptions(
            model="nova-2",
            language="en-US",
            sample_rate=16000,
            channels=1,
            encoding="linear16",
        )

        started = await dg_connection.start(live_options)
        if not started:
            result["error"] = "Connection failed to start"
            return result

        # Wait for open event or timeout
        try:
            await asyncio.wait_for(connection_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            result["error"] = "Connection timed out after 10 seconds"
            return result

        # Hold briefly, then close cleanly
        await asyncio.sleep(VERIFICATION_AUDIO_SECONDS)
        await dg_connection.finish()

    except Exception as e:
        result["error"] = str(e)

    return result


def run() -> bool:
    """
    Run all STT verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n── Deepgram STT Verification ─────────────────────────────")

    # Step 1: API key present
    print("  [1/3] Checking API key...")
    try:
        keys = get_api_keys()
        masked = keys.deepgram[:8] + "..." + keys.deepgram[-4:]
        print(f"        ✓ Key found: {masked}")
    except EnvironmentError as e:
        print(f"        ✗ {e}")
        return False

    # Step 2: Client initialisation
    print("  [2/3] Initialising Deepgram client...")
    try:
        options = DeepgramClientOptions(verbose=False)
        DeepgramClient(keys.deepgram, options)
        print("        ✓ Client initialised")
    except Exception as e:
        print(f"        ✗ Client failed: {e}")
        return False

    # Step 3: Live connection
    print("  [3/3] Opening live transcription connection...")
    result = asyncio.run(_open_live_connection(keys.deepgram))

    if result["error"]:
        print(f"        ✗ Connection error: {result['error']}")
        return False

    if not result["connected"]:
        print("        ✗ Connection did not open")
        return False

    print("        ✓ Live connection opened and closed cleanly")
    print("  ✓ Deepgram STT — ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
