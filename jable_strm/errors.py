"""页面解析相关的异常，各站点适配器共用。"""


class ParseError(Exception):
    """页面结构不符合预期（改版等）。"""


class VideoGone(ParseError):
    """影片已下架：站点返回 200 但只是兜底页，没有播放器。"""
