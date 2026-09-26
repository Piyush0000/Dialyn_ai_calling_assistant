FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY agents ./agents

RUN useradd --create-home agent && mkdir -p /srv/data && chown agent /srv/data
USER agent

ENV PATH="/srv/.venv/bin:$PATH" PORT=7860
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s   CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
