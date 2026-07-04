"""
tests/benchmark.py

CASCADE TRUE TTFA PERFORMANCE BENCHMARK

Runs controlled, repeated trials over a live WebSocket connection and reports:
  - Steady-state Time To First Audio Byte (TTFB) broken down by pipeline stage
  - Estimated TTFA (TTFB + documented browser decode/hardware offset)
  - Barge-in / Interruption latency as a separate metric

Usage:
    python tests/benchmark.py [--trials N] [--barge-trials M] [--host HOST]

Requirements:
    pip install websockets httpx
"""

import asyncio
import json
import time
import statistics
import websockets
import httpx
import argparse
import sys
import os
import platform
from typing import List, Dict, Optional

# Force UTF-8 output on Windows (default console uses cp1252 which chokes on
# box-drawing chars). If reconfigure is unavailable, fall back silently.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from backend.config import get_api_keys, get_model_config

SEP  = "-" * 65
SEP2 = "=" * 65

# ─── Helpers ──────────────────────────────────────────────────────────────────

def calculate_percentiles(data: List[float]) -> Dict[str, float]:
    if not data:
        return {"avg": 0, "p50": 0, "p90": 0, "p95": 0, "n": 0}
    sorted_data = sorted(data)
    n = len(sorted_data)
    return {
        "n":   n,
        "avg": statistics.mean(sorted_data),
        "p50": statistics.median(sorted_data),
        "p90": sorted_data[min(int(n * 0.90), n - 1)],
        "p95": sorted_data[min(int(n * 0.95), n - 1)],
    }

def print_percentiles(name: str, stats: Dict[str, float]):
    print(f"{name:<26} | {stats['avg']:<8.0f} | {stats['p50']:<8.0f} | {stats['p90']:<8.0f} | {stats['p95']:<8.0f}")

def print_env_info(host: str, tts: str):
    cfg = get_model_config()
    tts_label = "Deepgram Aura" if tts == "deepgram" else "Edge-TTS (fallback)"
    print(f"  Host:          {host}")
    print(f"  Origin:        {platform.node()} ({platform.system()} {platform.machine()})")
    print(f"  STT:           Deepgram {cfg.deepgram_model} / {cfg.stt_endpointing_ms}ms endpointing")
    print(f"  LLM:           {cfg.groq_model} (via Groq)")
    print(f"  TTS:           {tts_label}")
    print("-" * 65)

async def generate_synthetic_audio(text: str, api_key: str) -> bytes:
    """Synthesise a fixed PCM16 utterance via Deepgram TTS for use as a test fixture."""
    url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=linear16&sample_rate=16000"
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json={"text": text}, timeout=15.0)
        response.raise_for_status()
        return response.content


async def _stream_audio(websocket, audio_bytes: bytes, chunk_size: int = 3200, pace_ms: int = 10):
    """Stream PCM audio in chunks, mimicking a real microphone at 10ms pace."""
    for j in range(0, len(audio_bytes), chunk_size):
        await websocket.send(audio_bytes[j : j + chunk_size])
        await asyncio.sleep(pace_ms / 1000)
    await websocket.send(json.dumps({"type": "finalize", "reason": "local_vad"}))


# ─── Steady-State TTFA Trials ──────────────────────────────────────────────────

