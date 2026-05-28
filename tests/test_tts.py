"""
cascade/tests/test_tts.py

Verifies the OpenAI Text-to-Speech API key, connection, and audio output.

Tests:
  1. API key is present in environment
  2. OpenAI client initialises without error
  3. A TTS request produces valid audio bytes
  4. Streaming TTS delivers audio chunks correctly
"""

import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys, model_config
from openai import OpenAI

PROBE_TEXT = "Hello! I am your AI tutor. I am ready to help you learn."
MIN_AUDIO_BYTES = 1024  # A valid audio response should be at least 1KB


def _test_standard_tts(client: OpenAI, model: str, voice: str) -> dict:
    """Request a standard TTS audio response and validate the output."""
    result = {
        "success": False,
        "audio_bytes": 0,
        "latency_ms": None,
        "error": None,
    }
    try:
        start = time.perf_counter()
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=PROBE_TEXT,
            response_format="mp3",
        )
        elapsed = (time.perf_counter() - start) * 1000
        audio_data = response.content
        result["audio_bytes"] = len(audio_data)
        result["latency_ms"] = round(elapsed)
        result["success"] = result["audio_bytes"] >= MIN_AUDIO_BYTES
        if not result["success"]:
            result["error"] = (
                f"Audio response too small: {result['audio_bytes']} bytes "
                f"(expected >= {MIN_AUDIO_BYTES})"
            )
    except Exception as e:
        result["error"] = str(e)
    return result


def _test_streaming_tts(client: OpenAI, model: str, voice: str) -> dict:
    """Request a streaming TTS response and confirm chunks arrive."""
    result = {
        "success": False,
        "chunk_count": 0,
        "total_bytes": 0,
        "first_chunk_ms": None,
        "error": None,
    }
    try:
        start = time.perf_counter()
        with client.audio.speech.with_streaming_response.create(
            model=model,
            voice=voice,
            input=PROBE_TEXT,
            response_format="mp3",
        ) as response:
            for chunk in response.iter_bytes(chunk_size=4096):
                if chunk:
                    if result["first_chunk_ms"] is None:
                        result["first_chunk_ms"] = round(
                            (time.perf_counter() - start) * 1000
                        )
                    result["chunk_count"] += 1
                    result["total_bytes"] += len(chunk)

        result["success"] = (
            result["chunk_count"] > 0
            and result["total_bytes"] >= MIN_AUDIO_BYTES
        )
        if not result["success"]:
            result["error"] = (
                f"Streaming produced {result['chunk_count']} chunks, "
                f"{result['total_bytes']} bytes"
            )
    except Exception as e:
        result["error"] = str(e)
    return result


def run() -> bool:
    """
    Run all TTS verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n── OpenAI TTS Verification ───────────────────────────────")

    # Step 1: API key
    print("  [1/4] Checking API key...")
    try:
        keys = get_api_keys()
        masked = keys.openai[:8] + "..." + keys.openai[-4:]
        print(f"        ✓ Key found: {masked}")
    except EnvironmentError as e:
        print(f"        ✗ {e}")
        return False

    # Step 2: Client init
    print("  [2/4] Initialising OpenAI client...")
    try:
        client = OpenAI(api_key=keys.openai)
        print("        ✓ Client initialised")
    except Exception as e:
        print(f"        ✗ Client failed: {e}")
        return False

    # Step 3: Standard TTS
    model = model_config.openai_tts_model
    voice = model_config.openai_tts_voice
    print(f"  [3/4] Testing standard TTS (model: {model}, voice: {voice})...")
    std = _test_standard_tts(client, model, voice)
    if not std["success"]:
        print(f"        ✗ TTS failed: {std['error']}")
        return False
    print(f"        ✓ Audio received in {std['latency_ms']}ms")
    print(f"        ✓ Audio size: {std['audio_bytes']:,} bytes")

    # Step 4: Streaming TTS
    print("  [4/4] Testing streaming TTS...")
    stream = _test_streaming_tts(client, model, voice)
    if not stream["success"]:
        print(f"        ✗ Streaming failed: {stream['error']}")
        return False
    print(f"        ✓ First chunk in {stream['first_chunk_ms']}ms")
    print(f"        ✓ {stream['chunk_count']} chunks, {stream['total_bytes']:,} bytes total")
    print("  ✓ OpenAI TTS — ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
