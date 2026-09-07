import sqlite3

import pytest
from fastapi import HTTPException

from backend import app, db, executor as executor_module, settings
from backend.executor import (
    _build_pytest_args,
    _case_files_for_execution,
    _load_test_data_asset_context,
    _mark_task_running,
    _py_to_yaml_case_file,
    _start_task_stop_monitor,
    _yaml_to_py,
    create_task,
    stop_task,
)


def _init_executor_db(tmp_path, monkeypatch):
    root = tmp_path / "auto-test"
    monkeypatch.setattr(settings, "ROOT", str(root))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(root / "runtime"))
    monkeypatch.setattr(settings, "DB_PATH", str(root / "runtime" / "platform.db"))
    monkeypatch.setattr(settings, "REPORT_DIR", str(root / "reports"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(root / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(root / "logs"))
    db.init_db()
    db.execute(
        "INSERT OR IGNORE INTO projects(id,name,description,base_url,case_dir,created_at) "
        "VALUES('default','Default','','','cases','2026-08-03 00:00:00')"
    )


def test_yaml_filename_maps_to_project_specific_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    test_file = tmp_path / "tests" / "project123" / "ui" / "test_recorded.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_recorded(): pass\n", encoding="utf-8")

    mapped = _yaml_to_py(
        "test_recorded.yaml",
        project_id="project123",
        module="ui",
    )

    assert mapped == "tests/project123/ui/test_recorded.py"


def test_build_pytest_args_uses_selected_project_case(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    test_file = tmp_path / "tests" / "project123" / "ui" / "test_recorded.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_recorded(): pass\n", encoding="utf-8")

    args = _build_pytest_args(
        "ui",
        ["test_recorded.yaml"],
        str(tmp_path / "allure-results"),
        project_id="project123",
    )

    assert "tests/project123/ui/test_recorded.py" in args
    assert "tests/__init__.py" not in args
    assert args[-2:] == ["-m", "ui"]


def test_pytest_target_maps_back_to_default_yaml_case(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text("value: ${dataAssets.generatedData.phone}\n", encoding="utf-8")

    mapped = _py_to_yaml_case_file(
        "tests/ui/test_login.py",
        project_id="default",
        module="ui",
    )

    assert mapped == "cases/ui/test_login.yaml"


def test_run_all_resolves_yaml_cases_for_data_asset_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    test_file = tmp_path / "tests" / "ui" / "test_login.py"
    case_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    case_file.write_text("value: ${dataAssets.generatedData.phone}\n", encoding="utf-8")
    test_file.write_text("def test_login(): pass\n", encoding="utf-8")

    cases = _case_files_for_execution("default", "ui", [])

    assert cases == ["cases/ui/test_login.yaml"]


def test_project_run_all_resolves_yaml_cases_for_data_asset_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        executor_module,
        "_project_case_dir",
        lambda project_id: f"cases/{project_id}",
    )
    case_file = tmp_path / "cases" / "project123" / "ui" / "test_login.yaml"
    test_file = tmp_path / "tests" / "project123" / "ui" / "test_login.py"
    case_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    case_file.write_text("value: ${dataAssets.generatedData.phone}\n", encoding="utf-8")
    test_file.write_text("def test_login(): pass\n", encoding="utf-8")

    cases = _case_files_for_execution("project123", "ui", [])

    assert cases == ["cases/project123/ui/test_login.yaml"]


def test_api_cases_are_not_mapped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    test_file = tmp_path / "tests" / "api" / "test_legacy.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_legacy(): pass\n", encoding="utf-8")

    assert _yaml_to_py("cases/api/test_legacy.yaml", module="api") == ""


def test_yaml_mapping_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    backend_file = tmp_path / "backend" / "app.py"
    backend_file.parent.mkdir(parents=True)
    backend_file.write_text("raise SystemExit\n", encoding="utf-8")

    assert _yaml_to_py("cases/ui/../../backend/app.yaml", module="ui") == ""


def test_api_task_creation_is_rejected():
    with pytest.raises(ValueError, match="仅支持 ui"):
        create_task("default", module="api")


def test_mark_task_running_does_not_revive_stopped_task(tmp_path, monkeypatch):
    _init_executor_db(tmp_path, monkeypatch)
    db.execute(
        "INSERT INTO tasks(id,project_id,module,case_files,status,started_at) "
        "VALUES('task-stopped','default','ui','[]','stopped','2026-08-03 00:00:00')"
    )

    assert _mark_task_running("task-stopped") is False

    row = db.execute("SELECT status FROM tasks WHERE id='task-stopped'", fetch=True)[0]
    assert row["status"] == "stopped"


