"""Agent 任务记忆：跨步骤保存上下文、决策与可复用资产引用。

存储到 agent_memory 表，按 (agent_task_id, scope, key) 唯一。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from backend import db


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def remember(
    agent_task_id: str,
    key: str,
    value: Any,
    *,
    scope: str = "task",
) -> None:
    """写入一条记忆（upsert）。"""
    if not agent_task_id or not key:
        return
    now = _now()
    db.execute(
        "INSERT INTO agent_memory(agent_task_id, scope, key, value, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(agent_task_id, scope, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (agent_task_id, scope or "task", key, _dump(value), now, now),
    )


def recall(
    agent_task_id: str,
    key: str,
    *,
    scope: str = "task",
    default: Any = None,
) -> Any:
    """读取一条记忆；不存在时返回 default。"""
    rows = db.execute(
        "SELECT value FROM agent_memory WHERE agent_task_id=? AND scope=? AND key=?",
        (agent_task_id, scope or "task", key),
        fetch=True,
    )
    return _load(rows[0]["value"]) if rows else default


def list_memory(agent_task_id: str) -> list[dict]:
    """列出任务全部记忆。"""
    rows = db.execute(
        "SELECT id, scope, key, value, created_at, updated_at "
        "FROM agent_memory WHERE agent_task_id=? ORDER BY id",
        (agent_task_id,),
        fetch=True,
    ) or []
    items = []
    for row in rows:
        item = db.to_dict(row)
        item["value"] = _load(item.get("value"))
        items.append(item)
    return items


def clear_memory(agent_task_id: str) -> None:
    """清空任务记忆。"""
    db.execute("DELETE FROM agent_memory WHERE agent_task_id=?", (agent_task_id,))
