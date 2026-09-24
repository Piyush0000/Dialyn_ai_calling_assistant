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

EXPOSE 7860
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:7860/health')"
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
