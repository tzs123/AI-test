import json
import sqlite3
from datetime import datetime, timedelta

from backend import app as app_module
from backend import db as db_module
from backend import scheduler
from backend import settings


def _setup_job_save_test(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    db_path = runtime_dir / "platform.db"
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(settings, "DB_PATH", str(db_path))
    monkeypatch.setattr(
        app_module.project_service,
        "get_project",
        lambda project_id: {"id": project_id, "case_dir": "cases"},
    )
    monkeypatch.setattr(
        app_module.case_service,
        "list_cases",
        lambda project_id, module: [{"relative": "cases/ui/test_login.yaml"}],
    )
    monkeypatch.setattr(app_module.scheduler_module, "reload_jobs", lambda: None)
    db_module.init_db()
    return db_path


def _saved_job(db_path, job_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT trigger_type, cron, interval_seconds, execute_at, repeat_count, status "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def test_validate_job_trigger_infers_count_for_legacy_count_payload():
    body = app_module.JobIn(
        project_id="default",
        name="count job",
        cron="0 0 8 * * *",
        repeat_count=1,
    )

    assert app_module._validate_job_trigger(body) == ("COUNT", "", 3600, "")


def test_validate_job_trigger_infers_count_from_cron_with_repeat_count():
    body = app_module.JobIn(
        project_id="default",
        name="cron job",
        trigger_type="CRON",
        cron="0 0 8 * * *",
        repeat_count=1,
    )

    assert app_module._validate_job_trigger(body) == ("COUNT", "", 3600, "")


def test_validate_job_trigger_keeps_cron_without_repeat_count():
    body = app_module.JobIn(
        project_id="default",
        name="cron job",
        trigger_type="CRON",
        cron="0 0 8 * * *",
        repeat_count=0,
    )

    assert app_module._validate_job_trigger(body) == ("CRON", "0 0 8 * * *", 3600, "")


def test_create_job_saves_all_trigger_types(tmp_path, monkeypatch):
    db_path = _setup_job_save_test(tmp_path, monkeypatch)
    future_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    cases = [
        ("CRON", {"cron": "0 0 8 * * *", "repeat_count": 0}, {"trigger_type": "CRON", "cron": "0 0 8 * * *", "repeat_count": 0}),
        ("INTERVAL", {"interval_seconds": 600}, {"trigger_type": "INTERVAL", "cron": "", "interval_seconds": 600, "repeat_count": 0}),
        ("ONCE", {"execute_at": future_time}, {"trigger_type": "ONCE", "execute_at": future_time, "repeat_count": 0}),
        ("COUNT", {"repeat_count": 3}, {"trigger_type": "COUNT", "cron": "", "repeat_count": 3}),
    ]

    for trigger_type, payload, expected in cases:
        result = app_module.jobs_create(app_module.JobIn(
            project_id="default",
            name=f"{trigger_type} job",
            case_files=["cases/ui/test_login.yaml"],
            trigger_type=trigger_type,
            **payload,
        ))

        row = _saved_job(db_path, result["id"])
        for field, value in expected.items():
            assert row[field] == value


def test_update_job_saves_trigger_type_switches(tmp_path, monkeypatch):
    db_path = _setup_job_save_test(tmp_path, monkeypatch)
    created = app_module.jobs_create(app_module.JobIn(
        project_id="default",
        name="editable job",
        case_files=["cases/ui/test_login.yaml"],
        trigger_type="CRON",
        cron="0 0 8 * * *",
        repeat_count=0,
    ))
    job_id = created["id"]
    future_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    updates = [
        ("INTERVAL", {"interval_seconds": 900}, {"trigger_type": "INTERVAL", "interval_seconds": 900, "repeat_count": 0}),
        ("ONCE", {"execute_at": future_time}, {"trigger_type": "ONCE", "execute_at": future_time, "repeat_count": 0}),
        ("COUNT", {"repeat_count": 5, "cron": "0 0 8 * * *"}, {"trigger_type": "COUNT", "cron": "", "repeat_count": 5}),
    ]
    for trigger_type, payload, expected in updates:
        app_module.jobs_update(job_id, app_module.JobIn(
            project_id="default",
            name="editable job",
            case_files=["cases/ui/test_login.yaml"],
            trigger_type=trigger_type,
            **payload,
        ))

        row = _saved_job(db_path, job_id)
        for field, value in expected.items():
            assert row[field] == value


def test_legacy_jobs_without_trigger_type_migrate_repeat_count_to_count(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    db_path = runtime_dir / "platform.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jobs ("
        "id TEXT PRIMARY KEY, project_id TEXT, name TEXT, module TEXT, "
        "case_files TEXT, cron TEXT, enabled INTEGER DEFAULT 1, last_run TEXT, "
        "repeat_count INTEGER DEFAULT 0, total_runs INTEGER DEFAULT 0, status TEXT DEFAULT 'ACTIVE'"
        ")"
    )
    conn.execute(
        "INSERT INTO jobs(id,project_id,name,module,case_files,cron,repeat_count,total_runs,status) "
        "VALUES('job1','default','count job','ui','[]','0 0 8 * * *',3,0,'ACTIVE')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(settings, "DB_PATH", str(db_path))

    db_module.init_db()

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT trigger_type, cron, repeat_count FROM jobs WHERE id='job1'").fetchone()
    conn.close()
    assert row == ("COUNT", "", 3)


def test_scheduled_job_dispatches_once_and_completes_at_repeat_limit(monkeypatch):
    job = {
        "id": "job1",
        "project_id": "default",
        "module": "ui",
        "task_type": "TEST_CASE",
        "case_files": json.dumps(["test_login.yaml"]),
        "suite_id": "",
        "enabled": 1,
        "status": "ACTIVE",
        "trigger_type": "CRON",
        "repeat_count": 2,
        "total_runs": 1,
    }
    created = []
    dispatched = []

    def fake_execute(sql, params=(), fetch=False):
        if sql.startswith("SELECT * FROM jobs"):
            return [job] if fetch else None
        if sql.startswith("UPDATE jobs SET last_run="):
            job["last_run"] = params[0]
            job["total_runs"] += params[1]
            job["last_result"] = params[2]
            return None
        if sql.startswith("UPDATE jobs SET enabled=0,status='COMPLETED'"):
            job["enabled"] = 0
            job["status"] = "COMPLETED"
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(scheduler.db, "execute", fake_execute)
    monkeypatch.setattr(scheduler.db, "to_dict", lambda row: row)
    monkeypatch.setattr(scheduler, "_now", lambda: "2026-07-28 12:00:00")
    monkeypatch.setattr(scheduler, "reload_jobs", lambda: None)
    monkeypatch.setattr(
        scheduler.executor,
        "create_task",
        lambda **kwargs: created.append(kwargs) or "task-1",
    )
    monkeypatch.setattr(scheduler.executor, "dispatch", lambda task_id: dispatched.append(task_id))

    scheduler._run_job("job1")

    assert len(created) == 1
    assert created[0]["case_files"] == ["test_login.yaml"]
    assert created[0]["triggered_by"] == "cron:job1"
    assert dispatched == ["task-1"]
    assert job["total_runs"] == 2
    assert job["enabled"] == 0
    assert job["status"] == "COMPLETED"
    assert json.loads(job["last_result"]) == {"task_ids": ["task-1"]}


def test_scheduled_job_with_zero_repeat_limit_keeps_running(monkeypatch):
    job = {
        "id": "job1",
        "project_id": "default",
        "module": "ui",
        "task_type": "TEST_CASE",
        "case_files": json.dumps(["test_login.yaml"]),
        "suite_id": "",
        "enabled": 1,
        "status": "ACTIVE",
        "trigger_type": "CRON",
        "repeat_count": 0,
        "total_runs": 99,
    }
    completed_updates = []

    def fake_execute(sql, params=(), fetch=False):
        if sql.startswith("SELECT * FROM jobs"):
            return [job] if fetch else None
        if sql.startswith("UPDATE jobs SET last_run="):
            job["total_runs"] += params[1]
            job["last_result"] = params[2]
            return None
        if sql.startswith("UPDATE jobs SET enabled=0,status='COMPLETED'"):
            completed_updates.append(params)
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(scheduler.db, "execute", fake_execute)
    monkeypatch.setattr(scheduler.db, "to_dict", lambda row: row)
    monkeypatch.setattr(scheduler, "_now", lambda: "2026-07-28 12:00:00")
    monkeypatch.setattr(scheduler.executor, "create_task", lambda **kwargs: "task-1")
    monkeypatch.setattr(scheduler.executor, "dispatch", lambda task_id: None)

    scheduler._run_job("job1")

    assert job["total_runs"] == 100
    assert job["enabled"] == 1
    assert job["status"] == "ACTIVE"
    assert completed_updates == []


def test_count_trigger_dispatches_one_run_then_reschedules(monkeypatch):
    job = {
        "id": "job1",
        "project_id": "default",
        "module": "ui",
        "task_type": "TEST_CASE",
        "case_files": json.dumps(["test_login.yaml"]),
        "suite_id": "",
        "enabled": 1,
        "status": "ACTIVE",
        "trigger_type": "COUNT",
        "repeat_count": 3,
        "total_runs": 1,
    }
    dispatched = []
    reloads = []

    def fake_execute(sql, params=(), fetch=False):
        if sql.startswith("SELECT * FROM jobs"):
            return [job] if fetch else None
        if sql.startswith("UPDATE jobs SET last_run="):
            job["total_runs"] += params[1]
            job["last_result"] = params[2]
            return None
        if sql.startswith("UPDATE jobs SET enabled=0,status='COMPLETED'"):
            job["enabled"] = 0
            job["status"] = "COMPLETED"
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(scheduler.db, "execute", fake_execute)
    monkeypatch.setattr(scheduler.db, "to_dict", lambda row: row)
    monkeypatch.setattr(scheduler, "_now", lambda: "2026-07-28 12:00:00")
    monkeypatch.setattr(scheduler, "reload_jobs", lambda: reloads.append("reload"))
    monkeypatch.setattr(scheduler.executor, "create_task", lambda **kwargs: "task-1")
    monkeypatch.setattr(scheduler.executor, "dispatch", lambda task_id: dispatched.append(task_id))

    scheduler._run_job("job1")

    assert dispatched == ["task-1"]
    assert job["total_runs"] == 2
    assert job["enabled"] == 1
    assert job["status"] == "ACTIVE"
    assert reloads == ["reload"]
