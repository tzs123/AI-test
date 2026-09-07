"""日志自动清理模块。

支持的清理目录：
- logs/*.log          任务日志
- logs/workers/*.log  worker 日志
- screenshots/*       测试截图
- reports/allure-results/*  Allure 结果数据
- reports/allure-report/*   Allure HTML 报告
- reports/*.html      旧 HTML 报告

不清理：runtime/（含数据库 platform.db 和 jobs.json）

配置存储：runtime/log_cleanup_config.json
{
  "enabled": true,          # 是否启用自动清理
  "retention_days": 30,     # 保留天数
  "last_run": "2026-07-06", # 上次清理日期
}
"""
import os
import json
import time
import glob
from . import settings


# 清理目标目录配置：(目录路径, 描述, 文件匹配模式)
_CLEANUP_TARGETS = [
    ("logs", "任务日志", "*.log"),
    ("logs/workers", "Worker 日志", "*.log"),
    ("screenshots", "测试截图", "*"),
    ("reports/allure-results", "Allure 结果", "*"),
    ("reports/allure-report", "Allure 报告", "*"),
]

# 单独处理的报告 HTML 文件
_REPORT_HTML_FILES = ["report.html", "test_report.html"]

_CONFIG_PATH = os.path.join(settings.RUNTIME_DIR, "log_cleanup_config.json")

_DEFAULT_CONFIG = {
    "enabled": False,
    "retention_days": 30,
    "last_run": "",
}


def _load_config() -> dict:
    """加载清理配置，缺失字段用默认值补全。"""
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                cfg.update(loaded)
    except Exception:
        pass
    return cfg


def _save_config(cfg: dict):
    """保存清理配置。"""
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_config() -> dict:
    """获取当前清理配置。"""
    return _load_config()


def set_config(enabled: bool = None, retention_days: int = None) -> dict:
    """更新清理配置（只更新非 None 的字段）。"""
    cfg = _load_config()
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if retention_days is not None:
        cfg["retention_days"] = max(1, int(retention_days))
    _save_config(cfg)
    return cfg


def _dir_size_and_count(dir_path: str, pattern: str = "*") -> dict:
    """统计目录大小、文件数、最早/最新修改时间。"""
    result = {"size_bytes": 0, "file_count": 0, "oldest": "", "newest": ""}
    try:
        files = glob.glob(os.path.join(dir_path, pattern))
        for fp in files:
            if not os.path.isfile(fp):
                continue
            try:
                stat = os.stat(fp)
                result["size_bytes"] += stat.st_size
                result["file_count"] += 1
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
                if not result["oldest"] or mtime < result["oldest"]:
                    result["oldest"] = mtime
                if not result["newest"] or mtime > result["newest"]:
                    result["newest"] = mtime
            except Exception:
                pass
    except Exception:
        pass
    return result