def test_stop_task_kills_stored_runner_pid_when_pgid_missing(tmp_path, monkeypatch):
    _init_executor_db(tmp_path, monkeypatch)
    db.execute(
        "INSERT INTO tasks(id,project_id,module,case_files,status,runner_pid,runner_pgid,started_at) "
        "VALUES('task-running','default','ui','[]','running',43210,0,'2026-08-03 00:00:00')"
    )
    killed = []
    monkeypatch.setattr(executor_module, "_remove_queued_task", lambda _task_id: 0)
    monkeypatch.setattr(executor_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        executor_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(AssertionError("pgid should not be used")),
    )

    result = stop_task("task-running")

    assert result["ok"] is True
    assert killed == [(43210, executor_module._signal.SIGTERM)]


def test_worker_stop_monitor_terminates_local_process(monkeypatch):
    terminated = []

    class FakeProc:
        def __init__(self):
            self.done = False

        def poll(self):
            return 0 if self.done else None

    proc = FakeProc()
    monkeypatch.setattr(executor_module, "_get_task", lambda _task_id: {"status": "stopped"})

    def terminate(task_id, runner):
        terminated.append((task_id, runner))
        proc.done = True
        return True, "已停止"

    monkeypatch.setattr(executor_module, "_terminate_task_runner", terminate)

    monitor = _start_task_stop_monitor("task-monitor", proc, interval=0.01)
    try:
        for _ in range(50):
            if terminated:
                break
            executor_module.time.sleep(0.01)
    finally:
        monitor.set()

    assert terminated == [("task-monitor", proc)]


