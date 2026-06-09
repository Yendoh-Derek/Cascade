"""
cascade/backend/main.py

FastAPI application entry point.
Handles routing for health checks, WebSocket voice pipeline, and static files.
"""

import logging
import json
import time
import asyncio
from typing import Dict, Any, Optional
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import get_api_keys, get_model_config
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
    subject: Optional[str] = Query(default=None, description="Optional tutoring subject"),
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
    """
    # Validate subject parameter
    if subject is not None:
        if not isinstance(subject, str) or len(subject) > 200:
            subject = None
        elif not subject.strip():
            subject = None
    
    session: Optional[PipelineSession] = None
    try:
        await websocket.accept()
        logger.info(f"[WS] Client connected (subject={subject})")

        # Get API keys and config
        try:
            keys = get_api_keys()
            config = get_model_config()
        except EnvironmentError as e:
            await websocket.send_json({"type": "error", "message": f"API keys not configured: {str(e)}"})
            await websocket.close()
            return

        # Create pipeline session
        # Use lambda with asyncio.create_task to bridge sync callback to async WS send
        keys_dict = {
            "deepgram": keys.deepgram,
            "groq": keys.groq,
        }
        config_dict = {
            "deepgram_model": config.deepgram_model,
            "groq_model": config.groq_model,
            "edge_tts_voice": config.edge_tts_voice,
        }
        session = PipelineSession(
            api_keys=keys_dict,
            model_config=config_dict,
            send_message=lambda msg: asyncio.create_task(_send_ws_message(websocket, msg)),
            subject=subject,
        )

        # Initialize pipeline with timeout
        try:
            await asyncio.wait_for(session.initialize(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.send_json({"type": "error", "message": "Pipeline initialization timed out"})
            await websocket.close()
            return
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"Pipeline initialization failed: {str(e)}"})
            await websocket.close()
            return

        # Listen for incoming messages with idle timeout
        idle_timeout = 300  # 5 minutes
        
        while True:
            try:
                # Receive message with timeout
                message = await asyncio.wait_for(websocket.receive(), timeout=idle_timeout)

                if "bytes" in message:
                    audio_bytes = message["bytes"]
                    # Safety check: max 10MB per chunk (more reasonable than 100MB)
                    if len(audio_bytes) > 10_000_000:
                        logger.warning(f"[WS] Audio chunk too large: {len(audio_bytes)} bytes")
                        continue
                    if len(audio_bytes) < 100:
                        continue
                    
                    await session.handle_audio(audio_bytes)

                elif "text" in message:
                    text = message["text"]
                    if text == "stop":
                        logger.info("[WS] Client requested stop")
                        break
                    else:
                        try:
                            json.loads(text)
                            logger.debug("[WS] Received JSON message")
                        except json.JSONDecodeError:
                            logger.debug(f"[WS] Received text: {text[:50]}")
                else:
                    logger.warning(f"[WS] Unknown message format: {list(message.keys())}")

            except asyncio.TimeoutError:
                logger.info("[WS] Connection idle timeout")
                await websocket.send_json({"type": "error", "message": "Connection idle timeout"})
                break

    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected")
    except Exception as e:
        logger.error(f"[WS] Error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": f"Server error: {str(e)}"})
        except:
            pass
    finally:
        if session:
            try:
                await session.close()
            except Exception as e:
                logger.error(f"[WS] Error during session cleanup: {e}")


async def _send_ws_message(websocket: WebSocket, message: Dict[str, Any]):
    """Send a message over WebSocket, handling both JSON and binary."""
    try:
        if message.get("type") == "audio" and "data" in message:
            # Audio chunks are sent as binary data directly
            audio_data = message["data"]
            if isinstance(audio_data, str):
                audio_data = bytes.fromhex(audio_data)
            await websocket.send_bytes(audio_data)
        else:
            # All other messages as JSON
            await websocket.send_json(message)
    except Exception as e:
        # Client likely disconnected during send
        logger.debug(f"[WS] Failed to send message: {e}")


# Mount frontend static files last to avoid intercepting API/WS routes
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
