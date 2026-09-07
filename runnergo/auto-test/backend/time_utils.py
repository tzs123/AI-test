"""任务时间统一处理：数据库存 UTC，展示时转换为配置的本地时区。"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo


DISPLAY_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
_DISPLAY_ZONE = ZoneInfo(DISPLAY_TIMEZONE)
_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime(_TEXT_FORMAT)


def localize_utc_timestamp(value: Any) -> Any:
    """将数据库中的 UTC 时间文本转换为展示时区，非时间值原样返回。"""
    if value in (None, "", "-"):
        return value
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_DISPLAY_ZONE).strftime(_TEXT_FORMAT)


def localize_task_timestamps(task: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(task or {})
    if result.get("time_zone") == DISPLAY_TIMEZONE:
        return result
    for field in ("started_at", "finished_at"):
        if field in result:
            result[field] = localize_utc_timestamp(result.get(field))
    result["time_zone"] = DISPLAY_TIMEZONE
    return result
