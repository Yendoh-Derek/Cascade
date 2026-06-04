"""
cascade/backend/config.py

Central configuration module. Loads and validates all environment variables
at startup. Any missing key raises an explicit error immediately rather than
failing silently deep inside the pipeline.

TTS provider: ElevenLabs
  - Free tier: 10,000 characters/month (sufficient for demo)
  - Accepts payment methods available in Ghana
  - Streaming API compatible with Cascade's chunk-based pipeline
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class APIKeys:
    deepgram: str
    groq: str
    elevenlabs: str


@dataclass(frozen=True)
class ModelConfig:
    # STT
    deepgram_model: str = "nova-2"
    deepgram_language: str = "en-US"
    sample_rate: int = 16000
    channels: int = 1

    # LLM
    groq_model: str = "llama-3.3-70b-versatile"

    # TTS
    elevenlabs_model: str = "eleven_turbo_v2_5"   # Lowest latency ElevenLabs model
    elevenlabs_voice_id: str = ""                  # Populated from env at runtime


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


def _require_env(key: str) -> str:
    """Load a required environment variable, raising clearly if missing."""
    value = os.getenv(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: '{key}'\n"
            f"  → Copy .env.example to .env and fill in your API keys."
        )
    return value


def load_api_keys() -> APIKeys:
    """Load and return all API keys from environment."""
    return APIKeys(
        deepgram=_require_env("DEEPGRAM_API_KEY"),
        groq=_require_env("GROQ_API_KEY"),
        elevenlabs=_require_env("ELEVENLABS_API_KEY"),
    )


def load_model_config() -> ModelConfig:
    """
    Load model config, injecting the ElevenLabs voice ID from environment.
    The voice ID is kept in .env so it can be changed without touching code.
    """
    voice_id = _require_env("ELEVENLABS_VOICE_ID")
    return ModelConfig(elevenlabs_voice_id=voice_id)


# Singletons — imported directly by other modules
try:
    api_keys = load_api_keys()
    model_config = load_model_config()
except EnvironmentError as e:
    # Allow import to succeed so verification scripts can report the error cleanly
    api_keys = None
    model_config = None
    _config_error = str(e)
else:
    _config_error = None

server_config = ServerConfig()


def get_api_keys() -> APIKeys:
    """Return validated API keys or raise if config failed."""
    if api_keys is None:
        raise EnvironmentError(_config_error)
    return api_keys


def get_model_config() -> ModelConfig:
    """Return validated model config or raise if config failed."""
    if model_config is None:
        raise EnvironmentError(_config_error)
    return model_config
