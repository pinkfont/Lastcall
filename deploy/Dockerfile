# Lastcall server image: REST API + scheduler + Telegram bot.
# Build from the repository root:  docker build -f deploy/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    SUBMISSION_CHECK=remote

WORKDIR /srv/app
COPY app/requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY app/duetracker ./duetracker

# Run as an unprivileged user; the database lives on the mounted /data volume.
RUN useradd --uid 10001 --no-create-home lastcall && mkdir -p /data && chown lastcall /data
USER lastcall

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "duetracker", "serve"]