async def run_steady_state_trials(uri: str, audio_bytes: bytes, num_trials: int = 30):
    print(f"\n--- Steady-State TTFA: {num_trials} trials ---\n")

    stt_ms_list, llm_queue_list, llm_ttft_list = [], [], []
    llm_stream_list, tts_list, ttfb_list, sys_list = [], [], [], []

    try:
        async with websockets.connect(uri, max_size=None) as ws:
            # Consume tts_config welcome frame
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            for i in range(1, num_trials + 1):
                print(f"  Trial {i}/{num_trials}...", end="\r", flush=True)

                t_finalize_sent: Optional[float] = None
                t_first_audio: Optional[float] = None
                turn_latency: Optional[dict] = None
                turn_llm: Optional[dict] = None

                async def send_turn():
                    nonlocal t_finalize_sent
                    await _stream_audio(ws, audio_bytes)
                    t_finalize_sent = time.perf_counter()

                async def recv_turn():
                    nonlocal t_first_audio, turn_latency, turn_llm
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            print(f"\n  [warn] Trial {i}: recv timeout.")
                            break
                        if isinstance(raw, bytes):
                            if t_first_audio is None:
                                t_first_audio = time.perf_counter()
                            # Once we have first audio AND both metric messages,
                            # we have everything we need — exit without waiting
                            # for the full TTS response stream to finish.
                            if t_first_audio and turn_latency and turn_llm:
                                break
                        else:
                            msg = json.loads(raw)
                            t = msg.get("type")
                            if t == "llm_metrics":
                                turn_llm = msg
                            elif t == "latency":
                                turn_latency = msg
                            elif t == "response_end":
                                break

                # Run send + recv concurrently so backpressure never stalls either side.
                # Hard 45s per-trial wall-clock timeout in case the server hangs.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(send_turn(), recv_turn()),
                        timeout=45.0
                    )
                except asyncio.TimeoutError:
                    print(f"\n  [warn] Trial {i}: 45s wall-clock timeout — recording partial data.")

                if turn_latency and turn_llm and t_first_audio and t_finalize_sent:
                    ttfb = (t_first_audio - t_finalize_sent) * 1000
                    stt  = turn_latency.get("stt_tail_ms", 0)
                    tts  = turn_latency.get("tts_ms", 0)
                    q    = turn_llm.get("queue_ms", 0)
                    ttft = turn_llm.get("ttft_ms", 0)
                    strm = turn_llm.get("streaming_delay_ms", 0)
                    sys_over = max(0, ttfb - (stt + q + ttft + strm + tts))

                    stt_ms_list.append(stt)
                    llm_queue_list.append(q)
                    llm_ttft_list.append(ttft)
                    llm_stream_list.append(strm)
                    tts_list.append(tts)
                    sys_list.append(sys_over)
                    ttfb_list.append(ttfb)
                else:
                    print(f"\n  [skip] Trial {i}: incomplete data — {turn_latency=}, {turn_llm=}, {t_first_audio=}")

                # Drain any stale TTS bytes the server is still streaming for this
                # turn. Without this, they corrupt the next trial's t_first_audio.
                while True:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=1.5)
                    except asyncio.TimeoutError:
                        break  # Connection is quiet — safe to start next trial

    except Exception as e:
        print(f"\nError: {e}")

    n = len(ttfb_list)
    print(f"\n\n{'─'*65}".replace("─", "-"))
    print(f"  STEADY-STATE TTFB REPORT ({n}/{num_trials} valid trials, ms)")
    print(f"{'─'*65}")
    print(f"{'Metric':<26} | {'Avg':<8} | {'P50':<8} | {'P90':<8} | {'P95':<8}")
    print(f"{'─'*65}")
    print_percentiles("STT pipeline tail",       calculate_percentiles(stt_ms_list))
    print_percentiles("LLM queue + schedule",    calculate_percentiles(llm_queue_list))
    print_percentiles("LLM TTFT",                calculate_percentiles(llm_ttft_list))
    print_percentiles("LLM streaming delay",     calculate_percentiles(llm_stream_list))
    print_percentiles("TTS first byte",          calculate_percentiles(tts_list))
    print_percentiles("System / transit",        calculate_percentiles(sys_list))
    print(f"{'─'*65}")
    print_percentiles("Network TTFB (measured)", calculate_percentiles(ttfb_list))
    ttfa_list = [v + 75 for v in ttfb_list]
    print_percentiles("Est. TTFA (+75ms) *",     calculate_percentiles(ttfa_list))
    print(f"{'═'*65}")
    print("* TTFB = time from finalize→first audio byte at the script.")
    print("  True browser TTFA adds ~75ms (decode + hardware output buffer).")
    print("  Report both; let the reader add them.")


# ─── Barge-In / Interruption Trials ───────────────────────────────────────────

