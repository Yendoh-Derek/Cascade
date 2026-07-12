"""
cascade/backend/main.py

FastAPI application entry point.
Handles routing for health checks, WebSocket voice pipeline, and static files.
"""
import hashlib
import json
import logging
import asyncio
import hmac
import re as _re
import time
import secrets
from typing import Any, Callable, Dict, Optional
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import get_api_keys, get_model_config, server_config
from backend.pipeline import PipelineSession
from backend.vad import get_shared_vad_model
from backend.quota import quota_manager, RegistrationResult
from groq import AsyncGroq

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

LOCAL_WS_HOSTS = frozenset({"localhost", "127.0.0.1"})

def get_client_ip(request: Request | WebSocket) -> str:
    """Extract client IP.
    
    Proxy headers (CF-Connecting-IP, X-Forwarded-For) are only trusted when
    CASCADE_TRUST_PROXY_HEADERS=true — set this ONLY when actually deployed
    behind Cloudflare or a trusted reverse proxy. Ignoring these headers on
    a direct-exposed origin prevents IP spoofing via crafted headers.
    """
    if server_config.trust_proxy_headers:
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
        x_forwarded = request.headers.get("X-Forwarded-For")
        if x_forwarded:
            return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"



def is_websocket_origin_allowed(origin: Optional[str], host_header: str) -> bool:
    """Validate WebSocket Origin against the server Host header.

    Browsers always send Origin on cross-origin WebSocket handshakes. Missing
    Origin is only permitted for local development hosts so curl and test
    clients still work on localhost.
    """
    server_host = (host_header or "").split(":")[0]
    allowed_hosts = {server_host}
    if server_host in LOCAL_WS_HOSTS:
        allowed_hosts |= LOCAL_WS_HOSTS
    if origin:
        origin_host = urlsplit(origin).hostname or ""
        return origin_host in allowed_hosts
    return server_host in LOCAL_WS_HOSTS

@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_groq_client
    # Initialize shared Groq client on startup
    keys = get_api_keys()
    shared_groq_client = AsyncGroq(api_key=keys.groq)
    logger.info("[App] Shared Groq client initialized")
    
    # Initialize shared VAD model to prevent event-loop blocking on first session
    await asyncio.to_thread(get_shared_vad_model)
    logger.info("[App] Shared VAD model initialized")
    
    # Initialize quota DB
    await quota_manager.initialize()
    logger.info("[App] Quota manager initialized")
    
    yield
    # Clean up on shutdown
    if shared_groq_client:
        try:
            await shared_groq_client.close()
            logger.info("[App] Shared Groq client closed successfully")
        except Exception as e:
            logger.error(f"[App] Error closing shared Groq client: {e}")

app = FastAPI(
    title="Cascade — AI Voice Tutor",
    description="Low-latency streaming voice pipeline for AI tutoring sessions.",
    version="0.1.0",
    lifespan=lifespan,
)

# Process-wide concurrent WebSocket sessions cap
MAX_CONCURRENT_SESSIONS = server_config.max_concurrent_sessions
session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)

# Shared AsyncGroq client (initialized on app startup)
shared_groq_client: Optional[AsyncGroq] = None

# Per-tester active connection counter — prevents a single tester_id from
# holding more than MAX_TESTER_CONNECTIONS simultaneous WebSocket sessions.
MAX_TESTER_CONNECTIONS = 2
_active_tester_connections: dict[str, int] = {}

# UUID-shape regex for tester_id validation
_UUID_RE = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

# HMAC key for IP hashing. Derived from CASCADE_IP_HASH_SECRET env var.
# Auto-generated ephemerally if unset — log a warning so operators notice.
_ip_hash_key: bytes
if server_config.ip_hash_secret:
    _ip_hash_key = server_config.ip_hash_secret.encode()
else:
    _ip_hash_key = secrets.token_bytes(32)
    logger.warning(
        "[Security] CASCADE_IP_HASH_SECRET is not set. Using an ephemeral random key. "
        "IP hashes will change on every restart. Set a stable secret in production."
    )

