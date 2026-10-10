# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS web
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --strict-ssl=true
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NABD_DATA_DIR=/app/data BOT_DB_PATH=/app/data/bot.sqlite3
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN --mount=type=secret,id=system_ca \
    if [ -f /run/secrets/system_ca ]; then export PIP_CERT=/run/secrets/system_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /build/frontend/dist /app/frontend/dist
RUN useradd --uid 10001 --create-home nabd && mkdir -p /app/data \
    && chmod -R a+rX /app/backend /app/frontend && chown -R nabd:nabd /app/data
USER nabd
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
