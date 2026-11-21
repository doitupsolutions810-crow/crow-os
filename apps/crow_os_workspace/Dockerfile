FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/crow-os
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x docker-entrypoint.sh && mkdir -p /opt/crow-os/var

ENV CROW_HOST=0.0.0.0 \
    CROW_PORT=8765 \
    CROW_OPEN_BROWSER=0 \
    CROW_HOME=/opt/crow-os \
    CROW_REPO=/repo \
    CROW_WORKTREES=/opt/crow-os/var/worktrees \
    OLLAMA_HOST=http://host.docker.internal:11434 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)" || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
