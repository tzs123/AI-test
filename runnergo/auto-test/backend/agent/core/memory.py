"""核心记忆：任务级记忆复用 agent.memory，并新增项目级长期记忆。

项目级记忆保存：项目结构、接口信息、页面信息、历史失败，
供下一次测试自动读取（scope="project"，agent_task_id="project:<id>"）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from backend import db
from backend.agent.memory import clear_memory, list_memory, recall, remember  # noqa: F401


def _project_key(project_id: str) -> str:
    return f"project:{project_id or 'default'}"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def remember_project(project_id: str, key: str, value: Any) -> None:
    """写入项目级记忆（upsert）。"""
    if not project_id or not key:
        return
    now = _now()
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    db.execute(
        "INSERT INTO agent_memory(agent_task_id, scope, key, value, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(agent_task_id, scope, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_project_key(project_id), "project", key, payload, now, now),
    )


def recall_project(project_id: str, key: str, default: Any = None) -> Any:
    """读取项目级记忆。"""
    rows = db.execute(
        "SELECT value FROM agent_memory WHERE agent_task_id=? AND scope='project' AND key=?",
        (_project_key(project_id), key),
        fetch=True,
    )
    if not rows:
        return default
    raw = rows[0]["value"]
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def list_project_memory(project_id: str) -> list[dict]:
    """列出项目级全部记忆。"""
    rows = db.execute(
        "SELECT id, key, value, created_at, updated_at "
        "FROM agent_memory WHERE agent_task_id=? AND scope='project' ORDER BY id",
        (_project_key(project_id),),
        fetch=True,
    ) or []
    items = []
    for row in rows:
        item = db.to_dict(row)
        try:
            item["value"] = json.loads(item["value"])
        except (TypeError, ValueError):
            pass
        items.append(item)
    return items


def remember_system_fact(
    project_id: str,
    fact_type: str,
    content: Any,
) -> None:
    """保存系统事实（api/page/failure/structure），带时间戳，按 key 累计。"""
    if not project_id:
        return
    key = f"fact:{fact_type}"
    history = recall_project(project_id, key, [])
    if not isinstance(history, list):
        history = []
    history.append({"time": _now(), "content": content})
    history = history[-200:]
    remember_project(project_id, key, history)
