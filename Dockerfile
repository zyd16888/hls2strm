FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HLS2STRM_DATA_DIR=/data \
    HLS2STRM_OUTPUT_DIR=/strm \
    HLS2STRM_PORT=8080 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 先只装依赖：pyproject 不变时这一层走缓存，改代码不用重装依赖（arm64 用 QEMU 构建时很慢）
COPY pyproject.toml README.md ./
RUN mkdir hls2strm && touch hls2strm/__init__.py \
    && pip install . && pip uninstall -y hls2strm && rm -rf hls2strm
COPY hls2strm ./hls2strm
RUN pip install --no-deps . && mkdir -p /data /strm

# 构建时注入版本号（CI 里是 git tag 或 nightly-<短 sha>），状态接口和页面上会显示
ARG VERSION=dev
ENV HLS2STRM_VERSION=${VERSION}

EXPOSE 8080
VOLUME ["/data", "/strm"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "-m", "hls2strm"]