async def run_barge_in_trials(
    uri: str,
    initial_audio: bytes,
    interrupt_audio: bytes,
    num_trials: int = 10,
    barge_delay_ms: int = 500,
):
    print(f"\n--- Barge-In Latency: {num_trials} trials (barge at +{barge_delay_ms}ms into TTS) ---\n")

    cancel_ack_list: List[float] = []
    new_audio_list:  List[float] = []

    try:
        async with websockets.connect(uri, max_size=None) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            for i in range(1, num_trials + 1):
                print(f"  Trial {i}/{num_trials}...", end="\r", flush=True)

                t_barge: Optional[float] = None
                t_cancel_ack: Optional[float] = None
                t_new_audio: Optional[float] = None

                # Events for coordinating sender <-> receiver
                first_audio_arrived = asyncio.Event()
                barge_in_sent       = asyncio.Event()

                async def sender():
                    """Send initial utterance, wait for first audio, then barge in."""
                    await _stream_audio(ws, initial_audio)
                    # Wait until receiver sees first TTS audio before barging
                    try:
                        await asyncio.wait_for(first_audio_arrived.wait(), timeout=20.0)
                    except asyncio.TimeoutError:
                        return
                    # Simulate user listening for barge_delay_ms, then interrupt
                    await asyncio.sleep(barge_delay_ms / 1000)
                    nonlocal t_barge
                    t_barge = time.perf_counter()
                    await ws.send(json.dumps({"type": "cancel"}))
                    await _stream_audio(ws, interrupt_audio)
                    barge_in_sent.set()

                async def receiver():
                    """Single loop: old turn → barge-in → new turn.

                    We cannot rely on `turn_cancelled` because it is only emitted
                    when _active_turn_id is still set at cancel() time, which
                    race-conditions out during LLM retries. Instead we use the
                    new turn's `transcript` message as the interruption-ack anchor.
                    Latency definition:
                      cancel_ack_latency = time(cancel sent) → time(new transcript)
                      new_audio_latency  = time(cancel sent) → time(first audio byte
                                           of the new turn)
                    """
                    nonlocal t_cancel_ack, t_new_audio
                    new_turn_transcript_seen = False
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
                        except asyncio.TimeoutError:
                            break
                        if isinstance(raw, bytes):
                            if not first_audio_arrived.is_set():
                                first_audio_arrived.set()
                            elif new_turn_transcript_seen and t_new_audio is None:
                                # First audio byte AFTER new turn's transcript
                                t_new_audio = time.perf_counter()
                                break
                        else:
                            msg = json.loads(raw)
                            mt = msg.get("type")
                            if mt == "transcript" and barge_in_sent.is_set():
                                # New turn's transcript = interruption acknowledged
                                if t_cancel_ack is None:
                                    t_cancel_ack = time.perf_counter()
                                new_turn_transcript_seen = True
                            elif mt == "response_end" and new_turn_transcript_seen:
                                # New turn done but we never got audio — exit cleanly
                                break

                try:
                    await asyncio.wait_for(
                        asyncio.gather(sender(), receiver()),
                        timeout=45.0
                    )
                except asyncio.TimeoutError:
                    print(f"\n  [warn] Trial {i}: 45s overall timeout.")

                if t_barge and t_cancel_ack:
                    cancel_ack_list.append((t_cancel_ack - t_barge) * 1000)
                if t_barge and t_new_audio:
                    new_audio_list.append((t_new_audio - t_barge) * 1000)


                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"\nError: {e}")

    n = len(new_audio_list)
    print(f"\n\n{'─'*65}".replace("─", "-"))
    print(f"  BARGE-IN / INTERRUPTION REPORT ({n}/{num_trials} valid trials, ms)")
    print(f"{'─'*65}")
    print(f"{'Metric':<26} | {'Avg':<8} | {'P50':<8} | {'P90':<8} | {'P95':<8}")
    print(f"{'─'*65}")
    print_percentiles("Cancel ack latency",    calculate_percentiles(cancel_ack_list))
    print_percentiles("New audio turnaround",  calculate_percentiles(new_audio_list))
    print(f"{'=' * 65}")
    print("Cancel ack = time from client cancel → new turn transcript received.")
    print("New audio  = time from client cancel → first audio byte of new turn.")


# ─── Entry Point ───────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Cascade TTFA Benchmark")
    parser.add_argument("--trials",       type=int, default=30,            help="Steady-state trial count (≥30 for p95)")
    parser.add_argument("--barge-trials", type=int, default=10,            help="Barge-in trial count")
    parser.add_argument("--barge-delay",  type=int, default=500,           help="ms into TTS playback before barge-in")
    parser.add_argument("--host",         type=str, default="localhost:8000", help="Backend host:port")
    parser.add_argument("--tts",          type=str, default="deepgram",   help="TTS engine: deepgram (default) or edge")
    args = parser.parse_args()

    api_keys = get_api_keys()

    print("\n" + "═" * 65)
    print("  CASCADE TRUE TTFA BENCHMARK")
    print("═" * 65)
    print_env_info(args.host, args.tts)

    print("  Generating test audio fixtures via Deepgram TTS...", end="", flush=True)
    # Keep utterances very short (1 sentence) so Edge-TTS responses
    # finish in a few seconds and don't stall the benchmark.
    audio_short = await generate_synthetic_audio(
        "Hi, what time is it?", api_keys.deepgram
    )
    # audio_long: use a simple, short question so the LLM gives a brief but
    # audible response (avoids Groq rate-limiting on long counting responses).
    audio_long = await generate_synthetic_audio(
        "What is your name?", api_keys.deepgram
    )
    print(f" done ({len(audio_short)//2000:.1f}s + {len(audio_long)//2000:.1f}s PCM)")

    uri = f"ws://{args.host}/ws?tts_engine={args.tts}"
    print(f"  WebSocket URI: {uri}")

    if args.trials > 0:
        await run_steady_state_trials(uri, audio_short, num_trials=args.trials)

    if args.barge_trials > 0:
        await run_barge_in_trials(
            uri, audio_long, audio_short,
            num_trials=args.barge_trials,
            barge_delay_ms=args.barge_delay,
        )

    print("\nBenchmark complete.\n")


if __name__ == "__main__":
    asyncio.run(main())
