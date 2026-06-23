"""
cascade/tests/test_stt.py

Verifies the Deepgram Speech-to-Text API key and connection using our new STTHandler.

Tests:
  1. API key is present in environment
  2. STTHandler connects successfully
"""

import sys
import os
import asyncio

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys
from backend.stt import STTHandler


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
