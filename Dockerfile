FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source
COPY server/ ./server/
COPY client/ ./client/

# Runtime directories (mounted as volumes in production)
RUN mkdir -p session/audio data/characters data/sessions

EXPOSE 3200

# Load .env if present, then start
CMD ["uv", "run", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "3200"]
