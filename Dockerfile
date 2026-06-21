# MovieHouse — synced watch-party server (admin + viewer in one process)
FROM python:3.13-slim

# ffmpeg-less image; we only serve browser-native formats.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MH_HOST=0.0.0.0 \
    MH_ADMIN_PORT=10457 \
    MH_VIEWER_PORT=10458

WORKDIR /app

# Install dependencies first for better layer caching.
RUN pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "python-multipart>=0.0.9" \
        "httpx>=0.27"

# App code.
COPY hub.py media.py config.py auth.py admin.py viewer.py main.py ./
COPY templates ./templates

# Uploaded videos live here; mount a volume to persist them.
RUN mkdir -p /app/uploads
VOLUME ["/app/uploads"]

EXPOSE 10457 10458

CMD ["python", "main.py"]
