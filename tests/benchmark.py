import asyncio
import time
import sys
import os
import platform
import statistics
from typing import List, Dict

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from backend.config import get_api_keys, get_model_config

def print_header(title: str):
    print("\n" + "=" * 60)
    print(f"  CASCADE PERFORMANCE BENCHMARK: {title}")
    print("=" * 60)

def print_env_info():
    config = get_model_config()
    print(f"  OS Platform:      {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  Python Version:   {platform.python_version()}")
    print(f"  STT Model:        {config.deepgram_model}")
    print(f"  LLM Model:        {config.groq_model}")
    print(f"  Edge TTS Voice:   {config.edge_tts_voice}")
    print(f"  Deepgram Model:   {config.deepgram_tts_model}")
    print("-" * 60)

def calculate_percentiles(data: List[float]) -> Dict[str, float]:
    if not data:
        return {"avg": 0, "p50": 0, "p90": 0, "p95": 0}
    sorted_data = sorted(data)
    n = len(sorted_data)
    return {
        "avg": statistics.mean(sorted_data),
        "p50": statistics.median(sorted_data),
        "p90": sorted_data[int(n * 0.90)],
        "p95": sorted_data[int(n * 0.95)] if n > 1 else sorted_data[-1]
    }

async def mock_benchmark_run(trials: int = 5):
    print(f"Running {trials} mock benchmark trials to simulate pipeline processing...")
    
    stt_latencies = []
    ttft_latencies = []
    streaming_delay_latencies = []
    tts_latencies = []
    e2e_latencies = []

    # Simulate trials
    for i in range(1, trials + 1):
        print(f"  Trial {i}/{trials}...", end="\r")
        await asyncio.sleep(0.1) # Yield to event loop
        
        # Simulate realistic latencies based on live averages
        stt = 400 + (time.time() % 3) * 50    # 400-550ms (Deepgram endpointing)
        ttft = 180 + (time.time() % 4) * 40   # 180-340ms (Groq TTFT)
        streaming = 80 + (time.time() % 2) * 30 # 80-140ms (sentence buffer window)
        tts = 150 + (time.time() % 3) * 60    # 150-330ms (first audio chunk)
        
        # E2E E2E is measured from STT final to first audio byte
        # In cascade this is queue + ttft + streaming + tts
        e2e = ttft + streaming + tts
        
        stt_latencies.append(stt)
        ttft_latencies.append(ttft)
        streaming_delay_latencies.append(streaming)
        tts_latencies.append(tts)
        e2e_latencies.append(e2e)
        
    print("\nBenchmark runs completed successfully.")
    
    metrics = {
        "Deepgram STT": stt_latencies,
        "Groq LLM TTFT": ttft_latencies,
        "LLM Stream Delay": streaming_delay_latencies,
        "TTS First Byte": tts_latencies,
        "End-to-End Latency": e2e_latencies
    }
    
    print("\nLATENCY REPORT (ms)")
    print("-" * 60)
    print(f"{'Metric':<25} | {'Average':<8} | {'Median':<8} | {'P90':<8} | {'P95':<8}")
    print("-" * 60)
    for name, values in metrics.items():
        stats = calculate_percentiles(values)
        print(f"{name:<25} | {stats['avg']:<8.1f} | {stats['p50']:<8.1f} | {stats['p90']:<8.1f} | {stats['p95']:<8.1f}")
    print("=" * 60)

async def main():
    print_header("MOCK EXECUTION")
    try:
        get_api_keys()
        print_env_info()
    except EnvironmentError as e:
        print(f"Warning: Environment keys not configured ({e}). Running with default mock configs.")
    
    await mock_benchmark_run()

if __name__ == "__main__":
    asyncio.run(main())
