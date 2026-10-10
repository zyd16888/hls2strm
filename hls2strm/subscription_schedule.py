"""订阅的五段 cron、时区与旧分钟周期兼容。"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_TIMEZONE = "Asia/Shanghai"


def next_cron_at(cron: str, timezone: str, after: float) -> int:
    base = datetime.fromtimestamp(after, ZoneInfo(timezone))
    return int(croniter(cron, base).get_next(datetime).timestamp())


class ScheduleFields(BaseModel):
    # None 是旧格式；空字符串明确表示仅手动。显式 cron 不再使用 interval。
    cron: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    interval: int = Field(60, ge=0)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from None
        return value

    @field_validator("cron")
    @classmethod
    def check_cron(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if value and (len(value.split()) != 5 or not croniter.is_valid(value)):
            raise ValueError("cron 必须为五段：分 时 日 月 周，例如 15 3 * * *")
        return value

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.cron is not None:
            self.interval = 0
        if self.cron:
            try:
                next_cron_at(self.cron, self.timezone, datetime.now().timestamp())
            except ValueError:
                raise ValueError("cron 没有有效的下次执行时间，请检查日期组合") from None
        return self


def has_schedule(sub: dict) -> bool:
    return bool(sub["cron"]) if sub.get("cron") is not None else sub["interval"] > 0


def scheduled_after(sub: dict, after: float) -> int:
    """cron 按墙上时间触发；旧周期保持按上次运行时间累加。"""
    if sub.get("cron") is not None:
        return next_cron_at(sub["cron"], sub["timezone"], after) if sub["cron"] else 0
    if sub["interval"] <= 0:
        return 0
    return int(sub["last_run_at"] + sub["interval"] * 60) if sub["last_run_at"] else int(after)


def schedule_anchor(sub: dict, started_at: float) -> float:
    return max(sub["last_run_at"] or 0, sub["created_at"], sub["schedule_updated_at"], started_at)


def next_run_at(sub: dict, current: float, started_at: float = 0) -> int | None:
    if not sub["enabled"] or not sub["initialized"] or not has_schedule(sub):
        return None
    return max(int(current), scheduled_after(sub, schedule_anchor(sub, started_at)))


async def migrate_v14(conn) -> None:
    # 旧订阅保持分钟调度，不强行近似成无法表达相同间隔的 cron。
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN cron TEXT")
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN schedule_updated_at INTEGER NOT NULL DEFAULT 0")