# Allow operators to tighten CORS in production via CASCADE_CORS_ORIGINS env var.
# Default is "*" for local development only — restrict this before public deployment.
# Example: CASCADE_CORS_ORIGINS=https://myapp.com,https://staging.myapp.com
_cors_origins_raw = server_config.cors_origins
CORS_ORIGINS = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if CORS_ORIGINS == ["*"]:
    logger.warning(
        "[Security] CORS is open to all origins (*). "
        "Set CASCADE_CORS_ORIGINS env var before deploying to production."
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


@app.get("/quota/status", tags=["Quota"])
async def quota_status():
    """Returns available slots and budget."""
    return await quota_manager.get_status()


@app.post("/quota/feedback", tags=["Quota"])
async def quota_feedback(request: Request):
    """Saves end-of-session feedback."""
    data = await request.json()
    tester_id = data.get("tester_id")
    rating = data.get("rating")
    comment = data.get("comment")
    if tester_id and rating:
        await quota_manager.save_feedback(tester_id, rating, comment)
    return {"status": "ok"}



@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    tts_engine: str = Query(default="deepgram"),
):
    """
    WebSocket endpoint for the streaming voice pipeline.

    Client → Server messages:
      binary  — raw PCM16 audio bytes from the microphone
      text    — "stop" to end the session
                JSON with type "cancel" or "finalize"

    Server → Client messages:
      binary  — MP3 audio chunks (TTS output)
      JSON    — {type: "transcript"|"response_chunk"|"response_end"|
                        "latency"|"error"|"busy"}
    """

    # Origin validation runs BEFORE the semaphore acquire so we don't
    # spend a concurrency slot on a connection that's about to be rejected.
    await websocket.accept()
    logger.info("[WS] Client connected")

    origin = websocket.headers.get("origin")
    host_header = websocket.headers.get("host") or ""
    if not is_websocket_origin_allowed(origin, host_header):
        logger.warning(
            f"[WS] Rejecting connection from origin {origin!r} "
            f"(host={host_header!r})"
        )
        await websocket.close(code=4003)
        return

    # 2. Concurrency Capacity Cap Check / Acquire
    try:
        await asyncio.wait_for(session_semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        await websocket.send_json({
            "type": "busy",
            "reason": "capacity",
            "message": "Server is at maximum capacity. Please try again later."
        })
        await websocket.close()
        return

    try:
        session: Optional[PipelineSession] = None
        sender_task: Optional[asyncio.Task] = None
        ping_task: Optional[asyncio.Task] = None
        quota_task: Optional[asyncio.Task] = None
        session_unsaved_quota = 0.0
        sender_running = False
        tester_id: Optional[str] = None
        seconds_used = 0.0
        identified_tester_id: Optional[str] = None  # track for per-tester concurrency cleanup

        try:
            # Origin check already done and accepted above; skip the redundant block.

            # 1. Auth Secret Verification (HMAC challenge-response)
            auth_secret = server_config.auth_secret
            pre_auth_audio: list[bytes] = []
            if auth_secret:
                authorized = False
                nonce = secrets.token_hex(16)
                await websocket.send_json({"type": "challenge", "nonce": nonce})

                try:
                    start_time = time.time()
                    while time.time() - start_time < 5.0 and not authorized:
                        message = await asyncio.wait_for(
                            websocket.receive(),
                            timeout=5.0 - (time.time() - start_time)
                        )

                        msg_type = message.get("type", "")
                        if msg_type == "websocket.disconnect":
                            break

                        # If it's a text message, parse and check if it's the auth message
                        raw_text = message.get("text", "")
                        if raw_text:
                            try:
                                auth_msg = json.loads(raw_text.strip())
                                if auth_msg.get("type") == "auth":
                                    candidate_hmac = auth_msg.get("response")
                                    expected_hmac = hmac.new(
                                        auth_secret.encode(), nonce.encode(), hashlib.sha256
                                    ).hexdigest()
                                    if isinstance(candidate_hmac, str) and hmac.compare_digest(
                                        candidate_hmac, expected_hmac
                                    ):
                                        await websocket.send_json({"type": "auth_ok"})
                                        authorized = True
                                        break
                            except json.JSONDecodeError:
                                pass

                        # If it's a binary message (early audio), buffer it.
                        # Cap: same 10MB-per-chunk limit as the main loop, plus
                        # a 256KB cumulative cap — enough for audio sent slightly
                        # early, not enough for an unauthenticated flood.
                        raw_bytes = message.get("bytes")
                        if raw_bytes:
                            pre_auth_total: int = sum(len(chunk) for chunk in pre_auth_audio)
                            if (
                                len(raw_bytes) <= 10_000_000
                                and pre_auth_total + len(raw_bytes) <= 256 * 1024
                            ):
                                pre_auth_audio.append(raw_bytes)
                            else:
                                logger.warning(
                                    f"[WS] Pre-auth audio buffer cap exceeded "
                                    f"(chunk={len(raw_bytes)}B, total={pre_auth_total}B) — dropping"
                                )
                except (asyncio.TimeoutError, WebSocketDisconnect):
                    pass

                if not authorized:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Unauthorized: Invalid or missing auth secret"
                    })
                    await websocket.close(code=4001)
                    return

            if server_config.quota_enabled:
                identified = False
                start_time = time.time()
                while time.time() - start_time < 5.0 and not identified:
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive(),
                            timeout=5.0 - (time.time() - start_time)
                        )
                        msg_type = message.get("type", "")
                        if msg_type == "websocket.disconnect":
                            break
                        
                        raw_text = message.get("text", "")
                        if raw_text:
                            try:
                                id_msg = json.loads(raw_text.strip())
                                if id_msg.get("type") == "identify":
                                    candidate_id = id_msg.get("tester_id", "")
                                    # Sec: reject non-UUID tester_ids to prevent
                                    # arbitrarily large keys written to TEXT PRIMARY KEY.
                                    if not isinstance(candidate_id, str) or not _UUID_RE.match(candidate_id):
                                        await websocket.send_json({"type": "error", "message": "Invalid tester_id format"})
                                        await websocket.close(code=4000)
                                        return
                                    tester_id = candidate_id
                                    if tester_id:
                                        identified = True
                                        break
                            except json.JSONDecodeError:
                                pass
                        
                        raw_bytes = message.get("bytes")
                        if raw_bytes:
                            pre_auth_audio.append(raw_bytes)
                    except (asyncio.TimeoutError, WebSocketDisconnect):
                        break
                        
                if not identified or not tester_id:
                    await websocket.send_json({"type": "error", "message": "Missing identify message"})
                    await websocket.close(code=4000)
                    return

                # Per-tester concurrency cap: at most MAX_TESTER_CONNECTIONS simultaneous
                # sessions per tester_id. Prevents tab-duplication abuse.
                current_count = _active_tester_connections.get(tester_id, 0)
                if current_count >= MAX_TESTER_CONNECTIONS:
                    await websocket.send_json({
                        "type": "busy",
                        "reason": "duplicate_session",
                        "message": "You already have an active session open in another tab."
                    })
                    await websocket.close()
                    return
                _active_tester_connections[tester_id] = current_count + 1
                identified_tester_id = tester_id
                    
                # HMAC-salt the IP so quota.db leaks don’t expose raw IP hashes.
                ip_hash = hmac.new(_ip_hash_key, get_client_ip(websocket).encode(), hashlib.sha256).hexdigest()
                reg_result = await quota_manager.get_or_register(tester_id, ip_hash)
                
                if reg_result == RegistrationResult.CAP_REACHED:
                    await websocket.send_json({"type": "capacity_reached", "message": f"All {server_config.max_testers} testing spots are currently claimed."})
                    await websocket.close()
                    return
                elif reg_result == RegistrationResult.IP_RATE_LIMITED:
                    await websocket.send_json({"type": "ip_rate_limited", "message": "Too many new sessions from this network — please try again later."})
                    await websocket.close()
                    return
                    
                seconds_used = await quota_manager.get_seconds_used(tester_id)
                if seconds_used >= server_config.tester_budget_sec:
                    await websocket.send_json({"type": "quota_exceeded", "seconds_used": seconds_used})
                    await websocket.close()
                    return

            try:
                keys = get_api_keys()
                config = get_model_config()
            except EnvironmentError as e:
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return

            # Create outbound message queue first
            outbound_queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()

            # Initialize session first
            session = PipelineSession(
                api_keys={"deepgram": keys.deepgram, "groq": keys.groq},
                model_config={
                    "deepgram_model": config.deepgram_model,
                    "deepgram_language": config.deepgram_language,
                    "groq_model": config.groq_model,
                    "edge_tts_voice": config.edge_tts_voice,
                    "deepgram_tts_model": config.deepgram_tts_model,
                    "stt_endpointing_ms": config.stt_endpointing_ms,
                    "max_history_turns": config.max_history_turns,
                    "vad_threshold": config.vad_threshold,
                    "vad_silence_ms": config.vad_silence_ms,
                    "vad_min_speech_frames": config.vad_min_speech_frames,
                    "enable_speculative_llm": config.enable_speculative_llm,
                    "speculative_stability_matches": config.speculative_stability_matches,
                    "speculative_grace_ms": config.speculative_grace_ms,
                    "buffer_stall_ms": getattr(config, "buffer_stall_ms", 500),
                },
                outbound_queue=outbound_queue,
                tts_engine=tts_engine,
                llm_client=shared_groq_client,
            )

            # Now create and start sender task
            sender_running = True

            async def sender_coroutine() -> None:
                """Single coroutine responsible for all outbound WebSocket messages."""
                nonlocal sender_running
                while sender_running:
                    try:
                        msg = await outbound_queue.get()
                        if msg is None:  # Sentinel value to stop sender
                            break
                        if session and not session.can_send_message(msg):
                            # Skip awaiting _send_ws_message if we already know we'll drop it.
                            # This instantly purges hundreds of stale messages in one event loop tick
                            # instead of yielding context for each one.
                            continue
                        await _send_ws_message(websocket, msg, session.can_send_message)
                    except asyncio.CancelledError:
                        break
                    except (WebSocketDisconnect, ConnectionResetError, RuntimeError) as e:
                        # Expected: client disconnected mid-send
                        logger.debug(f"[WS] Sender: client disconnected ({type(e).__name__})")
                        break
                    except Exception as e:
                        # Log unexpected sender errors for diagnostics.
                        logger.error(f"[WS] Sender unexpected error: {type(e).__name__}: {e}")
                    finally:
                        try:
                            outbound_queue.task_done()
                        except ValueError:
                            pass

            sender_task = asyncio.create_task(sender_coroutine())

            # Heartbeat — sends a ping every 15s to detect silent disconnections (IMP-02)
            async def ping_coroutine():
                while sender_running:
                    await asyncio.sleep(15)
                    if not sender_running:
                        break
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break

            ping_task = asyncio.create_task(ping_coroutine())


            if server_config.quota_enabled and tester_id:
                async def quota_coroutine():
                    nonlocal session_unsaved_quota
                    budget = server_config.tester_budget_sec
                    used = seconds_used
                    last_tick = time.time()
                    grace_period_started = False
                    last_warning_sent: float = 0.0  

                    while sender_running:
                        await asyncio.sleep(1.0)
                        if not sender_running:
                            break

                        now = time.time()
                        delta = now - last_tick
                        last_tick = now

                        # Billing: only accumulate time while the user is actively
                        # speaking (VAD-detected) OR the AI is playing back audio.
                        # This ensures testers are billed fairly for engaged time,
                        # not for idle silence or thinking pauses between turns.
                        if session and (session.user_speaking or session.is_ai_speaking()) and not grace_period_started:
                            used += delta
                            session_unsaved_quota += delta
                            remaining = budget - used

                            if session_unsaved_quota >= 30.0:
                                await quota_manager.record_usage(tester_id, session_unsaved_quota)
                                session_unsaved_quota = 0.0

                            # Send quota update every 1 second of active time.
                            # Since the client no longer interpolates, we stream the exact value
                            # at 1Hz so the UI strictly reflects billing ticks.
                            if remaining > 0:
                                if now - last_warning_sent >= 1.0:
                                    last_warning_sent = now
                                    await websocket.send_json({"type": "quota_warning", "seconds_remaining": int(remaining)})
                            elif remaining <= 0 and not grace_period_started:
                                grace_period_started = True
                                # Lock out new turns — current in-flight turn is allowed to finish.
                                if session:
                                    session.quota_locked = True
                                # Signal frontend: budget exhausted, but wrapping up gracefully.
                                await websocket.send_json({"type": "quota_exceeded", "grace_period": True})
                                # Wait for the active turn to complete, with a 25s safety cap
                                # (tutoring explanations can run longer than 15s of TTS).
                                deadline = time.time() + 25.0
                                while time.time() < deadline:
                                    if session is None or not session.is_turn_active():
                                        break
                                    await asyncio.sleep(0.2)
                                await asyncio.sleep(0.3)  # Let final messages flush
                                await websocket.close(code=1000)
                                break

                quota_task = asyncio.create_task(quota_coroutine())


            try:
                await asyncio.wait_for(session.initialize(), timeout=30)
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

            # Feed any buffered pre-authorization audio chunks into the session
            if pre_auth_audio:
                logger.info(
                    f"[WS] Feeding {len(pre_auth_audio)} buffered pre-authorization "
                    f"audio chunks into session"
                )
                for chunk in pre_auth_audio:
                    if len(chunk) >= 2:
                        await session.handle_audio(chunk)

            idle_timeout = server_config.idle_timeout_sec

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
                    stripped_text = raw_text.strip()
                    if stripped_text == "stop":
                        logger.info("[WS] Stop signal received — ending session")
                        break

                    try:
                        control_msg = json.loads(stripped_text)
                        if isinstance(control_msg, dict):
                            ctrl_type = control_msg.get("type")
                            if ctrl_type == "pong":
                                continue
                            elif ctrl_type == "cancel":
                                logger.info("[WS] Cancel signal received")
                                if session:
                                    await session.cancel()
                                continue
                            elif ctrl_type == "finalize":
                                logger.info("[WS] Finalize signal received")
                                if session and session.stt_handler:
                                    await session.stt_handler.finalize()
                                continue
                            elif ctrl_type == "playback_finished":
                                turn_id = control_msg.get("turn_id")
                                logger.info(f"[WS] Playback finished signal received (turn={turn_id})")
                                if session:
                                    session.set_ai_speaking(False)
                                continue
                            elif ctrl_type == "client_latency":
                                # Record the reported perceived latency for the current turn.
                                perceived_ms = control_msg.get("first_audio_played_ms")
                                turn_id = control_msg.get("turn_id")
                                if isinstance(perceived_ms, (int, float)) and 0 < perceived_ms < 60000:
                                    logger.info(
                                        f"[WS] Perceived latency reported: {int(perceived_ms)}ms "
                                        f"(turn={turn_id})"
                                    )
                                    # Echo back to dashboard
                                    if session:
                                        session.send_message({
                                            "type": "perceived_latency",
                                            "perceived_ms": int(perceived_ms),
                                            "turn_id": turn_id,
                                        })
                                continue
                            elif ctrl_type == "load_history":
                                # Restore conversation history on session resume.
                                # Validation is delegated to TutorSession.load_history(),
                                # which drops any malformed or injected entries.
                                if session:
                                    raw_history = control_msg.get("history", [])
                                    if isinstance(raw_history, list):
                                        session.tutor.load_history(raw_history)
                                        logger.info(
                                            f"[WS] Conversation history restored: "
                                            f"{len(raw_history)} entries from client"
                                        )
                                continue
                    except json.JSONDecodeError:
                        pass

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
            # Clean up sender task
            try:
                sender_running = False
                # Cancel heartbeat
                if ping_task is not None and not ping_task.done():
                    ping_task.cancel()
                    try:
                        await asyncio.wait_for(ping_task, timeout=0.5)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # Cancel sender
                if sender_task is not None and not sender_task.done():
                    sender_task.cancel()
                    try:
                        await asyncio.wait_for(sender_task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # Cancel quota_task
                if quota_task is not None and not quota_task.done():
                    quota_task.cancel()
                    try:
                        await asyncio.wait_for(quota_task, timeout=0.5)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                
                # Save any remaining unsaved quota
                if server_config.quota_enabled and tester_id and session_unsaved_quota > 0:
                    try:
                        await quota_manager.record_usage(tester_id, session_unsaved_quota)
                    except Exception as eq:
                        logger.error(f"[WS] Error saving final quota: {eq}")

                # Decrement per-tester connection count
                if identified_tester_id:
                    count = _active_tester_connections.get(identified_tester_id, 1)
                    if count <= 1:
                        _active_tester_connections.pop(identified_tester_id, None)
                    else:
                        _active_tester_connections[identified_tester_id] = count - 1

            except Exception as e:
                logger.error(f"[WS] Error cleaning up sender: {e}")

            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.error(f"[WS] Error during session cleanup: {e}")
    finally:
        session_semaphore.release()


async def _send_ws_message(
    websocket: WebSocket,
    message: Dict[str, Any],
    can_send: Optional[Callable[[Dict[str, Any]], bool]] = None,
):
    """Route a message to the WebSocket — binary for audio, JSON for everything else.

    Implements atomic turn_id validation at final consumer point to prevent stale
    audio from interrupted turns from reaching the client (Phase 3 interruption hardening).
    """
    try:
        # Final atomic validation at send time (checks if turn is still active)
        if can_send and not can_send(message):
            msg_type = message.get("type", "unknown")
            turn_id = message.get("turn_id", "none")
            logger.debug(f"[WS] Dropping {msg_type} for turn {turn_id} (turn no longer active)")
            return

        if message.get("type") == "audio":
            audio_data = message.get("data", b"")
            if isinstance(audio_data, str):
                # Defensive check: nothing in the codebase currently sends audio as a hex string,
                # as pipeline.py puts raw bytes directly. Kept for backwards/defensive compatibility.
                audio_data = bytes.fromhex(audio_data)
            if audio_data:
                turn_id = message.get("turn_id")
                if turn_id is not None:
                    frame = turn_id.to_bytes(4, "big") + audio_data
                else:
                    frame = audio_data
                await websocket.send_bytes(frame)
                logger.debug(f"[WS] Audio sent for turn {turn_id}: {len(audio_data)} bytes")
        else:
            msg_type = message.get("type", "unknown")
            turn_id = message.get("turn_id")
            await websocket.send_json(message)
            logger.debug(f"[WS] {msg_type} sent for turn {turn_id}")
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
