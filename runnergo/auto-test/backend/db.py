"""SQLite 存储（项目 / 任务 / 定时任务），WAL 模式保证并发安全。"""
import sqlite3
import time
from . import settings


def get_conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    settings.ensure_dirs()
    conn = get_conn()
    legacy_job_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    jobs_had_repeat_count = "repeat_count" in legacy_job_columns
    jobs_had_trigger_type = "trigger_type" in legacy_job_columns
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            base_url TEXT,
            case_dir TEXT DEFAULT 'cases',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            module TEXT DEFAULT 'ui', -- UI 自动化模块
            case_files TEXT,          -- JSON list（空=全部）
            env TEXT DEFAULT 'test',  -- 环境：test/staging/prod
            status TEXT,              -- pending/running/success/failed
            passed INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            duration REAL DEFAULT 0,
            triggered_by TEXT,
            started_at TEXT,
            finished_at TEXT,
            report_url TEXT,
            runtime_override TEXT DEFAULT '[]',
            runtime_case_file TEXT DEFAULT '',
            runtime_data TEXT DEFAULT '[]',
            runtime_variables TEXT DEFAULT '{}',
            scheduled_job_id TEXT DEFAULT '',
            suite_id TEXT DEFAULT '',
            runner_pid INTEGER DEFAULT 0,
            runner_pgid INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT,
            description TEXT DEFAULT '',
            module TEXT,
            case_files TEXT,
            task_type TEXT DEFAULT 'TEST_CASE',
            suite_id TEXT DEFAULT '',
            cron TEXT,
            trigger_type TEXT DEFAULT 'CRON',
            interval_seconds INTEGER DEFAULT 3600,
            execute_at TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            status TEXT DEFAULT 'ACTIVE',
            repeat_count INTEGER DEFAULT 0,
            last_run TEXT,
            total_runs INTEGER DEFAULT 0,
            successful_runs INTEGER DEFAULT 0,
            failed_runs INTEGER DEFAULT 0,
            last_result TEXT DEFAULT '{}',
            notify_on_success INTEGER DEFAULT 0,
            notify_on_failure INTEGER DEFAULT 0,
            notification_type TEXT DEFAULT '',
            notify_emails TEXT DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS test_suites (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            case_files TEXT NOT NULL DEFAULT '[]',
            status TEXT DEFAULT 'idle',
            last_run TEXT,
            last_task_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notification_logs (
            id TEXT PRIMARY KEY,
            task_id TEXT DEFAULT '',
            job_id TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            notification_type TEXT DEFAULT 'feishu',
            status TEXT DEFAULT 'pending',
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            response_info TEXT DEFAULT '{}',
            error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS defects (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            project_id TEXT,
            tc_id TEXT,
            scenario TEXT,
            title TEXT,
            severity TEXT DEFAULT '中',
            error_type TEXT,
            error_message TEXT,
            page_url TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'open',
            source TEXT DEFAULT 'auto',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS web_elements (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            page_url TEXT,
            tag_name TEXT,
            element_role TEXT,
            signature TEXT NOT NULL,
            locators TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            element_context TEXT NOT NULL DEFAULT '{}',
            usage_count INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(project_id, signature)
        );

        CREATE TABLE IF NOT EXISTS web_recording_drafts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            start_url TEXT NOT NULL,
            viewport TEXT NOT NULL,
            steps TEXT NOT NULL,
            storage_state TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'recording',
            last_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_agent_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            target_type TEXT NOT NULL DEFAULT 'web',
            env TEXT NOT NULL DEFAULT 'test',
            base_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0,
            auto_execute INTEGER NOT NULL DEFAULT 1,
            plan TEXT NOT NULL DEFAULT '{}',
            context TEXT NOT NULL DEFAULT '{}',
            execution_task_id TEXT,
            result_summary TEXT NOT NULL DEFAULT '{}',
            failure_analysis TEXT NOT NULL DEFAULT '{}',
            defect_draft TEXT NOT NULL DEFAULT '{}',
            error_message TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_task_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ui_data_templates (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            case_file TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            runtime_override TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_web_elements_project
            ON web_elements(project_id, updated_at);

        CREATE INDEX IF NOT EXISTS idx_web_recording_drafts_project
            ON web_recording_drafts(project_id, updated_at);

        CREATE INDEX IF NOT EXISTS idx_ai_agent_tasks_project
            ON ai_agent_tasks(project_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_ai_agent_tasks_status
            ON ai_agent_tasks(status, updated_at);

        CREATE INDEX IF NOT EXISTS idx_ai_agent_events_task
            ON ai_agent_events(agent_task_id, id);

        CREATE INDEX IF NOT EXISTS idx_ui_data_templates_project_case
            ON ui_data_templates(project_id, case_file, updated_at);

        CREATE INDEX IF NOT EXISTS idx_test_suites_project
            ON test_suites(project_id, updated_at);

        CREATE INDEX IF NOT EXISTS idx_notification_logs_created
            ON notification_logs(created_at);
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL DEFAULT '',
            user_requirement TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'CREATED',
            auto_execute INTEGER NOT NULL DEFAULT 1,
            plan TEXT NOT NULL DEFAULT '{}',
            dimension_plan TEXT NOT NULL DEFAULT '{}',
            progress INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_task_id TEXT NOT NULL,
            step_no INTEGER NOT NULL,
            phase TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            input_payload TEXT NOT NULL DEFAULT '{}',
            output_payload TEXT NOT NULL DEFAULT '{}',
            error_message TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT,
            duration REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_task_id TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'task',
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(agent_task_id, scope, key)
        );

        CREATE TABLE IF NOT EXISTS test_assets (
            id TEXT PRIMARY KEY,
            asset_type TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'agent',
            count INTEGER NOT NULL DEFAULT 0,
            fields TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'success',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        
        CREATE TABLE IF NOT EXISTS agent_execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_task_id TEXT NOT NULL DEFAULT '',
            step_no INTEGER NOT NULL DEFAULT 0,
            trace_type TEXT NOT NULL DEFAULT 'thought',
            action TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            params TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'success',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_execution_log_task
            ON agent_execution_log(agent_task_id, step_no, id);

CREATE TABLE IF NOT EXISTS test_execution_result (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_task_id TEXT NOT NULL DEFAULT '',
            step_id INTEGER NOT NULL DEFAULT 0,
            tool TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'success',
            summary TEXT NOT NULL DEFAULT '',
            logs TEXT NOT NULL DEFAULT '',
            screenshots TEXT NOT NULL DEFAULT '[]',
            api_response TEXT NOT NULL DEFAULT '',
            failure_analysis TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_agent_tasks_status
            ON agent_tasks(status, updated_at);

        CREATE INDEX IF NOT EXISTS idx_agent_tasks_project
            ON agent_tasks(project_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_agent_steps_task
            ON agent_steps(agent_task_id, step_no);

        CREATE INDEX IF NOT EXISTS idx_agent_memory_task
            ON agent_memory(agent_task_id, key);

        CREATE INDEX IF NOT EXISTS idx_test_assets_type
            ON test_assets(asset_type, created_at);

        CREATE INDEX IF NOT EXISTS idx_test_execution_result_task
            ON test_execution_result(agent_task_id, id);
        """
    )
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN env TEXT DEFAULT 'test'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN pages_dir TEXT DEFAULT 'pages'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN engine TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN static_report_url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN envs TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN base_url TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN skipped INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN runtime_override TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN runtime_case_file TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN runtime_data TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN runtime_variables TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    for ddl in [
        "ALTER TABLE tasks ADD COLUMN scheduled_job_id TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN suite_id TEXT DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN runner_pid INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN runner_pgid INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN task_type TEXT DEFAULT 'TEST_CASE'",
        "ALTER TABLE jobs ADD COLUMN suite_id TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN trigger_type TEXT DEFAULT 'CRON'",
        "ALTER TABLE jobs ADD COLUMN interval_seconds INTEGER DEFAULT 3600",
        "ALTER TABLE jobs ADD COLUMN execute_at TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'ACTIVE'",
        "ALTER TABLE jobs ADD COLUMN repeat_count INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN total_runs INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN successful_runs INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN failed_runs INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN last_result TEXT DEFAULT '{}'",
        "ALTER TABLE jobs ADD COLUMN notify_on_success INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN notify_on_failure INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN notification_type TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN notify_emails TEXT DEFAULT '[]'",
        "ALTER TABLE jobs ADD COLUMN created_at TEXT",
        "ALTER TABLE jobs ADD COLUMN updated_at TEXT",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE web_recording_drafts ADD COLUMN storage_state TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE web_elements ADD COLUMN element_context TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_data_templates (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            case_file TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            runtime_override TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ui_data_templates_project_case "
        "ON ui_data_templates(project_id, case_file, updated_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS load_test_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            case_file TEXT NOT NULL,
            scenario_name TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'protocol',
            status TEXT NOT NULL DEFAULT 'pending',
            config_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_load_test_runs_project_created "
        "ON load_test_runs(project_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_suites (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            case_files TEXT NOT NULL DEFAULT '[]',
            status TEXT DEFAULT 'idle',
            last_run TEXT,
            last_task_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_logs (
            id TEXT PRIMARY KEY,
            task_id TEXT DEFAULT '',
            job_id TEXT DEFAULT '',
            project_id TEXT DEFAULT '',
            notification_type TEXT DEFAULT 'feishu',
            status TEXT DEFAULT 'pending',
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            response_info TEXT DEFAULT '{}',
            error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_test_suites_project ON test_suites(project_id, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_logs_created ON notification_logs(created_at)")
    # UI-only 迁移：历史“全部”任务等价为 UI，纯接口定时任务停用且不再调度。
    conn.execute("UPDATE tasks SET module='ui' WHERE module IS NULL OR module='' OR module='all'")
    conn.execute("UPDATE jobs SET module='ui' WHERE module IS NULL OR module='' OR module='all'")
    conn.execute("UPDATE jobs SET enabled=0 WHERE module='api'")
    conn.execute("UPDATE jobs SET status='ACTIVE' WHERE enabled=1 AND (status IS NULL OR status='')")
    conn.execute("UPDATE jobs SET status='PAUSED' WHERE enabled=0 AND (status IS NULL OR status='')")
    conn.execute("UPDATE jobs SET task_type='TEST_CASE' WHERE task_type IS NULL OR task_type=''")
    conn.execute("UPDATE jobs SET repeat_count=0 WHERE repeat_count IS NULL OR repeat_count<0")
    if legacy_job_columns and jobs_had_repeat_count and not jobs_had_trigger_type:
        conn.execute(
            "UPDATE jobs SET trigger_type='COUNT', cron='' "
            "WHERE COALESCE(repeat_count,0)>0"
        )
    conn.execute("UPDATE jobs SET trigger_type='CRON' WHERE trigger_type IS NULL OR trigger_type=''")
    conn.execute("UPDATE jobs SET interval_seconds=3600 WHERE interval_seconds IS NULL OR interval_seconds<60")
    conn.execute("UPDATE jobs SET execute_at='' WHERE execute_at IS NULL")
    conn.execute(
        "UPDATE jobs SET enabled=0,status='COMPLETED' "
        "WHERE COALESCE(repeat_count,0)>0 AND COALESCE(total_runs,0)>=COALESCE(repeat_count,0)"
    )
    conn.execute("UPDATE jobs SET notification_type='' WHERE notification_type IS NULL")
    conn.execute("UPDATE jobs SET notify_emails='[]' WHERE notify_emails IS NULL OR notify_emails=''")
    for row in conn.execute("SELECT id, report_url FROM tasks WHERE report_url IS NOT NULL AND report_url != ''").fetchall():
        normalized_url = settings.normalize_report_url(row["report_url"])
        if normalized_url != row["report_url"]:
            conn.execute("UPDATE tasks SET report_url=? WHERE id=?", (normalized_url, row["id"]))
    conn.commit()
    conn.close()


# ===== 通用执行 =====
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def execute(sql: str, params=(), fetch=False):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row else {}


def to_dicts(rows) -> list:
    return [dict(r) for r in rows] if rows else []
