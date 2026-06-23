"""
cascade/tests/test_tts.py

Verifies the edge-tts Text-to-Speech engine and audio output.

Tests:
  1. edge-tts package is importable
  2. Requested voice is available in the voice list
  3. Audio is generated and streamed correctly
"""

import sys
import os
import asyncio
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_model_config

PROBE_TEXT = "Hello! I am your AI tutor. I am ready to help you learn."
MIN_AUDIO_BYTES = 1024


async def _fetch_available_voices() -> list:
    """Retrieve the full list of voices available from edge-tts."""
    import edge_tts
    voices = await edge_tts.list_voices()
    return [v["ShortName"] for v in voices]


async def _test_streaming_tts(voice: str) -> dict:
    """Generate audio via edge-tts and confirm chunks stream correctly."""
    import edge_tts

    result = {
        "success": False,
        "chunk_count": 0,
        "total_bytes": 0,
        "first_chunk_ms": None,
        "error": None,
    }
    try:
        communicate = edge_tts.Communicate(PROBE_TEXT, voice)
        start = time.perf_counter()

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data = chunk["data"]
                if result["first_chunk_ms"] is None:
                    result["first_chunk_ms"] = round(
                        (time.perf_counter() - start) * 1000
                    )
                result["chunk_count"] += 1
                result["total_bytes"] += len(data)

        result["success"] = (
            result["chunk_count"] > 0
            and result["total_bytes"] >= MIN_AUDIO_BYTES
        )
        if not result["success"]:
            result["error"] = (
                f"Stream produced {result['chunk_count']} chunks, "
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
    print("\n-- Edge-TTS Verification ---------------------------------")

    # Step 1: Package import
    print("  [1/3] Checking edge-tts package...")
    try:
        import edge_tts  # noqa: F401
        print("        v edge-tts imported successfully")
    except ImportError:
        print("        x edge-tts not installed. Run: pip install edge-tts")
        return False

    # Step 2: Voice availability
    config = get_model_config()
    voice = config.edge_tts_voice
    print(f"  [2/3] Verifying voice '{voice}' is available...")
    try:
        available = asyncio.run(_fetch_available_voices())
        if voice not in available:
            print(f"        x Voice '{voice}' not found.")
            print("        Available English voices (sample):")
            en_voices = [v for v in available if v.startswith("en-US")][:5]
            for v in en_voices:
                print(f"          - {v}")
            return False
        print("        v Voice confirmed available")
    except Exception as e:
        print(f"        x Could not fetch voice list: {e}")
        return False

    # Step 3: Streaming audio
    print("  [3/3] Testing streaming audio generation...")
    result = asyncio.run(_test_streaming_tts(voice))
    if not result["success"]:
        print(f"        x Streaming failed: {result['error']}")
        return False
    print(f"        v First chunk in {result['first_chunk_ms']}ms")
    print(f"        v {result['chunk_count']} chunks, {result['total_bytes']:,} bytes total")
    print("  v Edge-TTS - ALL CHECKS PASSED\n")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
