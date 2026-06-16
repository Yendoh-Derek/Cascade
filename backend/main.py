"""
cascade/backend/main.py

FastAPI application entry point.
Handles routing for health checks, WebSocket voice pipeline, and static files.

Fixes applied:
  [C1] Disconnect message ({'type':'websocket.disconnect','code':N,'reason':''})
       now detected before attempting another receive() — breaks the loop
       cleanly instead of crashing with "Cannot call receive once a disconnect
       message has been received."
  [C2] Server-side: stop signal matching now handles both plain "stop" text
       and graceful disconnect equally.
  [M4] Sends user-visible "busy" message when a concurrent transcript is dropped
       so the user knows to wait.
"""

import logging
import asyncio
from typing import Any, Dict, Optional
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
    """Health check — confirms API keys are loaded without exposing values."""
    try:
        keys = get_api_keys()
        config = get_model_config()
        key_status = {
            "deepgram": bool(keys.deepgram),
            "groq": bool(keys.groq),
        }
        return {
            "status": "healthy" if all(key_status.values()) else "degraded",
            "api_keys_present": key_status,
            "models": {
                "stt": config.deepgram_model,
                "llm": config.groq_model,
                "edge_tts": config.edge_tts_voice,
                "deepgram_tts": config.deepgram_tts_model,
            },
        }
    except EnvironmentError as e:
        return {"status": "unhealthy", "error": str(e)}


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    subject: Optional[str] = Query(default=None),
    tts_engine: str = Query(default="edge"),
):
    """
    WebSocket endpoint for the streaming voice pipeline.

    Client → Server messages:
      binary  — raw PCM16 audio bytes from the microphone
      text    — "stop" to end the session

    Server → Client messages:
      binary  — MP3 audio chunks (TTS output)
      JSON    — {type: "transcript"|"response_chunk"|"response_end"|
                        "latency"|"error"|"busy"}
    """
    if subject is not None:
        subject = subject.strip()[:200] or None

    session: Optional[PipelineSession] = None

    try:
        await websocket.accept()
        logger.info(f"[WS] Client connected (subject={subject})")

        try:
            keys = get_api_keys()
            config = get_model_config()
        except EnvironmentError as e:
            await websocket.send_json({"type": "error", "message": str(e)})
            await websocket.close()
            return

        send_lock = asyncio.Lock()

        session = PipelineSession(
            api_keys={"deepgram": keys.deepgram, "groq": keys.groq},
            model_config={
                "deepgram_model": config.deepgram_model,
                "groq_model": config.groq_model,
                "edge_tts_voice": config.edge_tts_voice,
                "deepgram_tts_model": config.deepgram_tts_model,
            },
            send_message=lambda msg: asyncio.create_task(
                _send_ws_message(websocket, msg, send_lock)
            ),
            subject=subject,
            tts_engine=tts_engine,
        )

        try:
            await asyncio.wait_for(session.initialize(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.send_json(
                {"type": "error", "message": "Pipeline initialization timed out"}
            )
            await websocket.close()
            return
        except Exception as e:
            await websocket.send_json(
                {"type": "error", "message": f"Pipeline initialization failed: {e}"}
            )
            await websocket.close()
            return

        idle_timeout = 300  # 5 minutes

        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("[WS] Session idle — closing")
                await websocket.send_json(
                    {"type": "error", "message": "Session idle timeout"}
                )
                break

            # ── FIX [C1] ─────────────────────────────────────────────────
            # When the browser closes the connection, Starlette's receive()
            # returns {"type": "websocket.disconnect", "code": N, "reason": ""}.
            # The original code only checked for "bytes" and "text" keys,
            # so this message was logged as an unknown format and receive()
            # was called again — which raised the crash seen in the logs.
            # Solution: check the message type first, break immediately.
            msg_type = message.get("type", "")
            if msg_type == "websocket.disconnect":
                logger.info(
                    f"[WS] Client disconnected (code={message.get('code', '?')})"
                )
                break

            # Binary — raw PCM16 audio from the browser mic
            raw_bytes = message.get("bytes")
            if raw_bytes:
                if len(raw_bytes) > 10_000_000:
                    logger.warning(
                        f"[WS] Dropping oversized audio chunk ({len(raw_bytes)}B)"
                    )
                    continue
                if len(raw_bytes) >= 2:
                    await session.handle_audio(raw_bytes)
                continue

            # Text — control signals from the browser
            raw_text = message.get("text", "")
            if raw_text:
                if raw_text.strip() == "stop":
                    logger.info("[WS] Stop signal received — ending session")
                    break
                logger.debug(f"[WS] Unrecognised text message: {raw_text[:80]}")
                continue

            logger.debug(f"[WS] Unhandled message type: {msg_type!r}")

    except WebSocketDisconnect:
        logger.info("[WS] WebSocketDisconnect exception — client disconnected")
    except Exception as e:
        logger.error(f"[WS] Unexpected server error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if session:
            try:
                await session.close()
            except Exception as e:
                logger.error(f"[WS] Error during session cleanup: {e}")


async def _send_ws_message(websocket: WebSocket, message: Dict[str, Any], lock: asyncio.Lock):
    """Route a message to the WebSocket — binary for audio, JSON for everything else."""
    async with lock:
        try:
            if message.get("type") == "audio":
                audio_data = message.get("data", b"")
                if isinstance(audio_data, str):
                    audio_data = bytes.fromhex(audio_data)
                if audio_data:
                    await websocket.send_bytes(audio_data)
            else:
                await websocket.send_json(message)
        except Exception as e:
            # Client likely disconnected mid-send — not a server error
            logger.debug(f"[WS] Send failed (client likely gone): {e}")


# Mount frontend static files — must be registered last so it doesn't
# shadow the API routes or WebSocket endpoint defined above.
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount(
        "/", StaticFiles(directory=frontend_path, html=True), name="frontend"
    )
