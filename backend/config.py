"""
cascade/backend/config.py

Central configuration module. Loads and validates all environment variables
at startup. Any missing key raises an explicit error immediately rather than
failing silently deep inside the pipeline.

TTS provider: Deepgram Aura (Default) / Edge-TTS (Fallback)
  - Deepgram Aura: linear16, fast, requires DEEPGRAM_API_KEY
  - Edge-TTS: free, no API key, yields MP3 sentences
"""

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Prefer the repo's `.env` over stale process-level exports during local runs
# and reloads, which otherwise can cause valid project credentials to be
# shadowed by an outdated shell variable.
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=False)


@dataclass(frozen=True)
class APIKeys:
    deepgram: str
    groq: str


@dataclass(frozen=True)
class ModelConfig:
    # STT
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en-US"
    sample_rate: int = 16000
    channels: int = 1
    # Endpointing window in ms — how long Deepgram waits after last speech
    # before emitting speech_final. Tune via CASCADE_STT_ENDPOINTING env var.
    stt_endpointing_ms: int = int(os.getenv("CASCADE_STT_ENDPOINTING", "300"))
    
    # Advanced STT / VAD features
    vad_threshold: float = float(os.getenv("CASCADE_VAD_THRESHOLD", "0.5"))
    vad_silence_ms: int = int(os.getenv("CASCADE_VAD_SILENCE_MS", "200"))
    vad_min_speech_frames: int = int(os.getenv("CASCADE_VAD_MIN_SPEECH_FRAMES", "3"))
    speculative_grace_ms: int = int(os.getenv("CASCADE_SPECULATIVE_GRACE_MS", "180"))
    speculative_stability_matches: int = int(os.getenv("CASCADE_SPECULATIVE_STABILITY_MATCHES", "2"))
    enable_speculative_llm: bool = os.getenv("CASCADE_ENABLE_SPECULATIVE_LLM", "false").lower() == "true"

    # LLM
    groq_model: str = "llama-3.1-8b-instant"

    # Conversation history: max turn-pairs to retain. Tune via CASCADE_MAX_HISTORY_TURNS.
    max_history_turns: int = int(os.getenv("CASCADE_MAX_HISTORY_TURNS", "10"))

    # TTS — edge-tts requires no API key
    edge_tts_voice: str = "en-US-AriaNeural"
    deepgram_tts_model: str = "aura-2-asteria-en"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    max_concurrent_sessions: int = int(os.getenv("CASCADE_MAX_CONCURRENT_SESSIONS", "5"))
    cors_origins: str = os.getenv("CASCADE_CORS_ORIGINS", "*")
    auth_secret: str | None = os.getenv("CASCADE_AUTH_SECRET")
    idle_timeout_sec: int = int(os.getenv("CASCADE_IDLE_TIMEOUT_SEC", "300"))


def _require_env(key: str) -> str:
    """
    Load a required environment variable, raising clearly if missing.
    
    Args:
        key: Environment variable name
        
    Returns:
        The environment variable value (stripped)
        
    Raises:
        EnvironmentError: If variable is missing or empty
    """
    value = os.getenv(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: '{key}'\n"
            f"  → Copy .env.example to .env and fill in your API keys.\n"
            f"  → Required keys: DEEPGRAM_API_KEY, GROQ_API_KEY"
        )
    
    # Basic validation of key format
    if len(value) < 10:
        raise EnvironmentError(
            f"Environment variable '{key}' appears invalid (too short).\n"
            f"  → Check your .env file for correct API keys."
        )
    
    return value


def load_api_keys() -> APIKeys:
    """Load and return all API keys from environment."""
    return APIKeys(
        deepgram=_require_env("DEEPGRAM_API_KEY"),
        groq=_require_env("GROQ_API_KEY"),
    )


# Singletons — imported directly by other modules
api_keys: APIKeys | None = None
_config_error: str | None = None

try:
    api_keys = load_api_keys()
except EnvironmentError as e:
    api_keys = None
    _config_error = str(e)
else:
    _config_error = None

model_config = ModelConfig()
server_config = ServerConfig()


def get_api_keys() -> APIKeys:
    """Return validated API keys or raise if config failed."""
    if api_keys is None:
        raise EnvironmentError(_config_error)
    return api_keys


def get_model_config() -> ModelConfig:
    """Return model configuration."""
    return model_config
