import os

# 镜像构建时注入（git tag 或 nightly-<短 sha>），本地运行用包版本
__version__ = os.environ.get("HLS2STRM_VERSION") or os.environ.get("JABLE_VERSION") or "0.1.0"
