# Production image: the FastAPI app under uvicorn, no dev tools.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first, so editing the code doesn't re-download them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra serve --no-install-project
COPY ratelimit ratelimit
COPY webapp webapp
COPY README.md ./
RUN uv sync --locked --extra serve

FROM python:3.13-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY --from=build --chown=app /app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --start-interval=1s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# One process per container; scale by running more containers. With Redis as the
# backend they all share limits, which is the whole point.
CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
