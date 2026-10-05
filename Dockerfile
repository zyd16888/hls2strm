FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JABLE_DATA_DIR=/data \
    JABLE_OUTPUT_DIR=/strm \
    JABLE_PORT=8080 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jable_strm ./jable_strm
RUN pip install . && mkdir -p /data /strm

EXPOSE 8080
VOLUME ["/data", "/strm"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "-m", "jable_strm"]
