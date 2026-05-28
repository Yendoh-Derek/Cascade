"""
cascade/tests/verify_all.py

Master verification runner for Phase 1.

Runs all three API verification checks in sequence (STT, LLM, TTS)
and produces a final summary report. Exit code 0 = all passed,
exit code 1 = one or more failed.

Usage:
    python tests/verify_all.py
"""

import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from tests.test_stt import run as verify_stt
from tests.test_llm import run as verify_llm
from tests.test_tts import run as verify_tts


DIVIDER = "═" * 56


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
        "tts": "OpenAI TTS       ",
    }

    for key, label in labels.items():
        status = results.get(key)
        if status is True:
            icon = "✓"
            text = "PASSED"
        elif status is False:
            icon = "✗"
            text = "FAILED"
            all_passed = False
        else:
            icon = "–"
            text = "SKIPPED"
            all_passed = False

        print(f"  {icon}  {label}  {text}")

    print(f"\n  Completed in {elapsed:.2f}s")
    print(f"{DIVIDER}")

    if all_passed:
        print("  ✓  All checks passed. Ready to build Phase 2.\n")
    else:
        print("  ✗  Some checks failed. Fix the issues above before")
        print("     proceeding to Phase 2.\n")
        print("  Tip: Make sure your .env file is filled in correctly.")
        print("       Copy .env.example → .env and add your API keys.\n")

    return all_passed


def run() -> bool:
    print_header()

    results = {}
    start = time.perf_counter()

    # Deepgram STT
    try:
        results["stt"] = verify_stt()
    except Exception as e:
        print(f"  ✗ STT verification crashed unexpectedly: {e}\n")
        results["stt"] = False

    # Groq LLM
    try:
        results["llm"] = verify_llm()
    except Exception as e:
        print(f"  ✗ LLM verification crashed unexpectedly: {e}\n")
        results["llm"] = False

    # OpenAI TTS
    try:
        results["tts"] = verify_tts()
    except Exception as e:
        print(f"  ✗ TTS verification crashed unexpectedly: {e}\n")
        results["tts"] = False

    elapsed = time.perf_counter() - start
    all_passed = print_summary(results, elapsed)
    return all_passed


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
