"""按任务持久化 UI 场景的逐步骤执行状态。

执行器运行在 pytest 子进程中，页面查询运行在 FastAPI 进程中，因此这里使用
带文件锁的 JSON 文件作为轻量跨进程状态存储。写入采用临时文件替换，避免页面
轮询时读到半截 JSON。
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from . import settings

try:
    import fcntl
except ImportError:  # pragma: no cover - 正式运行环境为 Linux/macOS
    fcntl = None


_LOCAL_LOCK = threading.RLock()
ASSERTION_ACTIONS = frozenset({
    "assert_visible",
    "assert_text",
    "assert_url",
    "assert_error",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_task_id(task_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "", str(task_id or ""))
    if not value:
        raise ValueError("task_id 不能为空")
    return value


def _directory() -> str:
    path = os.path.join(settings.RUNTIME_DIR, "task_steps")
    os.makedirs(path, exist_ok=True)
    return path


def _path(task_id: str) -> str:
    return os.path.join(_directory(), f"{_safe_task_id(task_id)}.json")


def _lock_path(task_id: str) -> str:
    return os.path.join(_directory(), f"{_safe_task_id(task_id)}.lock")


@contextmanager
def _locked(task_id: str):
    with _LOCAL_LOCK:
        handle = open(_lock_path(task_id), "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _read_unlocked(task_id: str) -> Dict[str, Any]:
    try:
        with open(_path(task_id), "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_unlocked(task_id: str, payload: Dict[str, Any]):
    path = _path(task_id)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _assertion_counts(steps: list) -> Dict[str, int]:
    """聚合断言统计：明细逐条计数 + UI 断言步骤按命中状态计数。

    assert_error 命中时步骤状态为 failed 但 matched=True，业务语义是
    “结果符合预期”，因此按断言通过计数。
    """
    total = passed = failed = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        details = list(step.get("assertions") or [])
        diagnostics = step.get("diagnostics")
        if isinstance(diagnostics, dict):
            details.extend(diagnostics.get("assertions") or [])
        for item in details:
            if not isinstance(item, dict):
                continue
            total += 1
            if item.get("passed"):
                passed += 1
            else:
                failed += 1
        if step.get("is_assertion") and step.get("status") in {"passed", "failed"}:
            total += 1
            matched = step.get("assertion_matched")
            if matched is True:
                passed += 1
            elif matched is False:
                failed += 1
            elif step.get("status") == "passed":
                passed += 1
            else:
                failed += 1
    return {"total": total, "passed": passed, "failed": failed}


def _summarize(payload: Dict[str, Any]) -> Dict[str, Any]:
    steps = payload.get("steps") or []
    counts = {name: 0 for name in ("pending", "running", "passed", "failed", "skipped")}
    current_index = None
    for step in steps:
        status = str(step.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        if status == "running":
            current_index = step.get("index")

    iterations = payload.get("iterations") or []
    for iteration in iterations:
        iteration_steps = [
            step for step in steps
            if step.get("iteration_index") == iteration.get("index")
        ]
        iteration_counts = {
            name: sum(1 for step in iteration_steps if step.get("status") == name)
            for name in counts
        }
        iteration["total"] = len(iteration_steps)
        iteration["completed"] = sum(
            iteration_counts.get(name, 0) for name in ("passed", "failed", "skipped")
        )
        iteration["counts"] = iteration_counts
        if iteration_counts.get("failed"):
            iteration["status"] = "failed"
        elif iteration_steps and iteration_counts.get("skipped") == len(iteration_steps):
            iteration["status"] = "skipped"
        elif iteration_steps and iteration["completed"] == len(iteration_steps):
            iteration["status"] = "passed"
        elif iteration_counts.get("running") or iteration.get("status") == "running":
            iteration["status"] = "running"
        else:
            iteration["status"] = "pending"

    payload["total"] = len(steps)
    payload["completed"] = counts.get("passed", 0) + counts.get("failed", 0) + counts.get("skipped", 0)
    payload["current_index"] = current_index
    payload["counts"] = counts
    payload["assertion_stats"] = _assertion_counts(steps)
    payload["completed_iterations"] = sum(
        1 for iteration in iterations
        if iteration.get("status") in {"passed", "failed", "skipped"}
    )
    payload["updated_at"] = _now()
    return payload


def _flow_steps(
    flow: Dict[str, Any],
    *,
    iteration_index: int = 1,
    iteration_count: int = 1,
    iteration_label: str = "",
) -> list[Dict[str, Any]]:
    steps = []
    source_steps = flow.get("steps") or []
    for index, step in enumerate(source_steps, start=1):
        value = step if isinstance(step, dict) else {}
        global_index = (
            (iteration_index - 1) * len(source_steps) + index
            if iteration_count > 1 else index
        )
        steps.append({
            "index": global_index,
            "step_index": index,
            "iteration_index": iteration_index if iteration_count > 1 else None,
            "iteration_label": iteration_label if iteration_count > 1 else "",
            "id": str(value.get("id") or f"step_{index}"),
            "action": str(value.get("action") or value.get("type") or ""),
            "name": str(value.get("name") or value.get("action") or value.get("type") or f"步骤 {index}"),
            "is_assertion": str(value.get("action") or value.get("type") or "").strip().lower() in ASSERTION_ACTIONS,
            "assertion_matched": None,
            "status": "pending",
            "started_at": "",
            "finished_at": "",
            "duration": 0,
            "error": "",
            "screenshot": "",
            "log": "",
            "diagnostics": {},
        })
    return steps


def initialize(
    task_id: str,
    flow: Dict[str, Any],
    case_file: str = "",
    *,
    iteration_index: int = 1,
    iteration_count: int = 1,
    iteration_label: str = "",
    iteration_data: Optional[Dict[str, Any]] = None,
    iteration_data_rows: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    iteration_index = max(1, int(iteration_index or 1))
    iteration_count = max(iteration_index, int(iteration_count or 1))
    iteration_label = str(iteration_label or f"data-{iteration_index}")

    if iteration_count <= 1:
        payload = _summarize({
            "task_id": _safe_task_id(task_id),
            "case_file": str(case_file or ""),
            "flow_name": str(flow.get("name") or case_file or "UI 场景"),
            "status": "running",
            "error": "",
            "started_at": _now(),
            "finished_at": "",
            "steps": _flow_steps(flow),
        })
        with _locked(task_id):
            _write_unlocked(task_id, payload)
        return payload

    with _locked(task_id):
        payload = _read_unlocked(task_id)
        should_reset = (
            not payload
            or payload.get("case_file") != str(case_file or "")
            or int(payload.get("iteration_count") or 0) != iteration_count
        )
        if should_reset:
            steps = []
            iterations = []
            data_rows = iteration_data_rows if isinstance(iteration_data_rows, list) else []
            for current_index in range(1, iteration_count + 1):
                label = f"data-{current_index}"
                row_data = data_rows[current_index - 1] if current_index <= len(data_rows) else {}
                steps.extend(_flow_steps(
                    flow,
                    iteration_index=current_index,
                    iteration_count=iteration_count,
                    iteration_label=label,
                ))
                iterations.append({
                    "index": current_index,
                    "label": label,
                    "status": "pending",
                    "data": row_data if isinstance(row_data, dict) else {},
                    "started_at": "",
                    "finished_at": "",
                    "error": "",
                })
            payload = {
                "task_id": _safe_task_id(task_id),
                "case_file": str(case_file or ""),
                "flow_name": str(flow.get("name") or case_file or "UI 场景"),
                "status": "running",
                "error": "",
                "started_at": _now(),
                "finished_at": "",
                "iteration_count": iteration_count,
                "iterations": iterations,
                "steps": steps,
            }

        iteration = next(
            (item for item in payload.get("iterations") or [] if item.get("index") == iteration_index),
            None,
        )
        if iteration is not None:
            iteration["label"] = iteration_label
            iteration["data"] = iteration_data if isinstance(iteration_data, dict) else {}
            iteration["status"] = "running"
            iteration["started_at"] = iteration.get("started_at") or _now()
            iteration["finished_at"] = ""
            iteration["error"] = ""
            for step in payload.get("steps") or []:
                if step.get("iteration_index") == iteration_index:
                    step["iteration_label"] = iteration_label

        payload["status"] = "running"
        payload["finished_at"] = ""
        payload = _summarize(payload)
        _write_unlocked(task_id, payload)
        return payload


def read(task_id: str) -> Dict[str, Any]:
    with _locked(task_id):
        return _read_unlocked(task_id)


def mutate(task_id: str, callback: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    with _locked(task_id):
        payload = _read_unlocked(task_id)
        if not payload:
            payload = {"task_id": _safe_task_id(task_id), "status": "pending", "steps": []}
        callback(payload)
        _summarize(payload)
        _write_unlocked(task_id, payload)
        return payload


def update_step(
    task_id: str,
    index: int,
    *,
    iteration_index: Optional[int] = None,
    **fields,
) -> Dict[str, Any]:
    def apply(payload: Dict[str, Any]):
        steps = payload.setdefault("steps", [])
        if iteration_index is not None:
            target = next((
                step for step in steps
                if step.get("iteration_index") == iteration_index
                and int(step.get("step_index") or 0) == index
            ), None)
        else:
            target = steps[index - 1] if 1 <= index <= len(steps) else None
        if target is None:
            return
        target.update(fields)
        if fields.get("status") == "running":
            payload["status"] = "running"

    return mutate(task_id, apply)


def finalize(
    task_id: str,
    status: str,
    error: str = "",
    *,
    iteration_index: Optional[int] = None,
) -> Dict[str, Any]:
    normalized = str(status or "failed")

    def apply(payload: Dict[str, Any]):
        if iteration_index is not None:
            iteration = next((
                item for item in payload.get("iterations") or []
                if item.get("index") == iteration_index
            ), None)
            if iteration is not None:
                iteration["status"] = "passed" if normalized == "success" else normalized
                iteration["finished_at"] = _now()
                if error:
                    iteration["error"] = str(error)
            relevant_steps = [
                step for step in payload.get("steps") or []
                if step.get("iteration_index") == iteration_index
            ]
            for step in relevant_steps:
                step_status = step.get("status")
                if step_status == "running" and normalized in {"failed", "stopped"}:
                    step["status"] = "failed" if normalized == "failed" else "skipped"
                    step["finished_at"] = step.get("finished_at") or _now()
                    step["error"] = step.get("error") or str(error or "执行被中断")
                elif step_status == "pending" and normalized in {"failed", "stopped"}:
                    step["status"] = "skipped"
            iteration_statuses = {
                item.get("status") for item in payload.get("iterations") or []
            }
            if "failed" in iteration_statuses:
                payload["status"] = "failed"
            elif iteration_statuses and iteration_statuses.issubset({"passed", "skipped"}):
                payload["status"] = "success"
                payload["finished_at"] = _now()
            else:
                payload["status"] = "running"
            return

        payload["status"] = normalized
        payload["finished_at"] = _now()
        if error:
            payload["error"] = str(error)
        for step in payload.get("steps") or []:
            step_status = step.get("status")
            if step_status == "running" and normalized in {"failed", "stopped"}:
                step["status"] = "failed" if normalized == "failed" else "skipped"
                step["finished_at"] = step.get("finished_at") or _now()
                step["error"] = step.get("error") or str(error or "执行被中断")
            elif step_status == "pending" and normalized in {"failed", "stopped"}:
                step["status"] = "skipped"

    return mutate(task_id, apply)


def delete(task_id: str):
    with _locked(task_id):
        for path in (_path(task_id), _lock_path(task_id)):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