def test_run_task_terminates_process_when_stop_wins_after_spawn(tmp_path, monkeypatch):
    _init_executor_db(tmp_path, monkeypatch)
    db.execute(
        "INSERT INTO tasks(id,project_id,module,case_files,status,started_at) "
        "VALUES('task-race','default','ui','[]','pending','2026-08-03 00:00:00')"
    )
    killed_groups = []

    class FakeStdout:
        def readline(self):
            return ""

    class FakeProc:
        pid = 24680
        returncode = None
        stdout = FakeStdout()

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

        def poll(self):
            return self.returncode

    original_update = executor_module._update_task

    def update_and_stop(task_id, **fields):
        original_update(task_id, **fields)
        if "runner_pid" in fields:
            original_update(task_id, status="stopped", finished_at=executor_module._now())

    monkeypatch.setattr(executor_module, "_update_task", update_and_stop)
    monkeypatch.setattr(executor_module, "_clear_runtime_caches", lambda: None)
    monkeypatch.setattr(executor_module, "_load_test_data_asset_context", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(executor_module, "_build_pytest_args", lambda *_args, **_kwargs: ["pytest"])
    monkeypatch.setattr(executor_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProc())
    monkeypatch.setattr(executor_module.os, "getpgid", lambda _pid: 13579)
    monkeypatch.setattr(executor_module.os, "killpg", lambda pgid, sig: killed_groups.append((pgid, sig)))

    result = executor_module.run_task("task-race")

    assert result["status"] == "stopped"
    assert (13579, executor_module._signal.SIGTERM) in killed_groups


def test_load_test_data_asset_context_merges_quick_bound_alias_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text(
        "value: ${dataAssets.testData.CODE}\nother: ${dataAssets.testData.phone}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "testhub.sqlite3"
    monkeypatch.setenv("TEST_DATA_ASSETS_DB_PATH", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_data_asset_requirements (
          id INTEGER PRIMARY KEY,
          target_type TEXT,
          target_case_id INTEGER,
          alias TEXT,
          asset_type TEXT,
          quantity INTEGER,
          filters TEXT,
          tags TEXT,
          is_active INTEGER
        );
        CREATE TABLE test_data_assets (
          id INTEGER PRIMARY KEY,
          asset_type TEXT,
          status TEXT,
          payload TEXT,
          tags TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO test_data_asset_requirements
        (id,target_type,target_case_id,alias,asset_type,quantity,filters,tags,is_active)
        VALUES (1,'ui_automation',1,'testData','USER',1,'{}','["quick-bind-ui_automation-1-testData-code"]',1)
        """
    )
    conn.execute(
        """
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (1,'USER','AVAILABLE','{"code":"123456"}','["quick-bind-ui_automation-1-testData-code"]')
        """
    )
    conn.execute(
        """
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (2,'USER','AVAILABLE','{"phone":"18877570399"}','["quick-bind-ui_automation-1-testData-phone"]')
        """
    )
    conn.commit()
    conn.close()

    context = _load_test_data_asset_context("default", "ui", ["test_login.yaml"], {})

    assert context["dataAssets"]["testData"] == {
        "code": "123456",
        "phone": "18877570399",
    }
    assert context["data"]["testData"] == {
        "code": "123456",
        "phone": "18877570399",
    }
    assert context["data"]["code"] == "123456"
    assert context["data"]["phone"] == "18877570399"


def test_load_test_data_asset_context_expands_parameterized_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text(
        "value: ${dataAssets.testData.code}\nother: ${dataAssets.testData.phone}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "testhub.sqlite3"
    monkeypatch.setenv("TEST_DATA_ASSETS_DB_PATH", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_data_asset_requirements (
          id INTEGER PRIMARY KEY,
          target_type TEXT,
          target_case_id INTEGER,
          alias TEXT,
          asset_type TEXT,
          quantity INTEGER,
          filters TEXT,
          tags TEXT,
          is_active INTEGER
        );
        CREATE TABLE test_data_assets (
          id INTEGER PRIMARY KEY,
          asset_type TEXT,
          status TEXT,
          payload TEXT,
          tags TEXT
        );
        INSERT INTO test_data_asset_requirements
        (id,target_type,target_case_id,alias,asset_type,quantity,filters,tags,is_active)
        VALUES (1,'ui_automation',1,'testData','USER',1,'{}','["quick-bind-ui_automation-1-testData-code"]',1);
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (1,'USER','AVAILABLE','{"phone":"18877570399"}','["quick-bind-ui_automation-1-testData-phone"]');
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (2,'USER','AVAILABLE','[{"code":"122222"},{"code":"122"},{"code":"123456"}]','["quick-bind-ui_automation-1-testData-code"]');
        """
    )
    conn.commit()
    conn.close()

    context = _load_test_data_asset_context("default", "ui", ["test_login.yaml"], {})

    assert context["dataAssets"]["testData"] == {
        "phone": "18877570399",
        "code": "122222",
    }
    assert [
        row["dataAssets"]["testData"]["code"]
        for row in context["_parameterRows"]
    ] == ["122222", "122", "123456"]
    assert [
        row["data"]["code"]
        for row in context["_parameterRows"]
    ] == ["122222", "122", "123456"]
    assert all(
        row["dataAssets"]["testData"]["phone"] == "18877570399"
        for row in context["_parameterRows"]
    )
    assert all(
        row["data"]["phone"] == "18877570399"
        for row in context["_parameterRows"]
    )


def test_load_test_data_asset_context_honors_requested_row_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text(
        "value: ${dataAssets.generatedData.code}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "testhub.sqlite3"
    monkeypatch.setenv("TEST_DATA_ASSETS_DB_PATH", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_data_asset_requirements (
          id INTEGER PRIMARY KEY,
          target_type TEXT,
          target_case_id INTEGER,
          alias TEXT,
          asset_type TEXT,
          source_asset_id INTEGER,
          quantity INTEGER,
          filters TEXT,
          tags TEXT,
          is_active INTEGER
        );
        CREATE TABLE test_data_assets (
          id INTEGER PRIMARY KEY,
          asset_type TEXT,
          status TEXT,
          payload TEXT,
          tags TEXT
        );
        INSERT INTO test_data_asset_requirements
        (id,target_type,target_case_id,alias,asset_type,source_asset_id,quantity,filters,tags,is_active)
        VALUES (1,'ui_automation',1,'generatedData','USER',1,1,'{}','[]',1);
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (1,'USER','AVAILABLE','[{"code":"111111"},{"code":"222222"},{"code":"333333"},{"code":"444444"},{"code":"555555"}]','[]');
        """
    )
    conn.commit()
    conn.close()

    context = _load_test_data_asset_context(
        "default",
        "ui",
        ["test_login.yaml"],
        {},
        parameter_row_limit=3,
    )

    assert [
        row["dataAssets"]["generatedData"]["code"]
        for row in context["_parameterRows"]
    ] == ["111111", "222222", "333333"]


def test_load_test_data_asset_context_reads_bulk_csv_asset_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    media_root = tmp_path / "media"
    output_file = media_root / "test-data-bulk" / "rows.csv"
    output_file.parent.mkdir(parents=True)
    output_file.write_text(
        "user_id,phone\nU0001,13800138000\nU0002,13800138001\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TESTHUB_MEDIA_ROOT", str(media_root))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text(
        "value: ${dataAssets.generatedData.phone}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "testhub.sqlite3"
    monkeypatch.setenv("TEST_DATA_ASSETS_DB_PATH", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_data_asset_requirements (
          id INTEGER PRIMARY KEY,
          target_type TEXT,
          target_case_id INTEGER,
          alias TEXT,
          asset_type TEXT,
          quantity INTEGER,
          filters TEXT,
          tags TEXT,
          is_active INTEGER
        );
        CREATE TABLE test_data_assets (
          id INTEGER PRIMARY KEY,
          asset_type TEXT,
          status TEXT,
          payload TEXT,
          tags TEXT
        );
        CREATE TABLE bulk_test_data_jobs (
          id INTEGER PRIMARY KEY,
          status TEXT,
          output_format TEXT,
          output_file TEXT
        );
        INSERT INTO bulk_test_data_jobs (id,status,output_format,output_file)
        VALUES (7,'COMPLETED','csv','test-data-bulk/rows.csv');
        INSERT INTO test_data_asset_requirements
        (id,target_type,target_case_id,alias,asset_type,quantity,filters,tags,is_active)
        VALUES (1,'ui_automation',1,'generatedData','USER',1,'{}','["bulk-job-7"]',1);
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (1,'USER','AVAILABLE','{"_runnergo_source":"bulk_test_data","bulk_job_id":7}', '["bulk-generated","bulk-job-7"]');
        """
    )
    conn.commit()
    conn.close()

    context = _load_test_data_asset_context("default", "ui", ["test_login.yaml"], {})

    assert context["dataAssets"]["generatedData"] == {
        "phone": "13800138000",
    }
    assert [
        row["dataAssets"]["generatedData"]["phone"]
        for row in context["_parameterRows"]
    ] == ["13800138000", "13800138001"]


def test_load_test_data_asset_context_ignores_unreferenced_parameter_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    case_file = tmp_path / "cases" / "ui" / "test_login.yaml"
    case_file.parent.mkdir(parents=True)
    case_file.write_text(
        "value: ${dataAssets.testData.OCR}\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "testhub.sqlite3"
    monkeypatch.setenv("TEST_DATA_ASSETS_DB_PATH", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE test_data_asset_requirements (
          id INTEGER PRIMARY KEY,
          target_type TEXT,
          target_case_id INTEGER,
          alias TEXT,
          asset_type TEXT,
          quantity INTEGER,
          filters TEXT,
          tags TEXT,
          is_active INTEGER
        );
        CREATE TABLE test_data_assets (
          id INTEGER PRIMARY KEY,
          asset_type TEXT,
          status TEXT,
          payload TEXT,
          tags TEXT
        );
        INSERT INTO test_data_asset_requirements
        (id,target_type,target_case_id,alias,asset_type,quantity,filters,tags,is_active)
        VALUES (1,'ui_automation',1,'testData','USER',1,'{}','["quick-bind-ui_automation-1-testData-assert"]',1);
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (1,'USER','AVAILABLE','[{"phone1":"11122222222","code":"122222"},{"phone1":"1312222","code":"122333"}]','["quick-bind-ui_automation-1-testData-phone"]');
        INSERT INTO test_data_assets (id,asset_type,status,payload,tags)
        VALUES (2,'USER','AVAILABLE','{"OCR":"服务器异常，请联系管理员"}','["quick-bind-ui_automation-1-testData-assert"]');
        """
    )
    conn.commit()
    conn.close()

    context = _load_test_data_asset_context("default", "ui", ["test_login.yaml"], {})

    assert context["dataAssets"]["testData"] == {"OCR": "服务器异常，请联系管理员"}
    assert "_parameterRows" not in context


def test_api_module_is_rejected_by_platform_endpoints():
    with pytest.raises(HTTPException, match="接口测试模块已移除"):
        app.run(app.RunIn(project_id="default", module="api"))
    with pytest.raises(HTTPException, match="接口测试模块已移除"):
        app.cases_save("default", "api", "legacy.yaml", app.CaseIn(content="[]"))
    with pytest.raises(HTTPException, match="接口测试模块已移除"):
        app.jobs_create(app.JobIn(
            project_id="default",
            name="legacy",
            module="api",
            cron="0 0 8 * * ?",
        ))
