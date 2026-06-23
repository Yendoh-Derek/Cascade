import asyncio
import json
import websockets

async def run_diagnostics():
    uri = "ws://localhost:8000/ws?tts_engine=edge"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected! Waiting for welcome or tts_config...")
            # Wait for first metadata message (tts_config)
            try:
                msg = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                print("Received first message:", msg)
            except asyncio.TimeoutError:
                print("Timeout waiting for welcome/tts_config message!")
            
            # Send 5 seconds of blank 16kHz 16-bit mono PCM audio (32000 bytes per second)
            print("Sending 5 seconds of blank audio...")
            chunk = b"\x00" * 640  # 20ms frame at 16kHz
            for _ in range(250):
                await websocket.send(chunk)
                await asyncio.sleep(0.02)
            
            print("Finished sending audio. Sending finalize...")
            # Send finalize signal
            await websocket.send(json.dumps({"type": "finalize"}))
            
            print("Waiting for transcript or response chunks...")
            # Keep reading for 5 seconds to print incoming messages
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    print("Received server message:", msg[:500])
                except asyncio.TimeoutError:
                    print("No more messages received (timeout).")
                    break
    except Exception as e:
        print("WebSocket connection failed:", e)

if __name__ == "__main__":
    asyncio.run(run_diagnostics())