def _format_size(size_bytes: int) -> str:
    """字节大小转人类可读格式。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_stats() -> dict:
    """获取所有日志目录的统计信息。"""
    stats = []
    total_size = 0
    total_files = 0
    for rel_path, desc, pattern in _CLEANUP_TARGETS:
        abs_path = os.path.join(settings.ROOT, rel_path)
        info = _dir_size_and_count(abs_path, pattern)
        info["path"] = rel_path
        info["description"] = desc
        info["size_human"] = _format_size(info["size_bytes"])
        stats.append(info)
        total_size += info["size_bytes"]
        total_files += info["file_count"]

    # 单独统计 reports 下的 HTML 文件
    report_html_size = 0
    report_html_count = 0
    for html_file in _REPORT_HTML_FILES:
        fp = os.path.join(settings.ROOT, "reports", html_file)
        if os.path.exists(fp) and os.path.isfile(fp):
            try:
                stat = os.stat(fp)
                report_html_size += stat.st_size
                report_html_count += 1
            except Exception:
                pass
    if report_html_count > 0:
        stats.append({
            "path": "reports/*.html",
            "description": "HTML 报告",
            "size_bytes": report_html_size,
            "size_human": _format_size(report_html_size),
            "file_count": report_html_count,
            "oldest": "",
            "newest": "",
        })
        total_size += report_html_size
        total_files += report_html_count

    return {
        "targets": stats,
        "total_size_bytes": total_size,
        "total_size_human": _format_size(total_size),
        "total_files": total_files,
        "config": _load_config(),
    }


def _is_file_older_than(filepath: str, retention_days: int) -> bool:
    """判断文件是否超过保留天数。"""
    try:
        stat = os.stat(filepath)
        age_seconds = time.time() - stat.st_mtime
        return age_seconds > retention_days * 86400
    except Exception:
        return False


def _safe_remove(filepath: str) -> bool:
    """安全删除文件，跳过正在写入的文件。"""
    try:
        # 尝试重命名来检测文件是否正在被写入
        # 如果重命名失败，说明文件被占用，跳过删除
        tmp = filepath + ".cleanup_tmp"
        os.rename(filepath, tmp)
        os.remove(tmp)
        return True
    except OSError:
        # 文件被占用，跳过
        return False
    except Exception:
        return False


def cleanup_old_logs(retention_days: int = 30, dry_run: bool = False) -> dict:
    """清理超过保留天数的日志文件。

    参数：
    - retention_days: 保留最近多少天的日志
    - dry_run: 试运行，只统计不实际删除

    返回每个目录的清理统计。
    """
    results = []
    total_deleted = 0
    total_freed_bytes = 0

    for rel_path, desc, pattern in _CLEANUP_TARGETS:
        abs_path = os.path.join(settings.ROOT, rel_path)
        deleted = 0
        freed_bytes = 0
        skipped = 0

        files = glob.glob(os.path.join(abs_path, pattern))
        for fp in files:
            if not os.path.isfile(fp):
                continue
            if not _is_file_older_than(fp, retention_days):
                continue
            try:
                file_size = os.path.getsize(fp)
            except Exception:
                file_size = 0

            if dry_run:
                deleted += 1
                freed_bytes += file_size
                continue

            if _safe_remove(fp):
                deleted += 1
                freed_bytes += file_size
            else:
                skipped += 1

        results.append({
            "path": rel_path,
            "description": desc,
            "deleted": deleted,
            "skipped": skipped,
            "freed_bytes": freed_bytes,
            "freed_human": _format_size(freed_bytes),
        })
        total_deleted += deleted
        total_freed_bytes += freed_bytes

    # 清理 reports 下的 HTML 文件
    for html_file in _REPORT_HTML_FILES:
        fp = os.path.join(settings.ROOT, "reports", html_file)
        if os.path.exists(fp) and os.path.isfile(fp) and _is_file_older_than(fp, retention_days):
            try:
                file_size = os.path.getsize(fp)
            except Exception:
                file_size = 0
            if dry_run or _safe_remove(fp):
                # 添加到 reports HTML 统计
                html_result = next((r for r in results if r["path"] == "reports/*.html"), None)
                if html_result:
                    html_result["deleted"] += 1
                    html_result["freed_bytes"] += file_size
                    html_result["freed_human"] = _format_size(html_result["freed_bytes"])
                else:
                    results.append({
                        "path": "reports/*.html",
                        "description": "HTML 报告",
                        "deleted": 1,
                        "skipped": 0,
                        "freed_bytes": file_size,
                        "freed_human": _format_size(file_size),
                    })
                total_deleted += 1
                total_freed_bytes += file_size

    # 更新最后清理时间
    if not dry_run:
        cfg = _load_config()
        cfg["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_config(cfg)

    return {
        "retention_days": retention_days,
        "dry_run": dry_run,
        "results": results,
        "total_deleted": total_deleted,
        "total_freed_bytes": total_freed_bytes,
        "total_freed_human": _format_size(total_freed_bytes),
    }


def auto_cleanup_if_needed():
    """自动清理（由 scheduler 每日调用）。

    读取配置，如果开启自动清理，则按配置的保留天数清理。
    """
    cfg = _load_config()
    if not cfg.get("enabled"):
        return {"skipped": True, "reason": "自动清理未启用"}

    retention_days = cfg.get("retention_days", 30)
    result = cleanup_old_logs(retention_days=retention_days, dry_run=False)
    return {"skipped": False, "cleanup_result": result}
