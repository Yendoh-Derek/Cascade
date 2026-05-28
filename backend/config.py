"""
cascade/backend/config.py

Central configuration module. Loads and validates all environment variables
at startup. Any missing key raises an explicit error immediately rather than
failing silently deep inside the pipeline.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class APIKeys:
    deepgram: str
    groq: str
    openai: str


@dataclass(frozen=True)
class ModelConfig:
    groq_model: str = "llama-3.3-70b-versatile"
    openai_tts_model: str = "tts-1"
    openai_tts_voice: str = "nova"
    sample_rate: int = 16000
    channels: int = 1


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
        openai=_require_env("OPENAI_API_KEY"),
    )


# Singletons — imported directly by other modules
try:
    api_keys = load_api_keys()
except EnvironmentError as e:
    # Allow import to succeed so verification scripts can report the error cleanly
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
