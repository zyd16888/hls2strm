"""抓取和解析的异常，各站点适配器共用。"""


class FetchError(Exception):
    """可重试的失败：网络错误、超时、5xx 等。"""


class NotFound(Exception):
    """页面不存在（404），不再重试。"""


class Blocked(Exception):
    """所有渠道都被拦截。"""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(5.0, retry_after)


class ParseError(Exception):
    """页面结构不符合预期（改版等）。"""


class VideoGone(ParseError):
    """影片已下架：站点返回 200 但只是兜底页，没有播放器。"""


class RelayAborted(Exception):
    """中转传到一半上游断开：已经记过一行日志，抛出去只为让 uvicorn 断开连接（播放器会重试），不用再打 traceback。"""
