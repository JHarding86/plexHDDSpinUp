FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/config \
    CONFIG_DIR=/config \
    MNT_ROOT=/mnt \
    DISKS_INI=/unraid/disks.ini \
    PUID=99 \
    PGID=100 \
    PORT=9876

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9876
VOLUME /config

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '9876'), timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
# One worker so the wake queue, debounce state and websocket listener live in one process.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 60 --bind 0.0.0.0:${PORT} 'app:create_app()'"]
