# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="OmniFetch" \
      org.opencontainers.image.description="Local-First public media extraction, trimming, and remuxing via yt-dlp and FFmpeg" \
      org.opencontainers.image.version="1.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMNIFETCH_TEMP_DIR="/tmp/omnifetch_temp" \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Install FFmpeg/FFprobe and CA certificates without extra bloat
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create unprivileged runtime user (UID 1000) and isolated temp directory
RUN groupadd --gid 1000 omnifetch && \
    useradd --uid 1000 --gid omnifetch --shell /bin/bash --create-home omnifetch && \
    mkdir -p /app /tmp/omnifetch_temp && \
    chown -R omnifetch:omnifetch /app /tmp/omnifetch_temp

WORKDIR /app

# Install production runtime dependencies only (no pytest/ruff/pyright in final image)
COPY --chown=omnifetch:omnifetch requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY --chown=omnifetch:omnifetch . .

USER omnifetch

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; from utils.diagnostics import run_startup_diagnostics; assert run_startup_diagnostics().all_passed; assert urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status == 200" || exit 1

# For Local-First host security, bind loopback on publish: -p 127.0.0.1:8501:8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
