"""
cascade/backend/main.py

FastAPI application entry point.

Phase 1: Exposes a health check endpoint that confirms all three
API keys are present and the server is running. The WebSocket
pipeline is added in Phase 2.

Usage:
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_api_keys, get_model_config, server_config

app = FastAPI(
    title="Cascade — AI Voice Tutor",
    description="Low-latency streaming voice pipeline for AI tutoring sessions.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tightened in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            "elevenlabs": bool(keys.elevenlabs),
        }
        all_present = all(key_status.values())
        return {
            "status": "healthy" if all_present else "degraded",
            "api_keys_present": key_status,
            "models": {
                "stt": config.deepgram_model,
                "llm": config.groq_model,
                "tts_model": config.elevenlabs_model,
                "tts_voice_id": config.elevenlabs_voice_id[:6] + "...",
            },
        }
    except EnvironmentError as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
