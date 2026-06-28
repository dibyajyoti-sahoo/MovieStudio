# MovieHouse — synced watch-party server (admin + viewer in one process)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MH_HOST=0.0.0.0 \
    MH_ADMIN_PORT=10457 \
    MH_VIEWER_PORT=10458

WORKDIR /app

# ffmpeg is required to transcode non-browser formats (.mkv, .avi, …) to MP4.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
RUN pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "python-multipart>=0.0.9" \
        "httpx[socks]>=0.27" \
        "imageio-ffmpeg>=0.4"

# App code.
COPY hub.py media.py config.py auth.py admin.py viewer.py drive.py transcode.py main.py ./
COPY templates ./templates

# Uploaded videos live here; mount a volume to persist them.
RUN mkdir -p /app/uploads
VOLUME ["/app/uploads"]

EXPOSE 10457 10458

CMD ["python", "main.py"]
