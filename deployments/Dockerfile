# ============================================================================
# Dockerfile — z-winnow multi-stage build
#
# Stage 1: builder — install Poetry and all dependencies (with dev deps)
# Stage 2: runtime — copy only production deps + app code, non-root user
# ============================================================================

# ---- Stage 1: Builder ----
FROM python:3.12-slim AS builder

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system deps for Poetry and package building
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Copy dependency files first for better layer caching
COPY pyproject.toml poetry.lock* ./

# Install all dependencies (including dev) for running tests in builder
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --with dev

# Copy all source code
COPY src/ ./src/
COPY tests/ ./tests/
COPY schemas/ ./schemas/

# ---- Stage 2: Runtime (slim, production-only) ----
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="z-winnow"
LABEL org.opencontainers.image.description="LangGraph multi-agent daily report system"
LABEL org.opencontainers.image.version="0.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install runtime system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry in runtime (for `poetry run winnow`)
RUN pip install poetry && \
    poetry config virtualenvs.create false

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy Poetry-installed site-packages and binaries from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy pyproject.toml for `poetry run` support
COPY --from=builder /app/pyproject.toml /app/poetry.lock* ./

# Copy application code
COPY --from=builder /app/src ./src
COPY --from=builder /app/schemas ./schemas

# Create directories for runtime data (mounted as volumes in production)
RUN mkdir -p /app/data /app/reports /app/memory && \
    chown -R appuser:appuser /app

USER appuser

# Default command: show help
ENTRYPOINT ["poetry", "run", "winnow"]
CMD ["--help"]

# Health check: verify the package imports correctly
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import z_winnow; print('ok')"
