"""
cascade/tests/test_llm.py

Verifies the Groq LLM API key, connection, and streaming capability.

Tests:
  1. API key is present in environment
  2. Groq client initialises without error
  3. A standard (non-streaming) completion succeeds
  4. A streaming completion delivers tokens correctly
"""

import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys, model_config
from groq import Groq

PROBE_MESSAGE = "In one short sentence, what is the Pythagorean theorem?"


def _test_standard_completion(client: Groq, model: str) -> dict:
    """Send a simple non-streaming request and return result."""
    result = {"success": False, "response": None, "latency_ms": None, "error": None}
    try:
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROBE_MESSAGE}],
            max_tokens=100,
            temperature=0.3,
        )
        elapsed = (time.perf_counter() - start) * 1000
        result["success"] = True
        result["response"] = response.choices[0].message.content.strip()
        result["latency_ms"] = round(elapsed)
    except Exception as e:
        result["error"] = str(e)
    return result


def _test_streaming_completion(client: Groq, model: str) -> dict:
    """Send a streaming request and confirm tokens arrive incrementally."""
    result = {
        "success": False,
        "token_count": 0,
        "first_token_ms": None,
        "error": None,
    }
    try:
        start = time.perf_counter()
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROBE_MESSAGE}],
            max_tokens=100,
            temperature=0.3,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                if result["first_token_ms"] is None:
                    result["first_token_ms"] = round(
                        (time.perf_counter() - start) * 1000
                    )
                result["token_count"] += 1

        result["success"] = result["token_count"] > 0
        if not result["success"]:
            result["error"] = "Stream produced zero tokens"

    except Exception as e:
        result["error"] = str(e)
    return result


def run() -> bool:
    """
    Run all LLM verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n── Groq LLM Verification ─────────────────────────────────")

    # Step 1: API key
    print("  [1/4] Checking API key...")
    try:
        keys = get_api_keys()
        masked = keys.groq[:8] + "..." + keys.groq[-4:]
        print(f"        ✓ Key found: {masked}")
    except EnvironmentError as e:
        print(f"        ✗ {e}")
        return False

    # Step 2: Client init
    print("  [2/4] Initialising Groq client...")
    try:
        client = Groq(api_key=keys.groq)
        print("        ✓ Client initialised")
    except Exception as e:
        print(f"        ✗ Client failed: {e}")
        return False

    # Step 3: Standard completion
    model = model_config.groq_model
    print(f"  [3/4] Testing standard completion (model: {model})...")
    std = _test_standard_completion(client, model)
    if not std["success"]:
        print(f"        ✗ Completion failed: {std['error']}")
        return False
    print(f"        ✓ Response received in {std['latency_ms']}ms")
    print(f"        → \"{std['response']}\"")

    # Step 4: Streaming completion
    print("  [4/4] Testing streaming completion...")
    stream = _test_streaming_completion(client, model)
    if not stream["success"]:
        print(f"        ✗ Streaming failed: {stream['error']}")
        return False
    print(f"        ✓ First token in {stream['first_token_ms']}ms")
    print(f"        ✓ {stream['token_count']} tokens received via stream")
    print("  ✓ Groq LLM — ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
