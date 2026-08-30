FROM python:3.12-slim-bookworm

# Build deps for argon2-cffi and Pillow, removed after the install.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libffi-dev curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system netlog && \
    useradd --system --gid netlog --home-dir /var/lib/network-log-server netlog

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt && \
    pip install --no-cache-dir uvloop && \
    apt-get purge -y build-essential libffi-dev && apt-get autoremove -y

COPY backend/  /app/backend/
COPY frontend/ /app/frontend/
COPY database/ /app/database/

RUN mkdir -p /var/lib/network-log-server/branding \
             /var/log/network-log-server \
             /var/backups/network-log-server/archive && \
    chown -R netlog:netlog /var/lib/network-log-server \
                           /var/log/network-log-server \
                           /var/backups/network-log-server

ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    NLS_CONFIG_DIR=/etc/network-log-server

USER netlog
EXPOSE 8088 514/udp 514/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8088/api/health || exit 1

CMD ["uvicorn", "app.api.app:get_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8088"]
