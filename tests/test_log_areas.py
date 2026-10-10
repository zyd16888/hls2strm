import logging

from hls2strm.observability import RingHandler, log_area_of
from hls2strm.runtime import log_area, traffic


def record(name: str, msg: str = "x") -> logging.LogRecord:
    return logging.LogRecord(f"hls2strm.{name}", logging.INFO, __file__, 1, msg, (), None)


def test_log_areas():
    """播放请求、测速、连通性检测归播放区；后台任务归任务区；启动、接口操作归系统区。"""
    assert log_area_of("play") == "play" and log_area_of("health") == "play"
    assert log_area_of("engine") == "task" and log_area_of("sites.missav") == "task"
    assert log_area_of("api") == "system" and log_area_of("uvicorn") == "system"
    token = traffic.set("play")
    assert log_area_of("engine") == "play" and log_area_of("fetcher") == "play"  # 播放请求里抓页面也算播放
    traffic.reset(token)
    token = log_area.set("play")
    assert log_area_of("engine") == "play"  # 连通性检测
    log_area.reset(token)


def test_areas_are_kept_apart():
    """抓取刷屏时任务区只留最近 N 条，播放区的日志不会被挤掉；刚打开页面时每个区都补一些。"""
    ring = RingHandler(capacity=3)
    ring.emit(record("play", "播放"))
    for i in range(10):
        ring.emit(record("engine", f"任务 {i}"))
    assert [r["msg"] for r in ring.since(0, 100, "play")] == ["播放"]
    assert [r["msg"] for r in ring.since(0, 100, "task")] == ["任务 7", "任务 8", "任务 9"]
    assert [r["msg"] for r in ring.since(0, 100)] == ["播放", "任务 7", "任务 8", "任务 9"]
    assert [r["msg"] for r in ring.backlog(0, 2)] == ["播放", "任务 8", "任务 9"]
    assert all(r["area"] in ("play", "task") for r in ring.since(0, 100))
