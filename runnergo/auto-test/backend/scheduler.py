"""定时任务调度（APScheduler），支持从 DB 增删查 + 启停。"""
import json
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, executor

_scheduler: BackgroundScheduler = None


def _run_job(job_id: str):
    """定时任务触发：创建并派发任务。"""
    rows = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,), fetch=True)
    if not rows:
        return
    job = db.to_dict(rows[0])
    if not job.get("enabled") or job.get("status") in {"PAUSED", "COMPLETED"}:
        return
    trigger_type = str(job.get("trigger_type") or "CRON").upper()
    repeat_count = max(0, int(job.get("repeat_count") or 0))
    total_runs = max(0, int(job.get("total_runs") or 0))
    if repeat_count > 0 and total_runs >= repeat_count:
        db.execute(
            "UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?",
            (_now(), job_id),
        )
        reload_jobs()
        return
    case_files = json.loads(job.get("case_files") or "[]")
    if job.get("task_type") == "TEST_SUITE":
        suite_rows = db.execute(
            "SELECT case_files FROM test_suites WHERE id=? AND project_id=?",
            (job.get("suite_id") or "", job["project_id"]),
            fetch=True,
        )
        if not suite_rows:
            return
        case_files = json.loads(db.to_dict(suite_rows[0]).get("case_files") or "[]")
    task_id = executor.create_task(
        project_id=job["project_id"], module="ui",
        case_files=case_files, triggered_by=f"{trigger_type.lower()}:{job_id}",
        scheduled_job_id=job_id,
        suite_id=job.get("suite_id") or "",
    )
    executor.dispatch(task_id)
    next_total_runs = total_runs + 1
    db.execute(
        "UPDATE jobs SET last_run=?,total_runs=COALESCE(total_runs,0)+?,"
        "last_result=?,updated_at=? WHERE id=?",
        (_now(), 1, json.dumps({"task_ids": [task_id]}, ensure_ascii=False), _now(), job_id),
    )
    if trigger_type == "ONCE" or (repeat_count > 0 and next_total_runs >= repeat_count):
        db.execute(
            "UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?",
            (_now(), job_id),
        )
        reload_jobs()
    elif trigger_type == "COUNT":
        reload_jobs()


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _cron_trigger(expr: str):
    parts = [part.strip() for part in str(expr or "").split() if part.strip()]
    parts = ["*" if part == "?" else part for part in parts]
    if len(parts) == 5:
        return CronTrigger.from_crontab(" ".join(parts))
    if len(parts) == 6:
        second, minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            second=second,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )
    raise ValueError("Cron 表达式必须为 5 或 6 段")


def _parse_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("执行时间不能为空")
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError("执行时间格式无效") from exc


def _build_trigger(job: dict):
    trigger_type = str(job.get("trigger_type") or "CRON").upper()
    if trigger_type == "CRON":
        return _cron_trigger(job.get("cron") or "")
    if trigger_type == "INTERVAL":
        interval_seconds = max(60, int(job.get("interval_seconds") or 3600))
        return IntervalTrigger(seconds=interval_seconds)
    if trigger_type == "ONCE":
        run_date = _parse_datetime(job.get("execute_at") or "")
        if run_date <= datetime.now(run_date.tzinfo):
            raise ValueError("单次执行时间已过期")
        return DateTrigger(run_date=run_date)
    if trigger_type == "COUNT":
        repeat_count = max(0, int(job.get("repeat_count") or 0))
        total_runs = max(0, int(job.get("total_runs") or 0))
        if repeat_count <= 0:
            raise ValueError("执行次数必须大于0")
        if total_runs >= repeat_count:
            raise ValueError("执行次数已完成")
        return DateTrigger(run_date=datetime.now() + timedelta(seconds=1))
    raise ValueError("触发器类型无效")


def _auto_log_cleanup():
    """每日自动清理旧日志（由 scheduler 每日凌晨 3:00 调用）。

    读取 runtime/log_cleanup_config.json 配置：
    - enabled=False 时跳过
    - enabled=True 时按 retention_days 清理
    """
    try:
        from . import log_cleanup
        result = log_cleanup.auto_cleanup_if_needed()
        print(f"[scheduler] 日志自动清理: {result}", flush=True)
    except Exception as e:
        print(f"[scheduler] 日志自动清理异常: {e}", flush=True)


def _sync_jobs():
    """根据 DB 重建所有启用的 job。"""
    if _scheduler is None:
        return
    # 移除已存在的本调度 job
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("job-"):
            job.remove()
    rows = db.execute("SELECT * FROM jobs", fetch=True) or []
    for r in rows:
        job = db.to_dict(r)
        if (
            not job.get("enabled")
            or job.get("status") in {"PAUSED", "COMPLETED"}
            or job.get("module") == "api"
        ):
            continue
        try:
            trigger = _build_trigger(job)
        except Exception as exc:
            if str(job.get("trigger_type") or "CRON").upper() == "ONCE" and "已过期" in str(exc):
                db.execute(
                    "UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?",
                    (_now(), job["id"]),
                )
            print(f"[scheduler] 跳过无效定时任务 {job.get('id')}: {exc}", flush=True)
            continue
        repeat_count = max(0, int(job.get("repeat_count") or 0))
        total_runs = max(0, int(job.get("total_runs") or 0))
        if repeat_count > 0 and total_runs >= repeat_count:
            db.execute(
                "UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?",
                (_now(), job["id"]),
            )
            continue
        _scheduler.add_job(
            _run_job,
            trigger,
            id=f"job-{job['id']}",
            args=[job["id"]],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.start()
    _sync_jobs()
    # 注册每日日志自动清理 job（凌晨 3:00 执行）
    _scheduler.add_job(
        _auto_log_cleanup,
        CronTrigger(hour=3, minute=0),
        id="auto-log-cleanup",
        replace_existing=True,
    )
    return _scheduler


def reload_jobs():
    _sync_jobs()
