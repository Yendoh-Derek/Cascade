"""
cascade/tests/verify_all.py

Master verification runner for Phase 1.
"""

import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from tests.test_stt import run as verify_stt
from tests.test_llm import run as verify_llm
from tests.test_tts import run as verify_tts

DIVIDER = "=" * 56


def print_header():
    print(f"\n{DIVIDER}")
    print("  CASCADE — Phase 1 API Verification")
    print(f"{DIVIDER}")
    print("  Checking all required API connections...\n")


def print_summary(results: dict, elapsed: float):
    print(f"\n{DIVIDER}")
    print("  VERIFICATION SUMMARY")
    print(f"{DIVIDER}")

    all_passed = True
    labels = {
        "stt": "Deepgram STT     ",
        "llm": "Groq LLM         ",
        "tts": "Edge-TTS         ",  # [L2] was labelled "ElevenLabs TTS"
    }

    for key, label in labels.items():
        status = results.get(key)
        if status is True:
            icon, text = "v", "PASSED"
        elif status is False:
            icon, text = "x", "FAILED"
            all_passed = False
        else:
            icon, text = "–", "SKIPPED"
            all_passed = False
        print(f"  {icon}  {label}  {text}")

    print(f"\n  Completed in {elapsed:.2f}s")
    print(f"{DIVIDER}")

    if all_passed:
        print("  v  All checks passed. Ready to build Phase 2.\n")
    else:
        print("  x  Some checks failed. Fix the issues above.\n")
        print("  Tip: Copy .env.example → .env and add your API keys.\n")

    return all_passed


def run() -> bool:
    print_header()
    results = {}
    start = time.perf_counter()

    for key, label, fn in [
        ("stt", "STT", verify_stt),
        ("llm", "LLM", verify_llm),
        ("tts", "Edge-TTS", verify_tts),  # [L2] fix
    ]:
        try:
            results[key] = fn()
        except Exception as e:
            print(f"  x {label} verification crashed: {e}\n")
            results[key] = False

    elapsed = time.perf_counter() - start
    return print_summary(results, elapsed)


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
