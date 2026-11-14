FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN useradd --create-home --uid 1000 deals && mkdir -p /data && chown deals:deals /data
USER deals
# Browser fallback off: the HTTP path needs no browser and Chromium is too heavy for a small VPS.
ENV DEALS_DATA_DIR=/data DEALS_PORT=8050 DEALS_BROWSER=0
VOLUME /data
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD curl -fsS http://127.0.0.1:${DEALS_PORT}/healthz || exit 1
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${DEALS_PORT} --proxy-headers --forwarded-allow-ips='*'"]
