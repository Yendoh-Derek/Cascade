"""
cascade/tests/test_tts.py

Verifies the ElevenLabs Text-to-Speech API key, voice ID, and audio output.

Tests:
  1. API key and voice ID are present in environment
  2. A standard TTS request produces valid audio bytes
  3. A streaming TTS request delivers audio chunks correctly
"""

import sys
import os
import time
import urllib.request
import urllib.error
import json

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys, get_model_config

PROBE_TEXT = "Hello! I am your AI tutor. I am ready to help you learn."
MIN_AUDIO_BYTES = 1024
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


def _make_headers(api_key: str) -> dict:
    return {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }


def _make_payload(text: str, model_id: str) -> bytes:
    return json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }).encode("utf-8")


def _test_standard_tts(api_key: str, voice_id: str, model_id: str) -> dict:
    """Request a standard TTS audio response and validate the output."""
    result = {
        "success": False,
        "audio_bytes": 0,
        "latency_ms": None,
        "error": None,
    }
    try:
        url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}"
        req = urllib.request.Request(
            url,
            data=_make_payload(PROBE_TEXT, model_id),
            headers=_make_headers(api_key),
            method="POST",
        )
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as response:
            audio_data = response.read()
        elapsed = (time.perf_counter() - start) * 1000

        result["audio_bytes"] = len(audio_data)
        result["latency_ms"] = round(elapsed)
        result["success"] = result["audio_bytes"] >= MIN_AUDIO_BYTES

        if not result["success"]:
            result["error"] = (
                f"Audio too small: {result['audio_bytes']} bytes "
                f"(expected >= {MIN_AUDIO_BYTES})"
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        result["error"] = f"HTTP {e.code}: {body}"
    except Exception as e:
        result["error"] = str(e)
    return result


def _test_streaming_tts(api_key: str, voice_id: str, model_id: str) -> dict:
    """Request a streaming TTS response and confirm chunks arrive."""
    result = {
        "success": False,
        "chunk_count": 0,
        "total_bytes": 0,
        "first_chunk_ms": None,
        "error": None,
    }
    try:
        url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}/stream"
        req = urllib.request.Request(
            url,
            data=_make_payload(PROBE_TEXT, model_id),
            headers=_make_headers(api_key),
            method="POST",
        )
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as response:
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
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
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        result["error"] = f"HTTP {e.code}: {body}"
    except Exception as e:
        result["error"] = str(e)
    return result


def run() -> bool:
    """
    Run all TTS verification checks.
    Returns True if all pass, False otherwise.
    """
    print("\n── ElevenLabs TTS Verification ───────────────────────────")

    # Step 1: API key + voice ID
    print("  [1/3] Checking API key and Voice ID...")
    try:
        keys = get_api_keys()
        config = get_model_config()
        masked_key = keys.elevenlabs[:8] + "..." + keys.elevenlabs[-4:]
        masked_voice = config.elevenlabs_voice_id[:6] + "..."
        print(f"        ✓ API key found:  {masked_key}")
        print(f"        ✓ Voice ID found: {masked_voice}")
    except EnvironmentError as e:
        print(f"        ✗ {e}")
        return False

    # Step 2: Standard TTS
    model = config.elevenlabs_model
    print(f"  [2/3] Testing standard TTS (model: {model})...")
    std = _test_standard_tts(keys.elevenlabs, config.elevenlabs_voice_id, model)
    if not std["success"]:
        print(f"        ✗ TTS failed: {std['error']}")
        return False
    print(f"        ✓ Audio received in {std['latency_ms']}ms")
    print(f"        ✓ Audio size: {std['audio_bytes']:,} bytes")

    # Step 3: Streaming TTS
    print("  [3/3] Testing streaming TTS...")
    stream = _test_streaming_tts(keys.elevenlabs, config.elevenlabs_voice_id, model)
    if not stream["success"]:
        print(f"        ✗ Streaming failed: {stream['error']}")
        return False
    print(f"        ✓ First chunk in {stream['first_chunk_ms']}ms")
    print(f"        ✓ {stream['chunk_count']} chunks, {stream['total_bytes']:,} bytes total")
    print("  ✓ ElevenLabs TTS — ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
