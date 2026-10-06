FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-cache

# Run as an unprivileged user; own /app so the venv is readable.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# stdio by default. For the remote transport override the command:
#   docker run -e SPLITWISE_API_KEY=... -e SPLITWISE_MCP_BEARER=... -e SPLITWISE_MCP_HOST=0.0.0.0 \
#     -e SPLITWISE_MCP_ALLOWED_HOSTS=<public host> -p 8765:8765 <image> amazing-splitwise-mcp-http
CMD ["amazing-splitwise-mcp"]
