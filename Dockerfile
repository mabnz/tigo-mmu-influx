# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code.
COPY tigo_app.py tigo_scraper.py ./

# Run as a non-root user; create the state dir it expects.
RUN useradd --system --uid 1000 --home-dir /app --shell /usr/sbin/nologin tigo \
    && mkdir -p /opt/tigo \
    && chown -R tigo:tigo /opt/tigo /app
USER tigo

ENV LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8080 \
    STATE_FILE=/opt/tigo/state.json

EXPOSE 8080
VOLUME ["/opt/tigo"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "tigo_app.py"]
