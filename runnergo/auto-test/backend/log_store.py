"""按任务存储/读取执行日志（logs/<task_id>.log）。"""
import os
from . import settings
from .safe_paths import safe_child_path, safe_identifier


def _path(task_id: str) -> str:
    settings.ensure_dirs()
    task_id = safe_identifier(task_id, label="task_id")
    return safe_child_path(
        settings.LOGS_DIR,
        f"{task_id}.log",
        allowed_suffixes=(".log",),
        filename_only=True,
    )


def write(task_id: str, msg: str):
    with open(_path(task_id), "a", encoding="utf-8") as f:
        f.write(msg if msg.endswith("\n") else msg + "\n")


def read(task_id: str) -> str:
    try:
        with open(_path(task_id), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def delete(task_id: str):
    """删除指定任务的日志文件。"""
    p = _path(task_id)
    if os.path.isfile(p):
        os.remove(p)


def list_logs() -> list:
    """列出所有日志文件名（去掉 .log）。"""
    try:
        return sorted(
            f[:-4] for f in os.listdir(settings.LOGS_DIR) if f.endswith(".log")
        )
    except FileNotFoundError:
        return []
