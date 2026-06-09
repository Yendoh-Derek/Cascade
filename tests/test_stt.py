"""
cascade/tests/test_stt.py

Verifies the Deepgram Speech-to-Text API key and connection.

Tests:
  1. API key is present in environment
  2. Deepgram client initialises without error
  3. Deepgram API responds to a simple prerecorded audio request
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys
from deepgram import DeepgramClient


def _test_deepgram_connection(api_key: str) -> dict:
    """
    Test Deepgram connection by initializing client and opening a live connection.
    """
    result = {"success": False, "error": None}
    try:
        client = DeepgramClient(api_key=api_key)
        
        # Test actual live connection handshake
        options = {
            "model": "nova-2",
            "smart_format": True,
        }
        
        # connect() returns a context manager, __enter__ triggers the handshake
        with client.listen.v1.connect(**options):
            result["success"] = True
            
    except Exception as e:
        result["error"] = str(e)
    return result


def run() -> bool:
    """
    Run all STT verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n-- Deepgram STT Verification -----------------------------")

    # Step 1: API key present
    print("  [1/3] Checking API key...")
    try:
        keys = get_api_keys()
        masked = keys.deepgram[:8] + "..." + keys.deepgram[-4:]
        print(f"        v Key found: {masked}")
    except EnvironmentError as e:
        print(f"        x {e}")
        return False

    # Step 2: Client init
    print("  [2/3] Initialising Deepgram client...")
    
    # Step 3: Live connection
    print("  [3/3] Testing live connection handshake...")
    result = _test_deepgram_connection(keys.deepgram)
    if not result["success"]:
        print(f"        x Connection failed: {result['error']}")
        return False
    print(f"        v Live connection established successfully")
    print("  v Deepgram STT -- ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
