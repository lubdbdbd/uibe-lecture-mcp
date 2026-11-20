from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

CN = timezone(timedelta(hours=8))
ORIGIN = "https://ea.uibe.edu.cn"


def now() -> datetime:
    return datetime.now(CN)


def dt(value: str | datetime) -> datetime:
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return d.replace(tzinfo=CN) if d.tzinfo is None else d.astimezone(CN)


def class_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("课程 ID 格式错误")
    return value


class Lecture(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    opens: datetime
    closes: datetime
    credits: float
    platform: str = ""
    category: str = ""
    organizer: str = ""
    status: str = ""
    detail_url: str
    apply_url: str

    _id = field_validator("id")(class_id)
    _dates = field_validator("start", "end", "opens", "closes", mode="before")(dt)


class Busy(BaseModel):
    start: datetime
    end: datetime
    title: str = "不可用时间"

    _dates = field_validator("start", "end", mode="before")(dt)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class Criteria(BaseModel):
    keywords_any: list[str] = Field(default_factory=list)
    keywords_all: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    organizers: list[str] = Field(default_factory=list)
    date_from: datetime | None = None
    date_until: datetime | None = None
    weekdays: list[int] = Field(default_factory=list, description="周一=1，周日=7")
    time_from: str | None = None
    time_until: str | None = None
    min_credits: float = Field(0, ge=0)
    max_duration_minutes: int | None = Field(None, gt=0)
    busy: list[Busy] = Field(default_factory=list)
    ignore_conflicts: bool = Field(False, description="用户明确要求不考虑时间冲突时设为 true")

    @field_validator("date_from", "date_until", mode="before")
    @classmethod
    def dates(cls, v):
        return dt(v) if v else None

    @field_validator("weekdays")
    @classmethod
    def days(cls, v):
        if any(x < 1 or x > 7 for x in v):
            raise ValueError("weekdays 取值为 1–7")
        return v

    @field_validator("time_from", "time_until")
    @classmethod
    def times(cls, v):
        if v is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("时间须为 HH:MM")
        return v

    @model_validator(mode="after")
    def bounds(self):
        if self.date_from and self.date_until and self.date_until <= self.date_from:
            raise ValueError("date_until 必须晚于 date_from")
        if self.time_from and self.time_until and self.time_until <= self.time_from:
            raise ValueError("每日时间窗口应在同一天内")
        return self


def overlaps(a, b) -> bool:
    return a.start < b.end and b.start < a.end


def matches(c: Lecture, f: Criteria) -> bool:
    text = " ".join((c.title, c.platform, c.category, c.organizer)).casefold()
    return all((
        not f.keywords_any or any(k.casefold() in text for k in f.keywords_any),
        all(k.casefold() in text for k in f.keywords_all),
        not any(k.casefold() in text for k in f.exclude),
        not f.platforms or c.platform in f.platforms,
        not f.categories or c.category in f.categories,
        not f.organizers or c.organizer in f.organizers,
        not f.date_from or c.start >= f.date_from,
        not f.date_until or c.start < f.date_until,
        not f.weekdays or c.start.isoweekday() in f.weekdays,
        not f.time_from or c.start.strftime("%H:%M") >= f.time_from,
        not f.time_until or (c.start.date() == c.end.date() and c.end.strftime("%H:%M") <= f.time_until),
        c.credits >= f.min_credits,
        not f.max_duration_minutes or (c.end-c.start).total_seconds() <= f.max_duration_minutes*60,
        f.ignore_conflicts or not any(overlaps(c, b) for b in f.busy),
    ))


class Rule(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, description="用户的报名授权和具体筛选要求")
    criteria: Criteria = Field(default_factory=Criteria)
    expires: datetime
    mode: Literal["enroll", "monitor"] = "enroll"
    max_success: int = Field(1, ge=1, le=50)
    scan_seconds: int = Field(300, ge=60, le=86400)
    watch_minutes: int = Field(0, ge=0, le=1440)

    _dates = field_validator("expires", mode="before")(dt)

