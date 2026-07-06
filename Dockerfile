FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Silero VAD model so runtime does not depend on GitHub reachability
ENV TORCH_HOME=/app/.cache/torch
RUN mkdir -p /app/.cache/torch && \
    python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True, skip_validation=True)"

# Copy source code
COPY backend/ backend/
COPY frontend/ frontend/

# Expose the FastAPI port
EXPOSE 8000

# Add non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app/.cache
USER appuser

# Add HEALTHCHECK
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Run the FastAPI server via uvicorn
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
