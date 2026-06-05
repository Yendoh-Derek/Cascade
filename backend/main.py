"""
cascade/backend/main.py

FastAPI application entry point.

Phase 1: Health check endpoints
Phase 2: WebSocket pipeline for streaming voice agent

Usage:
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
"""

import logging
import json
import time
import asyncio
from typing import Dict, Any
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import get_api_keys, get_model_config, server_config
from backend.pipeline import PipelineSession

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Cascade — AI Voice Tutor",
    description="Low-latency streaming voice pipeline for AI tutoring sessions.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount frontend static files
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")



@app.get("/", tags=["Health"])
def root():
    """Root endpoint — confirms the server is alive."""
    return {
        "project": "Cascade",
        "status": "running",
        "phase": 1,
        "message": "API server is live. Run tests/verify_all.py to verify API keys.",
    }


@app.get("/health", tags=["Health"])
def health():
    """
    Health check endpoint.
    Confirms all API keys are loaded without exposing their values.
    """
    try:
        keys = get_api_keys()
        config = get_model_config()
        key_status = {
            "deepgram": bool(keys.deepgram),
            "groq": bool(keys.groq),
        }
        all_present = all(key_status.values())
        return {
            "status": "healthy" if all_present else "degraded",
            "api_keys_present": key_status,
            "models": {
                "stt": config.deepgram_model,
                "llm": config.groq_model,
                "tts": config.edge_tts_voice,
            },
        }
    except EnvironmentError as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    subject: str = Query(default=None, description="Optional tutoring subject"),
):
    """
    WebSocket endpoint for streaming voice pipeline.

    Protocol:
    - Client sends: raw audio bytes (binary messages)
    - Client sends: "stop" (text message) to end session
    - Server sends: {"type": "audio", "data": hex_encoded_mp3_bytes}
    - Server sends: {"type": "latency", "latency_ms": int}
    - Server sends: {"type": "error", "message": str}
    - Server sends: {"type": "transcript", "text": str}
    
    Safety limits:
    - Max 100MB per audio message (prevents DoS)
    - Connection timeout: 5 minutes idle (prevents resource hoarding)
    """
    # Validate subject parameter
    if subject is not None:
        if not isinstance(subject, str) or len(subject) > 200:
            subject = None  # Ignore invalid subjects
        elif not subject.strip():
            subject = None
    
    try:
        await websocket.accept()
        logger.info(f"[WS] Client connected (subject={subject})")

        # Get API keys and config
        try:
            keys = get_api_keys()
            config = get_model_config()
        except EnvironmentError as e:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"API keys not configured: {str(e)}",
                }
            )
            await websocket.close()
            return

        # Create pipeline session
        session = PipelineSession(
            api_keys={
                "deepgram": keys.deepgram,
                "groq": keys.groq,
            },
            model_config={
                "deepgram_model": config.deepgram_model,
                "groq_model": config.groq_model,
                "edge_tts_voice": config.edge_tts_voice,
            },
            send_message=lambda msg: _send_ws_message(websocket, msg),
            subject=subject,
        )

        # Initialize pipeline with timeout
        try:
            await asyncio.wait_for(session.initialize(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Pipeline initialization timed out",
                }
            )
            await websocket.close()
            return
        except Exception as e:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"Pipeline initialization failed: {str(e)}",
                }
            )
            await websocket.close()
            return

        # Listen for incoming messages with idle timeout
        idle_timeout = 300  # 5 minutes idle timeout
        last_activity = time.time()
        
        try:
            while True:
                try:
                    # Receive message with timeout
                    message = await asyncio.wait_for(
                        websocket.receive(),
                        timeout=idle_timeout
                    )
                    last_activity = time.time()

                    if "bytes" in message:
                        # Audio data from client
                        audio_bytes = message["bytes"]
                        
                        # Safety check: max 100MB per chunk
                        if len(audio_bytes) > 100_000_000:
                            logger.warning(f"[WS] Audio chunk too large: {len(audio_bytes)} bytes")
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": "Audio chunk too large (max 100MB)",
                                }
                            )
                            continue
                        
                        await session.handle_audio(audio_bytes)

                    elif "text" in message:
                        # Text command
                        text = message["text"]
                        if text == "stop":
                            logger.info("[WS] Client requested stop")
                            break
                        else:
                            logger.warning(f"[WS] Unknown text message: {text}")

                except asyncio.TimeoutError:
                    logger.info("[WS] Connection idle timeout")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Connection idle timeout (5 minutes)",
                        }
                    )
                    break

        except WebSocketDisconnect:
            logger.info("[WS] Client disconnected")
        except Exception as e:
            logger.error(f"[WS] Error: {e}")
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"Server error: {str(e)}",
                    }
                )
            except:
                pass  # Client may have disconnected

    except Exception as e:
        logger.error(f"[WS] Connection error: {e}")
    finally:
        # Clean up session
        try:
            if "session" in locals():
                await session.close()
        except Exception as e:
            logger.error(f"[WS] Error during cleanup: {e}")


async def _send_ws_message(websocket: WebSocket, message: Dict[str, Any]):
    """Send a message over WebSocket, handling both JSON and binary."""
    try:
        if message.get("type") == "audio" and "data" in message:
            # Audio chunks are sent as binary data directly
            audio_data = message["data"]
            if isinstance(audio_data, str):
                # If hex-encoded string, convert to bytes
                audio_data = bytes.fromhex(audio_data)
            await websocket.send_bytes(audio_data)
        else:
            # All other messages (transcript, response_chunk, latency, error, etc) as JSON
            await websocket.send_json(message)
    except Exception as e:
        logger.error(f"[WS] Error sending message: {e}")
