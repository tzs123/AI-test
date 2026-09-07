"""测试可视化平台后端：FastAPI 完整 REST API。

能力：
- 项目 / 用例（YAML）CRUD
- 单/批量执行（任务隔离 + 流式日志 + Allure 报告 + 截图）
- 执行记录 / 详情 / 日志 / 截图查看
- 定时任务管理（cron）
- 负载均衡（Redis 队列 + 多 worker）
- 飞书报告通知
"""
import os
import json
import hmac
import time
import shutil
import asyncio
import threading
import uuid
import yaml
from datetime import datetime
from urllib.parse import unquote as _url_unquote
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from typing import Optional

from backend import db, settings, log_store, step_store, executor, redis_queue
from backend import runnergo_test_objects
from backend.auth import service as auth_service
from backend.safe_paths import safe_child_path, safe_identifier
from backend.url_security import validate_outbound_url
from backend.time_utils import localize_task_timestamps
from backend.projects import service as project_service
from backend.cases import service as case_service
from backend import scheduler as scheduler_module
from backend.web_recorder import (
    router as web_recorder_router,
    recording_manager,
    mark_stale_recording_drafts_interrupted,
)
from backend import load_test
from backend import legacy_performance
from core.runtime_case import RuntimeCase, normalize_overrides, public_input_fields

app = FastAPI(title="自动化测试可视化平台", version="1.0")
app.include_router(web_recorder_router)
app.include_router(load_test.router)
app.include_router(legacy_performance.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "token", "X-Agent-Token"],
)


def unified_login_response(request: Request):
    hostname = request.url.hostname or "localhost"
    login_url = f"{request.url.scheme}://{hostname}:9998/#/login"
    return HTMLResponse(
        "<!doctype html><meta charset=\"utf-8\">"
        f"<script>window.top.location.replace({json.dumps(login_url)})</script>",
        headers={"Cache-Control": "no-store"},
    )


_CONTENT_TYPE_BY_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}


def _content_type_for(path: str) -> str:
    import os as _os
    ext = _os.path.splitext(path)[1].lower()
    return _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")


async def _serve_public_report(request: Request):
    """Serve a task's Allure report using an HMAC-signed, timestamped token.

    URL shape:
        /public-report/{task_id}/{URL-encoded token}/report/[file_path]
    When the token is valid and matches the task_id, we serve the file from
    settings.REPORT_DIR/tasks/{task_id}/report/ — identical to the private
    StaticFiles mount but without requiring a RunnerGo login session.
    """
    from fastapi.responses import Response as _Response

    raw_path = request.url.path.removeprefix("/public-report/").lstrip("/")
    # raw_path := "{task_id}/{quoted_token}/report/[file...]"
    parts = raw_path.split("/", 2)
    if len(parts) < 2:
        return JSONResponse(
            {"detail": "报告链接无效"}, status_code=404, headers={"Cache-Control": "no-store"}
        )
    task_id = parts[0]
    quoted_token = parts[1]
    remainder = parts[2] if len(parts) == 3 else ""
    try:
        token = _url_unquote(quoted_token)
        signed_task_id = settings.unsign_report_token(token)
    except Exception:
        return JSONResponse(
            {"detail": "报告链接无效或已过期"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    if signed_task_id != task_id:
        return JSONResponse(
            {"detail": "报告链接无效"}, status_code=404, headers={"Cache-Control": "no-store"}
        )

    # remainder must start with "report/" or be empty (redirect to report/)
    if remainder == "" or remainder == "report":
        # Redirect to the canonical index path so relative assets resolve
        new_query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(
            url=f"{request.url.path.rstrip('/')}/report/{new_query}",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    if not remainder.startswith("report"):
        return JSONResponse(
            {"detail": "报告链接无效"}, status_code=404, headers={"Cache-Control": "no-store"}
        )
    rel_file = remainder.removeprefix("report").lstrip("/") or "index.html"

    try:
        report_task_root = os.path.join(settings.REPORT_DIR, "tasks", task_id, "report")
        full_path = safe_child_path(report_task_root, rel_file)
    except ValueError:
        return JSONResponse(
            {"detail": "报告链接无效"}, status_code=404, headers={"Cache-Control": "no-store"}
        )
    if not os.path.isfile(full_path):
        return JSONResponse(
            {"detail": "报告文件不存在"}, status_code=404, headers={"Cache-Control": "no-store"}
        )

    # Small files: read and return via Response for speed; larger files stream via FileResponse
    try:
        response: _Response
        size = os.path.getsize(full_path)
        if size <= 2 * 1024 * 1024:
            with open(full_path, "rb") as fh:
                data = fh.read()
            response = _Response(
                content=data,
                media_type=_content_type_for(full_path),
                headers={
                    "X-Robots-Tag": "noindex, nofollow",
                    "Cache-Control": "private, max-age=3600",
                },
            )
        else:
            response = FileResponse(
                path=full_path,
                media_type=_content_type_for(full_path),
                filename=os.path.basename(full_path),
                headers={
                    "X-Robots-Tag": "noindex, nofollow",
                    "Cache-Control": "private, max-age=3600",
                },
            )
        return response
    except Exception:
        return JSONResponse(
            {"detail": "无法读取报告文件"}, status_code=500, headers={"Cache-Control": "no-store"}
        )


@app.middleware("http")
async def runnergo_authentication(request: Request, call_next):
    """Protect every API, UI, report, and screenshot route with RunnerGo auth."""
    if request.url.path == "/api/health" or request.method.upper() == "OPTIONS":
        return await call_next(request)
    # Publicly-shared (signed-token) Allure report URLs bypass login so that
    # recipients of Feishu / WeCom / DingTalk cards can open reports directly.
    if request.url.path.startswith("/public-report/"):
        return await _serve_public_report(request)
    if request.url.path.startswith("/api/agent/internal/"):
        expected = os.environ.get(
            "TEST_DATA_CENTER_AGENT_TOKEN",
            "runnergo-local-agent-token",
        )
        supplied = request.headers.get("X-Agent-Token", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                {"detail": "内部 Agent 令牌无效"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return await call_next(request)
    if not settings.AUTH_REQUIRED:
        return await call_next(request)

    credential = auth_service.extract_credential(request)
    if not credential:
        if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
            return unified_login_response(request)
        return JSONResponse(
            {"detail": "未检测到 RunnerGo 登录态"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    if credential.source == "cookie" and not auth_service.cookie_request_has_valid_origin(request):
        return JSONResponse(
            {"detail": "请求来源校验失败"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    try:
        user = await run_in_threadpool(auth_service.validate_credential, credential.token)
    except auth_service.AuthenticationUnavailable:
        return JSONResponse(
            {"detail": "RunnerGo 权限服务暂时不可用"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    if user is None:
        if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
            return unified_login_response(request)
        return JSONResponse(
            {"detail": "RunnerGo 登录态无效或已过期"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    request.state.runnergo_user = user
    return await call_next(request)

# ===== 静态资源挂载 =====
os.makedirs(settings.REPORT_DIR, exist_ok=True)
os.makedirs(settings.SCREENSHOTS_DIR, exist_ok=True)
app.mount(
    "/assets",
    StaticFiles(directory=os.path.join(settings.ROOT, "templates", "assets")),
    name="ui-assets",
)
app.mount("/report", StaticFiles(directory=settings.REPORT_DIR, html=True), name="report")
app.mount("/screenshots", StaticFiles(directory=settings.SCREENSHOTS_DIR), name="screenshots")


# ===== 启动初始化 =====
@app.on_event("startup")
def _startup():
    db.init_db()
    legacy_performance.init_storage()
    load_test.recover_incomplete_runs()
    mark_stale_recording_drafts_interrupted()
    project_service.seed_default_project()
    try:
        from backend import agent
        agent.recover_incomplete_tasks()
        agent.recover_workflow_tasks()
    except Exception as e:
        print(f"[agent] 恢复未完成任务失败（不影响主服务）: {e}")
    try:
        scheduler_module.start_scheduler()
    except Exception as e:
        print(f"[scheduler] 启动失败（不影响主服务）: {e}")

    # === 自动启动 worker ===
    if settings.WORKER_AUTO_START:
        try:
            # 先检查当前是否已有存活的 worker，避免重复启动
            _existing = _count_workers()
            if _existing > 0:
                print(f"[worker] 检测到已有 {_existing} 个 worker 运行中，跳过自动启动")
            elif not redis_queue.available():
                print("[worker] Redis 不可用，跳过 worker 自动启动（worker 启动需要 Redis 队列）")
            else:
                _count = settings.WORKER_COUNT
                print(f"[worker] Docker 容器启动，自动拉起 {_count} 个 worker 进程...")
                _start_worker_process(count=_count)
        except Exception as e:
            print(f"[worker] 自动启动失败（不影响主服务）: {e}")


@app.on_event("shutdown")
def _shutdown():
    load_test.shutdown()
    recording_manager.shutdown()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "auto-test"}


@app.get("/api/runnergo-teams")
def runnergo_team_list(request: Request):
    credential = auth_service.extract_credential(request)
    try:
        teams = runnergo_test_objects.list_teams(
            credential.token if credential else "",
        )
    except runnergo_test_objects.TestObjectError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"teams": teams}


@app.get("/api/runnergo-test-objects")
def runnergo_test_object_list(request: Request, team_id: str):
    credential = auth_service.extract_credential(request)
    try:
        objects = runnergo_test_objects.list_test_objects(
            credential.token if credential else "",
            team_id,
        )
    except runnergo_test_objects.TestObjectError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"objects": objects, "team_id": str(team_id)}


# ===== 首页 =====
@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(settings.ROOT, "templates", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace(
        "__AUTO_TEST_ALLOW_PYTHON_EDIT__",
        "true" if settings.ALLOW_PYTHON_EDIT else "false",
    )


# ===== Pydantic 模型 =====
class ProjectIn(BaseModel):
    name: str
    description: str = ""
    base_url: str = ""
    case_dir: str = None
    envs: dict = None            # 多环境 Base URL，如 {"test": "http://...", "staging": "http://..."}


class CaseIn(BaseModel):
    content: str


class CaseBatchDeleteIn(BaseModel):
    filenames: list[str] = Field(default_factory=list)


class RunIn(BaseModel):
    project_id: str
    module: str = "ui"
    case_files: list = []        # 空=全部 UI 用例
    triggered_by: str = "manual"
    env: str = "test"            # 环境：test/staging/prod
    base_url: str = ""           # 执行时使用的 Base URL（覆盖项目默认配置）
    runtime_variables: dict = Field(default_factory=dict)
    runtimeOverride: list[dict] = Field(default_factory=list)
    runtimeCaseFile: str = ""


class RuntimeScanIn(BaseModel):
    project_id: str
    module: str = "ui"
    case_file: str


class UiDataTemplateIn(BaseModel):
    project_id: str
    case_file: str
    name: str
    description: str = ""
    runtimeOverride: list[dict] = Field(default_factory=list)


class UiDataTemplateUpdateIn(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    runtimeOverride: Optional[list[dict]] = None


class TestSuiteIn(BaseModel):
    project_id: str
    name: str
    description: str = ""
    case_files: list[str] = Field(default_factory=list)


class JobIn(BaseModel):
    project_id: str
    name: str
    description: str = ""
    module: str = "ui"
    case_files: list = Field(default_factory=list)
    task_type: str = "TEST_CASE"  # TEST_CASE / TEST_SUITE
    suite_id: str = ""
    cron: str = ""               # 5 段或 6 段 cron（含秒）
    trigger_type: str = "CRON"   # CRON / INTERVAL / ONCE / COUNT
    interval_seconds: int = 3600
    execute_at: str = ""
    enabled: bool = True
    status: str = "ACTIVE"
    repeat_count: int = 0


class BatchDeleteIn(BaseModel):
    task_ids: list[str]


class NotificationBatchDeleteIn(BaseModel):
    ids: list[str]


class ReportNotificationSendIn(BaseModel):
    report_ids: list[str] = Field(default_factory=list, min_length=1, max_length=50)
    notification_ids: list[str] = Field(default_factory=list, min_length=1, max_length=50)


class AgentTaskIn(BaseModel):
    project_id: str = "default"
    goal: str
    env: str = "test"
    base_url: str = ""
    auto_execute: bool = True
    case_files: list[str] = Field(default_factory=list)
    created_by: str = ""


class InternalUiOrchestrationIn(BaseModel):
    project_id: str = "default"
    case_files: list[str] = Field(default_factory=list)
    base_url: str = ""
    env: str = "test"
    test_data_count: int = Field(default=3, ge=1, le=20)
    parent_task_id: str = ""
    data_bindings: list[dict] = Field(default_factory=list)
    repair_strategy: dict = Field(default_factory=dict)


class TestDataGenerateIn(BaseModel):
    type: str = "USER"
    count: int = 100
    fields: list[str] = Field(default_factory=list)
    project_id: str = ""
    api_url: str = ""
    code_length: int = 6


class AgentWorkflowIn(BaseModel):
    task_name: str = ""
    user_requirement: str
    project_id: str = "default"
    auto_execute: bool = True
    created_by: str = ""


class FailureAnalyzeIn(BaseModel):
    logs: str = ""
    screenshots: list[str] = Field(default_factory=list)
    api_response: str = ""
    context: str = ""


class AgentPlanIn(BaseModel):
    requirement: str


class DataAgentGenerateIn(BaseModel):
    swagger_url: str = ""
    swagger_doc: Optional[dict] = None
    api: str = ""
    mode: list[str] = Field(default_factory=lambda: ["normal", "boundary", "security"])
    project_id: str = ""
    count: int = 1000


class BrowserAgentRunIn(BaseModel):
    url: str = ""
    goal: str = ""
    case_file: str = ""
    project_id: str = ""
    headless: bool = True


class SecurityAgentScanIn(BaseModel):
    api_doc: str = ""
    target: str = ""
    parameters: list[dict] = Field(default_factory=list)
    headers: dict = Field(default_factory=dict)
    project_id: str = ""


class FailureAnalyzeBatchIn(BaseModel):
    failures: list[dict] = Field(default_factory=list)


class FailureAutoFixIn(BaseModel):
    analysis: dict = Field(default_factory=dict)


class ReactRunIn(BaseModel):
    force: bool = False


def _normalize_task_report(task: dict) -> dict:
    task = localize_task_timestamps(task)
    for field in ("runtime_override", "runtime_data", "runtime_variables"):
        value = task.get(field) if task else None
        if isinstance(value, str):
            try:
                task[field] = json.loads(value or "[]")
            except (TypeError, ValueError):
                task[field] = []
    if task and task.get("report_url"):
        task["report_url"] = settings.normalize_report_url(task["report_url"])
    return task


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _validate_project_urls(base_url: str, envs: dict = None) -> tuple:
    try:
        normalized_base = validate_outbound_url(base_url) if base_url else ""
        normalized_envs = (
            {
                str(name): validate_outbound_url(url) if url else ""
                for name, url in envs.items()
            }
            if envs is not None
            else None
        )
        return normalized_base, normalized_envs
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_project_update_urls(pid: str, base_url: str, envs: dict = None) -> tuple:
    """Validate changed project URLs while preserving previously saved values.

    Older projects may contain internal test addresses that are blocked by the
    current SSRF policy. Editing an unrelated project field should not be
    prevented by those unchanged legacy values.
    """
    existing = project_service.get_project(pid) or {}
    existing_envs = existing.get("envs") if isinstance(existing.get("envs"), dict) else {}

    def validate_if_changed(value: str, previous: str) -> str:
        value = str(value or "").strip()
        previous = str(previous or "").strip()
        if not value:
            return ""
        if value == previous or value.rstrip("/") == previous.rstrip("/"):
            return value
        return validate_outbound_url(value)

    try:
        normalized_base = validate_if_changed(base_url, existing.get("base_url") or "")
        normalized_envs = (
            {
                str(name): validate_if_changed(url, existing_envs.get(str(name), ""))
                for name, url in envs.items()
            }
            if envs is not None
            else None
        )
        return normalized_base, normalized_envs
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_runtime_template_payload(project_id: str, case_file: str, runtime_override: list) -> tuple[str, list]:
    if not project_service.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    stored_case_file = str(case_file or "").strip()
    if not stored_case_file:
        raise HTTPException(400, "请选择 UI 用例")
    filename = os.path.basename(stored_case_file)
    try:
        content = case_service.get_case(project_id, "ui", filename)
        source_case = yaml.safe_load(content) or {}
        normalized_override = normalize_overrides(runtime_override)
        RuntimeCase(source_case, normalized_override).build()
    except FileNotFoundError as exc:
        raise HTTPException(404, "用例不存在") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return stored_case_file, normalized_override


def _ui_data_template_to_dict(row) -> dict:
    item = db.to_dict(row)
    try:
        item["runtimeOverride"] = json.loads(item.pop("runtime_override", "[]") or "[]")
    except (TypeError, ValueError):
        item["runtimeOverride"] = []
    return item


def _suite_to_dict(row) -> dict:
    item = db.to_dict(row)
    try:
        item["case_files"] = json.loads(item.get("case_files") or "[]")
    except (TypeError, ValueError):
        item["case_files"] = []
    item["case_count"] = len(item["case_files"])
    return item


def _job_to_dict(row) -> dict:
    item = db.to_dict(row)
    try:
        item["case_files"] = json.loads(item.get("case_files") or "[]")
    except (TypeError, ValueError):
        item["case_files"] = []
    try:
        item["last_result"] = json.loads(item.get("last_result") or "{}")
    except (TypeError, ValueError):
        item["last_result"] = {}
    item["enabled"] = bool(item.get("enabled"))
    item["repeat_count"] = max(0, int(item.get("repeat_count") or 0))
    item["trigger_type"] = (item.get("trigger_type") or "CRON").upper()
    item["interval_seconds"] = max(60, int(item.get("interval_seconds") or 3600))
    item["execute_at"] = item.get("execute_at") or ""
    item["status"] = item.get("status") or ("ACTIVE" if item["enabled"] else "PAUSED")
    if item["repeat_count"] > 0 and max(0, int(item.get("total_runs") or 0)) >= item["repeat_count"]:
        item["enabled"] = False
        item["status"] = "COMPLETED"
    return item


def _parse_job_execute_at(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        raise HTTPException(400, "执行时间不能为空")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise HTTPException(400, "执行时间格式无效") from exc


def _validate_job_trigger(body: JobIn) -> tuple[str, str, int, str]:
    trigger_type = str(body.trigger_type or "").upper()
    if not trigger_type:
        trigger_type = "CRON"
    if trigger_type == "CRON" and int(body.repeat_count or 0) > 0:
        trigger_type = "COUNT"
    if trigger_type not in {"CRON", "INTERVAL", "ONCE", "COUNT"}:
        raise HTTPException(400, "触发器类型无效")
    cron = str(body.cron or "").strip()
    interval_seconds = int(body.interval_seconds or 3600)
    execute_at = str(body.execute_at or "").strip()
    if trigger_type == "CRON":
        if not cron:
            raise HTTPException(400, "Cron 表达式不能为空")
        try:
            scheduler_module._cron_trigger(cron)
        except Exception as exc:
            raise HTTPException(400, f"Cron 表达式无效: {exc}") from exc
        return trigger_type, cron, max(60, interval_seconds), ""
    if trigger_type == "INTERVAL":
        if interval_seconds < 60:
            raise HTTPException(400, "间隔秒数不能小于60秒")
        return trigger_type, cron, interval_seconds, ""
    if trigger_type == "COUNT":
        if int(body.repeat_count or 0) <= 0:
            raise HTTPException(400, "执行次数必须大于0")
        return trigger_type, "", max(60, interval_seconds), ""
    execute_dt = _parse_job_execute_at(execute_at)
    now = datetime.now(execute_dt.tzinfo)
    if execute_dt <= now:
        raise HTTPException(400, "执行时间必须大于当前时间")
    return trigger_type, cron, max(60, interval_seconds), execute_dt.strftime("%Y-%m-%d %H:%M:%S")


def _validate_case_files(project_id: str, case_files: list) -> list[str]:
    if not project_service.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    available = {
        item["relative"]
        for item in case_service.list_cases(project_id, "ui")
    }
    normalized = []
    for value in case_files or []:
        case_file = str(value or "").strip().replace("\\", "/")
        if not case_file:
            continue
        if case_file not in available:
            basename_match = next((item for item in available if os.path.basename(item) == os.path.basename(case_file)), "")
            if basename_match:
                case_file = basename_match
        if case_file not in available:
            raise HTTPException(400, f"用例不存在: {value}")
        if case_file not in normalized:
            normalized.append(case_file)
    return normalized


def _job_case_files(job: dict) -> list[str]:
    if job.get("task_type") == "TEST_SUITE":
        rows = db.execute(
            "SELECT case_files FROM test_suites WHERE id=? AND project_id=?",
            (job.get("suite_id") or "", job.get("project_id") or ""),
            fetch=True,
        )
        if not rows:
            raise HTTPException(400, "测试套件不存在")
        return json.loads(db.to_dict(rows[0]).get("case_files") or "[]")
    return list(job.get("case_files") or [])


def _dispatch_job_once(job: dict, triggered_by: str) -> list[str]:
    case_files = _job_case_files(job)
    if not case_files:
        raise HTTPException(400, "请选择测试用例或测试套件")
    repeat_count = max(0, int(job.get("repeat_count") or 0))
    total_runs = max(0, int(job.get("total_runs") or 0))
    if repeat_count > 0 and total_runs >= repeat_count:
        db.execute("UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?", (_now(), job["id"]))
        scheduler_module.reload_jobs()
        raise HTTPException(400, "定时任务执行次数已完成")
    task_id = executor.create_task(
        project_id=job["project_id"],
        module="ui",
        case_files=case_files,
        triggered_by=triggered_by,
        scheduled_job_id=job.get("id") or "",
        suite_id=job.get("suite_id") or "",
    )
    threading.Thread(target=executor.dispatch, args=(task_id,), daemon=True).start()
    now = _now()
    db.execute(
        "UPDATE jobs SET last_run=?,total_runs=COALESCE(total_runs,0)+?,"
        "last_result=?,updated_at=? WHERE id=?",
        (now, 1, json.dumps({"task_ids": [task_id]}, ensure_ascii=False), now, job["id"]),
    )
    trigger_type = str(job.get("trigger_type") or "CRON").upper()
    if trigger_type == "ONCE" or (repeat_count > 0 and total_runs + 1 >= repeat_count):
        db.execute("UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?", (_now(), job["id"]))
        scheduler_module.reload_jobs()
    elif trigger_type == "COUNT":
        scheduler_module.reload_jobs()
    return [task_id]


# ===== 项目 =====
@app.get("/api/projects")
def projects_list():
    return project_service.list_projects()


@app.post("/api/projects")
def projects_create(body: ProjectIn):
    base_url, envs = _validate_project_urls(body.base_url, body.envs)
    try:
        project = project_service.create_project(
            body.name, body.description, base_url, body.case_dir, envs=envs)
        return project
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{pid}")
def projects_get(pid: str):
    p = project_service.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@app.put("/api/projects/{pid}")
def projects_update(pid: str, body: ProjectIn):
    base_url, envs = _validate_project_update_urls(pid, body.base_url, body.envs)
    try:
        project = project_service.update_project(
            pid, name=body.name, description=body.description,
            base_url=base_url, case_dir=body.case_dir, envs=envs)
        return project
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/projects/{pid}")
def projects_delete(pid: str):
    if pid == "default":
        raise HTTPException(400, "默认项目不可删除")
    project_service.delete_project(pid)
    return {"ok": True}


# ===== 用例（YAML）=====
@app.get("/api/projects/{pid}/cases")
def cases_list(pid: str, module: str = "ui"):
    try:
        return case_service.list_cases(pid, module)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/projects/{pid}/cases/{module}/{filename}")
def cases_get(pid: str, module: str, filename: str):
    if module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 ui")
    try:
        content = case_service.get_case(pid, module, filename)
        return {"filename": filename, "module": module, "content": content}
    except FileNotFoundError:
        raise HTTPException(404, "用例不存在")
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/projects/{pid}/cases/{module}/batch-delete")
def cases_batch_delete(pid: str, module: str, body: CaseBatchDeleteIn):
    if module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 ui")
    filenames = list(dict.fromkeys(str(item or "").strip() for item in body.filenames))
    filenames = [item for item in filenames if item]
    if not filenames:
        raise HTTPException(400, "至少选择一个用例")
    try:
        deleted = case_service.delete_cases(pid, module, filenames)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"删除文件失败: {exc}") from exc
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


@app.post("/api/projects/{pid}/cases/{module}/{filename}")
def cases_save(pid: str, module: str, filename: str, body: CaseIn):
    if module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 ui")
    try:
        path = case_service.save_case(pid, module, filename, body.content)
        return {"ok": True, "path": os.path.relpath(path, settings.ROOT)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.put("/api/projects/{pid}/cases/{module}/{filename}")
def cases_rename(
    pid: str, module: str, filename: str, new_filename: str, body: CaseIn
):
    if module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 ui")
    try:
        path = case_service.rename_case(
            pid, module, filename, new_filename, body.content
        )
        return {
            "ok": True,
            "filename": os.path.basename(path),
            "path": os.path.relpath(path, settings.ROOT),
        }
    except FileNotFoundError as e:
        raise HTTPException(404, "用例不存在") from e
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/projects/{pid}/cases/{module}/{filename}")
def cases_delete(pid: str, module: str, filename: str):
    if module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 ui")
    if case_service.delete_case(pid, module, filename):
        return {"ok": True}
    raise HTTPException(404, "用例不存在")


# ===== 执行 =====
@app.post("/api/runtime-case/scan")
def runtime_case_scan(body: RuntimeScanIn):
    """扫描 UI Flow 中支持执行前覆盖的输入步骤。"""
    if body.module != "ui":
        raise HTTPException(400, "仅支持 UI 自动化用例")
    filename = os.path.basename(str(body.case_file or ""))
    try:
        content = case_service.get_case(body.project_id, body.module, filename)
        case_data = yaml.safe_load(content) or {}
    except FileNotFoundError as exc:
        raise HTTPException(404, "用例不存在") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "caseFile": body.case_file,
        "fields": public_input_fields(case_data),
    }


@app.get("/api/ui-data-templates")
def ui_data_templates_list(project_id: str, case_file: str = ""):
    if not project_service.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    params = [project_id]
    where = "project_id=?"
    selected_case_file = str(case_file or "").strip()
    if selected_case_file:
        where += " AND (case_file=? OR case_file=?)"
        params.extend([selected_case_file, os.path.basename(selected_case_file)])
    rows = db.execute(
        f"SELECT * FROM ui_data_templates WHERE {where} ORDER BY updated_at DESC",
        tuple(params),
        fetch=True,
    )
    return [_ui_data_template_to_dict(row) for row in rows]


@app.post("/api/ui-data-templates")
def ui_data_templates_create(body: UiDataTemplateIn):
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(400, "模板名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "模板名称最多 100 个字符")
    case_file, runtime_override = _validate_runtime_template_payload(
        body.project_id,
        body.case_file,
        body.runtimeOverride,
    )
    template_id = uuid.uuid4().hex[:12]
    now = _now()
    db.execute(
        "INSERT INTO ui_data_templates("
        "id,project_id,case_file,name,description,runtime_override,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            template_id,
            body.project_id,
            case_file,
            name,
            str(body.description or "").strip(),
            json.dumps(runtime_override, ensure_ascii=False),
            now,
            now,
        ),
    )
    rows = db.execute("SELECT * FROM ui_data_templates WHERE id=?", (template_id,), fetch=True)
    return _ui_data_template_to_dict(rows[0])


@app.put("/api/ui-data-templates/{template_id}")
def ui_data_templates_update(template_id: str, body: UiDataTemplateUpdateIn):
    rows = db.execute("SELECT * FROM ui_data_templates WHERE id=?", (template_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "模板不存在")
    current = db.to_dict(rows[0])
    name = str(body.name if body.name is not None else current.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "模板名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "模板名称最多 100 个字符")
    if body.runtimeOverride is None:
        try:
            runtime_override = json.loads(current.get("runtime_override") or "[]")
        except (TypeError, ValueError):
            runtime_override = []
    else:
        _, runtime_override = _validate_runtime_template_payload(
            current["project_id"],
            current["case_file"],
            body.runtimeOverride,
        )
    db.execute(
        "UPDATE ui_data_templates SET name=?,description=?,runtime_override=?,updated_at=? WHERE id=?",
        (
            name,
            str(body.description if body.description is not None else current.get("description") or "").strip(),
            json.dumps(runtime_override, ensure_ascii=False),
            _now(),
            template_id,
        ),
    )
    rows = db.execute("SELECT * FROM ui_data_templates WHERE id=?", (template_id,), fetch=True)
    return _ui_data_template_to_dict(rows[0])


@app.delete("/api/ui-data-templates/{template_id}")
def ui_data_templates_delete(template_id: str):
    rows = db.execute("SELECT id FROM ui_data_templates WHERE id=?", (template_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "模板不存在")
    db.execute("DELETE FROM ui_data_templates WHERE id=?", (template_id,))
    return {"ok": True}


# ===== 测试套件 =====
@app.get("/api/test-suites")
def test_suites_list(project_id: str = ""):
    if project_id:
        rows = db.execute(
            "SELECT * FROM test_suites WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
            fetch=True,
        )
    else:
        rows = db.execute("SELECT * FROM test_suites ORDER BY updated_at DESC", fetch=True)
    return [_suite_to_dict(row) for row in rows]


@app.post("/api/test-suites")
def test_suites_create(body: TestSuiteIn):
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(400, "套件名称不能为空")
    case_files = _validate_case_files(body.project_id, body.case_files)
    if not case_files:
        raise HTTPException(400, "测试套件至少需要选择一个用例")
    suite_id = uuid.uuid4().hex[:8]
    now = _now()
    db.execute(
        "INSERT INTO test_suites(id,project_id,name,description,case_files,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            suite_id,
            body.project_id,
            name,
            str(body.description or "").strip(),
            json.dumps(case_files, ensure_ascii=False),
            "idle",
            now,
            now,
        ),
    )
    rows = db.execute("SELECT * FROM test_suites WHERE id=?", (suite_id,), fetch=True)
    return _suite_to_dict(rows[0])


@app.put("/api/test-suites/{suite_id}")
def test_suites_update(suite_id: str, body: TestSuiteIn):
    rows = db.execute("SELECT * FROM test_suites WHERE id=?", (suite_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "测试套件不存在")
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(400, "套件名称不能为空")
    case_files = _validate_case_files(body.project_id, body.case_files)
    if not case_files:
        raise HTTPException(400, "测试套件至少需要选择一个用例")
    db.execute(
        "UPDATE test_suites SET project_id=?,name=?,description=?,case_files=?,updated_at=? WHERE id=?",
        (
            body.project_id,
            name,
            str(body.description or "").strip(),
            json.dumps(case_files, ensure_ascii=False),
            _now(),
            suite_id,
        ),
    )
    rows = db.execute("SELECT * FROM test_suites WHERE id=?", (suite_id,), fetch=True)
    return _suite_to_dict(rows[0])


@app.delete("/api/test-suites/{suite_id}")
def test_suites_delete(suite_id: str):
    rows = db.execute("SELECT id FROM test_suites WHERE id=?", (suite_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "测试套件不存在")
    db.execute("DELETE FROM test_suites WHERE id=?", (suite_id,))
    db.execute("UPDATE jobs SET suite_id='',enabled=0,status='PAUSED' WHERE suite_id=?", (suite_id,))
    scheduler_module.reload_jobs()
    return {"ok": True}


@app.post("/api/test-suites/{suite_id}/run")
def test_suites_run(suite_id: str):
    rows = db.execute("SELECT * FROM test_suites WHERE id=?", (suite_id,), fetch=True)
    if not rows:
        raise HTTPException(404, "测试套件不存在")
    suite = _suite_to_dict(rows[0])
    if not suite["case_files"]:
        raise HTTPException(400, "测试套件没有用例")
    task_id = executor.create_task(
        project_id=suite["project_id"],
        module="ui",
        case_files=suite["case_files"],
        triggered_by=f"suite:{suite_id}",
        suite_id=suite_id,
    )
    db.execute(
        "UPDATE test_suites SET status='running',last_run=?,last_task_id=?,updated_at=? WHERE id=?",
        (_now(), task_id, _now(), suite_id),
    )
    threading.Thread(target=executor.dispatch, args=(task_id,), daemon=True).start()
    return {"ok": True, "task_id": task_id, "case_count": len(suite["case_files"])}


@app.post("/api/run")
def run(body: RunIn, request: Request = None):
    if body.module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 UI 自动化")
    proj = project_service.get_project(body.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    # 如果未指定 base_url，使用项目 envs 中对应环境的 URL，再退回 proj.base_url
    base_url = body.base_url
    if not base_url:
        envs = proj.get("envs") or {}
        base_url = envs.get(body.env) or proj.get("base_url") or ""
    if base_url:
        try:
            base_url = validate_outbound_url(base_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    try:
        runtime_override = normalize_overrides(body.runtimeOverride)
        runtime_case_file = str(body.runtimeCaseFile or "").strip()
        if runtime_case_file:
            if len(body.case_files) != 1:
                raise ValueError("前置数据配置仅支持单个 UI 用例执行")
            if os.path.basename(runtime_case_file) != os.path.basename(body.case_files[0]):
                raise ValueError("runtimeCaseFile 与当前选择的用例不一致")
            content = case_service.get_case(
                body.project_id, body.module, os.path.basename(runtime_case_file)
            )
            RuntimeCase(yaml.safe_load(content) or {}, runtime_override).build()
        elif runtime_override:
            raise ValueError("提供 runtimeOverride 时必须指定 runtimeCaseFile")
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc

    selected_case_files = list(body.case_files or [])
    if not selected_case_files:
        selected_case_files = [item["relative"] for item in case_service.list_cases(body.project_id, "ui")]
    try:
        case_payloads = []
        for case_file in selected_case_files:
            content = case_service.get_case(body.project_id, body.module, os.path.basename(case_file))
            case_payloads.append(yaml.safe_load(content) or {})
        references = []
        for payload in case_payloads:
            references.extend(runnergo_test_objects.collect_references(payload))
        credential = auth_service.extract_credential(request) if request is not None else None
        test_object_bundle = runnergo_test_objects.resolve_references(
            case_payloads,
            credential.token if credential else "",
        ) if references else {}
    except (runnergo_test_objects.TestObjectError, yaml.YAMLError, OSError, ValueError) as exc:
        raise HTTPException(400, f"测试对象引用校验失败: {exc}") from exc

    task_id = executor.create_task(
        body.project_id, body.module, body.case_files, body.triggered_by,
        body.env, base_url=base_url,
        runtime_override=runtime_override,
        runtime_case_file=runtime_case_file,
        runtime_variables=body.runtime_variables,
    )
    try:
        executor.store_test_object_bundle(task_id, test_object_bundle)
    except Exception as exc:
        executor.discard_test_object_bundle(task_id)
        db.execute(
            "UPDATE tasks SET status='failed',finished_at=? WHERE id=?",
            (_now(), task_id),
        )
        raise HTTPException(500, f"测试对象运行时配置创建失败: {exc}") from exc
    # 负载均衡：入队由 worker 消费；Redis 不可用时本地同步执行
    threading.Thread(target=executor.dispatch, args=(task_id,), daemon=True).start()
    return {"task_id": task_id, "status": "pending",
            "queue": redis_queue.available(), "base_url": base_url}


# ===== AI Test Agent =====
@app.post("/api/agent/internal/orchestrate-ui")
def internal_orchestrate_ui(body: InternalUiOrchestrationIn):
    from backend.agent.tools import prepare_ui_data_binding, run_ui_test

    project_id = str(body.project_id or "default").strip() or "default"
    case_files = [str(item).strip() for item in body.case_files if str(item).strip()]
    if not case_files:
        raise HTTPException(400, "至少选择一个 UI YAML 用例")

    for case_file in case_files:
        path = executor._case_file_path(project_id, "ui", case_file)
        if not path or not os.path.isfile(path):
            raise HTTPException(400, f"UI YAML 用例不存在: {case_file}")

    parent_task_id = str(body.parent_task_id or uuid.uuid4().hex[:12]).strip()
    try:
        binding_result = prepare_ui_data_binding(
            project_id=project_id,
            case_files=case_files,
            agent_task_id=parent_task_id,
            count=body.test_data_count,
            existing_bindings=body.data_bindings,
        )
        execution = run_ui_test(
            project_id=project_id,
            case_files=case_files,
            env=body.env,
            base_url=body.base_url,
            triggered_by=f"testhub-agent:{parent_task_id}",
            data_bindings=binding_result.get("bindings") or [],
            test_data_count=body.test_data_count,
            runtime_variables={"_runnergo_repair_strategy": body.repair_strategy}
            if body.repair_strategy else {},
        )
    except (RuntimeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc

    bindings = binding_result.get("bindings") or []
    return {
        **execution,
        "status": "dispatched",
        "parent_task_id": parent_task_id,
        "case_files": case_files,
        "bindings": bindings,
        "test_data_count": body.test_data_count,
        "assets": [item.get("asset") or {} for item in bindings],
    }


@app.get("/api/agent/internal/tasks/{task_id}")
def internal_ui_task_detail(task_id: str):
    task_id = safe_identifier(task_id, label="task_id")
    task = executor._get_task(task_id)
    if not task:
        raise HTTPException(404, "UI 执行任务不存在")
    normalized = _normalize_task_report(task)
    case_files = normalized.get("case_files")
    if isinstance(case_files, str):
        try:
            normalized["case_files"] = json.loads(case_files or "[]")
        except (TypeError, ValueError):
            normalized["case_files"] = []
    if str(normalized.get("status") or "").lower() in {"success", "failed", "stopped"}:
        step_details = step_store.read(task_id)
        failed_steps = [
            step for step in (step_details.get("steps") or [])
            if str(step.get("status") or "").lower() == "failed"
        ]
        normalized["execution_details"] = step_details
        normalized["failure_steps"] = failed_steps
        normalized["log_tail"] = log_store.read(task_id)[-12000:]
    return normalized


@app.get("/api/agent/tasks")
def agent_tasks_list(project_id: str = "", limit: int = 100):
    from backend import agent
    return agent.list_workflow_tasks(project_id=project_id, limit=limit)


@app.post("/api/agent/tasks")
def agent_tasks_create(body: AgentTaskIn):
    from backend import agent
    try:
        task = agent.create_agent_workflow_task(
            task_name="",
            user_requirement=body.goal,
            project_id=body.project_id,
            auto_execute=False,
            created_by=body.created_by,
        )
        if body.auto_execute:
            from backend.agent.core.executor import start_react_run
            start_react_run(task["id"])
            task = agent.get_workflow_task(task["id"]) or task
        return task
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/agent/tasks/{task_id}")
def agent_task_detail(task_id: str):
    from backend import agent
    task = agent.get_workflow_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    return task


@app.get("/api/agent/tasks/{task_id}/events")
def agent_task_events(task_id: str):
    from backend import agent
    if not agent.get_workflow_task(task_id):
        raise HTTPException(404, "Agent 任务不存在")
    return agent.get_workflow_timeline(task_id)


@app.get("/api/agent/tasks/{task_id}/timeline")
def agent_task_timeline(task_id: str):
    return agent_workflow_task_timeline(task_id)


@app.post("/api/agent/tasks/{task_id}/execute")
def agent_task_execute(task_id: str):
    from backend.agent.core.executor import start_react_run
    try:
        return start_react_run(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/agent/tasks/{task_id}/save-flow")
def agent_task_save_flow(task_id: str):
    raise HTTPException(410, "旧 Flow 草稿确认接口已替换为 AI Agent 工作流")


@app.post("/api/agent/tasks/{task_id}/retry")
def agent_task_retry(task_id: str):
    from backend.agent.core.executor import clear_execution_log, start_react_run
    try:
        clear_execution_log(task_id)
        return start_react_run(task_id, force=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/agent/tasks/{task_id}/stop")
def agent_task_stop(task_id: str):
    from backend.agent.core.executor import stop_react_run
    try:
        return stop_react_run(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.delete("/api/agent/tasks/{task_id}")
def agent_task_delete(task_id: str):
    from backend import agent
    try:
        agent.delete_workflow_task(task_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ===== 测试数据中心 =====
@app.get("/api/test-data/assets")
def test_data_assets_list(project_id: str = "", limit: int = 100):
    from backend.agent.tools import list_test_assets
    return list_test_assets(project_id=project_id, limit=limit)


@app.post("/api/test-data/generate")
def test_data_generate(body: TestDataGenerateIn):
    from backend.agent.tools import generate_test_data
    try:
        return generate_test_data(
            asset_type=body.type,
            count=body.count,
            fields=body.fields,
            project_id=body.project_id,
            api_url=body.api_url,
            code_length=body.code_length,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/test-data/assets/{asset_id}")
def test_data_asset_detail(asset_id: str):
    from backend.agent.tools import get_test_asset
    asset = get_test_asset(asset_id)
    if not asset:
        raise HTTPException(404, "测试数据资产不存在")
    return asset


# ===== AI Agent 工作流（v2）=====
@app.get("/api/agent/tools")
def agent_tools_list():
    from backend import agent
    return {"tools": agent.list_tools()}


@app.post("/api/agent/v2/analyze")
def agent_failure_analyze(body: FailureAnalyzeIn):
    from backend import agent
    return agent.analyze_failure(
        logs=body.logs,
        screenshots=body.screenshots,
        api_response=body.api_response,
        context=body.context,
    )


@app.post("/api/agent/v2/plan")
def agent_workflow_plan(body: AgentPlanIn):
    from backend.agent.core.planner import build_react_plan
    try:
        return build_react_plan(body.requirement)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/agent/v2/tasks")
def agent_workflow_tasks_list(project_id: str = "", limit: int = 100):
    from backend import agent
    return agent.list_workflow_tasks(project_id=project_id, limit=limit)


@app.post("/api/agent/v2/tasks")
def agent_workflow_tasks_create(body: AgentWorkflowIn):
    from backend import agent
    try:
        task = agent.create_agent_workflow_task(
            task_name=body.task_name,
            user_requirement=body.user_requirement,
            project_id=body.project_id,
            auto_execute=False,
            created_by=body.created_by,
        )
        if body.auto_execute:
            from backend.agent.core.executor import start_react_run
            start_react_run(task["id"])
            task = agent.get_workflow_task(task["id"]) or task
        return task
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/agent/v2/tasks/{task_id}")
def agent_workflow_task_detail(task_id: str):
    from backend import agent
    task = agent.get_workflow_task(task_id)
    if not task:
        raise HTTPException(404, "工作流任务不存在")
    return task


@app.get("/api/agent/v2/tasks/{task_id}/timeline")
def agent_workflow_task_timeline(task_id: str):
    from backend import agent
    task = agent.get_workflow_task(task_id)
    if not task:
        raise HTTPException(404, "工作流任务不存在")
    from backend.agent.core.executor import get_execution_log, get_react_timeline
    trace = get_execution_log(task_id)
    task["steps"] = get_react_timeline(task_id)
    task["execution_logs"] = trace
    task["execution_results"] = task.get("results", [])
    task["memories"] = task.get("memory", [])
    task["context"] = {
        "source": "react_agent_planner",
        "summary": task.get("user_requirement") or "",
    }
    task["test_plan"] = task.get("dimension_plan") or {}
    return {
        "task_id": task_id,
        "status": task["status"],
        "task": task,
        "steps": task["steps"],
        "brain": trace,
    }


@app.get("/api/agent/v2/tasks/{task_id}/memory")
def agent_workflow_task_memory(task_id: str):
    from backend import agent
    if not agent.get_workflow_task(task_id):
        raise HTTPException(404, "工作流任务不存在")
    return agent.list_memory(task_id)


@app.post("/api/agent/v2/tasks/{task_id}/start")
def agent_workflow_task_start(task_id: str):
    from backend.agent.core.executor import start_react_run
    try:
        return start_react_run(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/agent/v2/tasks/{task_id}/stop")
def agent_workflow_task_stop(task_id: str):
    from backend.agent.core.executor import stop_react_run
    try:
        return stop_react_run(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/agent/v2/tasks/{task_id}")
def agent_workflow_task_delete(task_id: str):
    from backend import agent
    try:
        agent.delete_workflow_task(task_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/agent/v2/reports/{filename}")
def agent_workflow_report(filename: str):
    from backend.safe_paths import safe_child_path
    report_dir = os.path.join(settings.RUNTIME_DIR, "agent-reports")
    path = safe_child_path(report_dir, filename, allowed_suffixes=(".json", ".md"), filename_only=True)
    if not os.path.exists(path):
        raise HTTPException(404, "报告不存在")
    return FileResponse(path, media_type="application/json" if filename.endswith(".json") else "text/markdown")


# ===== 数据智能体（Data Agent）=====
@app.post("/api/data-agent/generate-from-api")
def data_agent_generate_from_api(body: DataAgentGenerateIn):
    from backend.data_agent import generate_from_api
    try:
        return generate_from_api(
            swagger_url=body.swagger_url,
            swagger_doc=body.swagger_doc,
            api=body.api,
            mode=body.mode,
            project_id=body.project_id,
            count=body.count,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ===== 浏览器视觉智能体（Web Vision Agent）=====
@app.post("/api/browser-agent/run")
def browser_agent_run(body: BrowserAgentRunIn):
    from backend.browser_agent import run_browser_agent
    return run_browser_agent(
        url=body.url,
        goal=body.goal,
        case_file=body.case_file,
        project_id=body.project_id,
        headless=body.headless,
    )


# ===== 安全测试智能体（Security Agent）=====
@app.post("/api/security-agent/scan")
def security_agent_scan(body: SecurityAgentScanIn):
    from backend.security_agent import scan
    try:
        return scan(
            api_doc=body.api_doc,
            target=body.target,
            parameters=body.parameters,
            headers=body.headers,
            project_id=body.project_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ===== 失败分析系统（Failure Analyzer）=====
@app.post("/api/analyzer/solve")
def analyzer_solve(body: FailureAnalyzeIn):
    from backend.analyzer import solve
    return solve(
        logs=body.logs,
        screenshots=body.screenshots,
        api_response=body.api_response,
        context=body.context,
    )


@app.post("/api/analyzer/batch")
def analyzer_batch(body: FailureAnalyzeBatchIn):
    from backend.analyzer import analyze_batch
    return analyze_batch(body.failures)


@app.post("/api/analyzer/auto-fix")
def analyzer_auto_fix(body: FailureAutoFixIn):
    from backend.analyzer import auto_fix
    return auto_fix(body.analysis)


# ===== AI Agent 核心（ReAct 自主决策）=====
@app.get("/api/agent/react/tools")
def agent_react_tools():
    from backend.agent.core.tool_registry import list_tools
    return {"tools": list_tools()}


@app.post("/api/agent/v2/tasks/{task_id}/react")
def agent_workflow_task_react(task_id: str, body: Optional[ReactRunIn] = None):
    from backend.agent.core.executor import start_react_run
    try:
        return start_react_run(task_id, force=bool(body and body.force))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/agent/v2/tasks/{task_id}/react/stop")
def agent_workflow_task_react_stop(task_id: str):
    from backend.agent.core.executor import stop_react_run
    try:
        return stop_react_run(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _workflow_trace_fallback(task_id: str) -> list[dict]:
    """旧工作流没有 ReAct 日志时，基于步骤/结果合成大脑轨迹，避免前端空白。"""
    from backend import agent
    from backend.agent.executor import get_execution_results

    trace = []
    step_no = 0
    for step in agent.get_workflow_timeline(task_id):
        step_no += 1
        status = "failed" if step.get("status") == "failed" else "success"
        tool = step.get("tool") or ""
        trace.append({
            "id": f"fallback-{step_no}-thought",
            "step_no": step_no,
            "trace_type": "thought",
            "action": f"准备执行：{step.get('action') or step.get('phase') or 'Agent 步骤'}",
            "tool": tool,
            "params": step.get("input_payload") or {},
            "result": {},
            "status": "success",
            "created_at": step.get("created_at") or step.get("started_at") or "",
        })
        step_no += 1
        output = step.get("output_payload") or {}
        trace.append({
            "id": f"fallback-{step_no}-observation",
            "step_no": step_no,
            "trace_type": "observation",
            "action": step.get("error_message") or output.get("note") or output.get("message") or str(output.get("summary") or step.get("status") or ""),
            "tool": tool,
            "params": {},
            "result": output,
            "status": status,
            "created_at": step.get("finished_at") or step.get("updated_at") or "",
        })
    for result in get_execution_results(task_id):
        if result.get("status") != "failed":
            continue
        analysis = result.get("failure_analysis") or {}
        if not analysis:
            continue
        step_no += 1
        trace.append({
            "id": f"fallback-{step_no}-reflection",
            "step_no": step_no,
            "trace_type": "reflection",
            "action": f"{analysis.get('reason', '')}；建议：{analysis.get('suggestion', '')}",
            "tool": result.get("tool") or "",
            "params": {},
            "result": analysis,
            "status": "failed",
            "created_at": result.get("created_at") or "",
        })
    return trace


@app.get("/api/agent/v2/tasks/{task_id}/react/trace")
def agent_workflow_task_react_trace(task_id: str):
    from backend import agent
    from backend.agent.core.executor import get_execution_log
    if not agent.get_workflow_task(task_id):
        raise HTTPException(404, "工作流任务不存在")
    trace = get_execution_log(task_id)
    if not trace:
        trace = _workflow_trace_fallback(task_id)
    return {"task_id": task_id, "trace": trace}


# ===== 项目级记忆（Agent Memory）=====
@app.get("/api/agent/v2/memory/project")
def agent_project_memory(project_id: str = "default"):
    from backend.agent.core.memory import list_project_memory
    return list_project_memory(project_id or "default")



@app.get("/api/tasks")
def tasks_list(project_id: str = None, limit: int = 100):
    if project_id:
        rows = db.execute(
            "SELECT t.*, p.name as project_name FROM tasks t "
            "LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE t.project_id=? AND COALESCE(t.module,'ui')<>'api' "
            "ORDER BY t.started_at DESC LIMIT ?",
            (project_id, limit), fetch=True)
    else:
        rows = db.execute(
            "SELECT t.*, p.name as project_name FROM tasks t "
            "LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE COALESCE(t.module,'ui')<>'api' "
            "ORDER BY t.started_at DESC LIMIT ?", (limit,), fetch=True)
    return [_normalize_task_report(t) for t in db.to_dicts(rows)]


@app.delete("/api/tasks/batch")
def tasks_batch_delete(body: BatchDeleteIn):
    deleted = 0
    errors = []
    for tid in body.task_ids:
        try:
            tid = safe_identifier(tid, label="task_id")
            # a. 删除数据库记录
            db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            # b. 删除报告目录
            report_dir = safe_child_path(
                os.path.join(settings.REPORT_DIR, "tasks"), tid, filename_only=True
            )
            if os.path.isdir(report_dir):
                shutil.rmtree(report_dir)
            # c. 删除截图目录
            screenshot_dir = safe_child_path(
                settings.SCREENSHOTS_DIR, tid, filename_only=True
            )
            if os.path.isdir(screenshot_dir):
                shutil.rmtree(screenshot_dir)
            # d. 删除日志文件
            log_store.delete(tid)
            # e. 删除逐步骤执行状态
            step_store.delete(tid)
            deleted += 1
        except Exception as e:
            errors.append({"task_id": tid, "error": str(e)})
    return {"deleted": deleted, "errors": errors}


@app.get("/api/tasks/{tid}")
def task_detail(tid: str):
    rows = db.execute(
        "SELECT t.*, p.name as project_name FROM tasks t "
        "LEFT JOIN projects p ON t.project_id=p.id "
        "WHERE t.id=? AND COALESCE(t.module,'ui')<>'api'", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")
    task = _normalize_task_report(db.to_dict(rows[0]))
    try:
        state = step_store.read(tid)
        stats = state.get("assertion_stats") if isinstance(state, dict) else None
        if isinstance(stats, dict) and stats.get("total"):
            task["assertion_stats"] = stats
    except Exception:
        pass
    return task


@app.post("/api/tasks/{tid}/stop")
def task_stop(tid: str):
    """停止正在运行的任务。"""
    rows = db.execute("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")
    status = db.to_dict(rows[0])["status"]
    if status not in ("running", "pending", "paused"):
        raise HTTPException(400, f"任务状态为'{status}'，无法停止")
    result = executor.stop_task(tid)
    if not result["ok"]:
        raise HTTPException(500, result.get("msg") or "停止任务失败")
    return result


@app.post("/api/tasks/{tid}/pause")
def task_pause(tid: str):
    rows = db.execute("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")
    result = executor.pause_task(tid)
    if not result["ok"]:
        raise HTTPException(400, result.get("msg") or "暂停任务失败")
    return result


@app.post("/api/tasks/{tid}/resume")
def task_resume(tid: str):
    rows = db.execute("SELECT status,runner_pgid FROM tasks WHERE id=?", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")
    result = executor.resume_task(tid)
    if not result["ok"]:
        raise HTTPException(400, result.get("msg") or "恢复任务失败")
    refreshed = db.execute("SELECT status,runner_pgid FROM tasks WHERE id=?", (tid,), fetch=True)
    task = db.to_dict(refreshed[0]) if refreshed else {}
    if task.get("status") == "pending" and not int(task.get("runner_pgid") or 0):
        threading.Thread(target=executor.dispatch, args=(tid,), daemon=True).start()
    return result


@app.get("/api/tasks/{tid}/log")
def task_log(tid: str):
    return {"task_id": tid, "log": log_store.read(tid)}


@app.get("/api/tasks/{tid}/steps")
def task_steps(tid: str):
    rows = db.execute("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")
    task_status = db.to_dict(rows[0])["status"]
    state = step_store.read(tid)
    if not state:
        return {
            "task_id": tid,
            "status": task_status,
            "task_status": task_status,
            "total": 0,
            "completed": 0,
            "current_index": None,
            "counts": {},
            "steps": [],
        }
    state["task_status"] = task_status
    return state


@app.get("/api/tasks/{tid}/log/stream")
async def task_log_stream(tid: str):
    """SSE 端点：实时推送日志增量内容。"""
    # 验证任务存在
    rows = db.execute("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)
    if not rows:
        raise HTTPException(404, "任务不存在")

    async def event_generator():
        offset = 0
        # 先发送当前已有日志
        current_log = log_store.read(tid)
        if current_log:
            offset = len(current_log)
            yield f"data: {json.dumps({'log': current_log}, ensure_ascii=False)}\n\n"

        idle_count = 0
        max_idle = 600  # 500ms * 600 = 300s 超时

        while True:
            await asyncio.sleep(0.5)
            # 检查任务状态，终止态则再推一次后断开
            rows = db.execute("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)
            status = db.to_dict(rows[0])["status"] if rows else "unknown"
            finished = status in ("success", "failed", "stopped")

            current_log = log_store.read(tid)
            new_content = current_log[offset:]
            if new_content:
                offset = len(current_log)
                idle_count = 0
                yield f"data: {json.dumps({'log': new_content}, ensure_ascii=False)}\n\n"

            if finished:
                yield f"data: {json.dumps({'status': status, 'finished': True}, ensure_ascii=False)}\n\n"
                break

            idle_count += 1
            if idle_count >= max_idle:
                yield f"data: {json.dumps({'timeout': True}, ensure_ascii=False)}\n\n"
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/tasks/{tid}/screenshots")
def task_screenshots(tid: str):
    try:
        tid = safe_identifier(tid, label="task_id")
        d = safe_child_path(settings.SCREENSHOTS_DIR, tid, filename_only=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not os.path.isdir(d):
        return {"task_id": tid, "screenshots": []}
    files = sorted(
        f for f in os.listdir(d)
        if f.endswith(".png")
        and os.path.isfile(os.path.join(d, f))
        and not os.path.islink(os.path.join(d, f))
    )
    return {"task_id": tid, "screenshots": [f"/screenshots/{tid}/{f}" for f in files]}


# ===== 定时任务 =====
@app.get("/api/jobs")
def jobs_list():
    rows = db.execute(
        "SELECT j.*, p.name as project_name, s.name as suite_name FROM jobs j "
        "LEFT JOIN projects p ON j.project_id=p.id "
        "LEFT JOIN test_suites s ON j.suite_id=s.id "
        "WHERE COALESCE(j.module,'ui')<>'api' ORDER BY j.id",
        fetch=True,
    )
    return [_job_to_dict(row) for row in rows]


@app.post("/api/jobs")
def jobs_create(body: JobIn):
    if body.module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 UI 自动化")
    task_type = (body.task_type or "TEST_CASE").upper()
    if task_type not in {"TEST_CASE", "TEST_SUITE"}:
        raise HTTPException(400, "任务类型无效")
    case_files = _validate_case_files(body.project_id, body.case_files) if task_type == "TEST_CASE" else []
    suite_id = str(body.suite_id or "").strip() if task_type == "TEST_SUITE" else ""
    if task_type == "TEST_CASE" and not case_files:
        raise HTTPException(400, "请选择测试用例")
    if task_type == "TEST_SUITE":
        rows = db.execute("SELECT id FROM test_suites WHERE id=? AND project_id=?", (suite_id, body.project_id), fetch=True)
        if not rows:
            raise HTTPException(400, "请选择测试套件")
    trigger_type, cron, interval_seconds, execute_at = _validate_job_trigger(body)
    jid = uuid.uuid4().hex[:8]
    now = _now()
    status_value = "ACTIVE" if body.enabled and body.status != "PAUSED" else "PAUSED"
    db.execute(
        "INSERT INTO jobs("
        "id,project_id,name,description,module,case_files,task_type,suite_id,cron,"
        "trigger_type,interval_seconds,execute_at,"
        "enabled,status,repeat_count,last_run,total_runs,successful_runs,failed_runs,"
        "last_result,notify_on_success,notify_on_failure,notification_type,notify_emails,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,0,0,?,?,?,?,?,?,?)",
        (
            jid, body.project_id, body.name, body.description, "ui",
            json.dumps(case_files, ensure_ascii=False), task_type, suite_id,
            cron, trigger_type, interval_seconds, execute_at,
            int(status_value == "ACTIVE"), status_value,
            max(0, int(body.repeat_count or 0)) if trigger_type == "COUNT" else 0, "{}",
            0, 0, "", "[]", now, now,
        ),
    )
    scheduler_module.reload_jobs()
    return {"id": jid, "ok": True}


@app.put("/api/jobs/{jid}")
def jobs_update(jid: str, body: JobIn):
    if body.module != "ui":
        raise HTTPException(400, "接口测试模块已移除，仅支持 UI 自动化")
    rows = db.execute("SELECT id FROM jobs WHERE id=?", (jid,), fetch=True)
    if not rows:
        raise HTTPException(404, "定时任务不存在")
    task_type = (body.task_type or "TEST_CASE").upper()
    if task_type not in {"TEST_CASE", "TEST_SUITE"}:
        raise HTTPException(400, "任务类型无效")
    case_files = _validate_case_files(body.project_id, body.case_files) if task_type == "TEST_CASE" else []
    suite_id = str(body.suite_id or "").strip() if task_type == "TEST_SUITE" else ""
    if task_type == "TEST_CASE" and not case_files:
        raise HTTPException(400, "请选择测试用例")
    if task_type == "TEST_SUITE":
        suite_rows = db.execute("SELECT id FROM test_suites WHERE id=? AND project_id=?", (suite_id, body.project_id), fetch=True)
        if not suite_rows:
            raise HTTPException(400, "请选择测试套件")
    trigger_type, cron, interval_seconds, execute_at = _validate_job_trigger(body)
    status_value = "ACTIVE" if body.enabled and body.status != "PAUSED" else "PAUSED"
    db.execute(
        "UPDATE jobs SET project_id=?,name=?,description=?,module=?,case_files=?,"
        "task_type=?,suite_id=?,cron=?,trigger_type=?,interval_seconds=?,execute_at=?,"
        "enabled=?,status=?,repeat_count=?,notify_on_success=?,notify_on_failure=?,"
        "notification_type=?,notify_emails=?,updated_at=? WHERE id=?",
        (
            body.project_id, body.name, body.description, "ui",
            json.dumps(case_files, ensure_ascii=False), task_type, suite_id,
            cron, trigger_type, interval_seconds, execute_at,
            int(status_value == "ACTIVE"), status_value,
            max(0, int(body.repeat_count or 0)) if trigger_type == "COUNT" else 0,
            0, 0, "", "[]", _now(), jid,
        ),
    )
    scheduler_module.reload_jobs()
    return {"ok": True}


@app.post("/api/jobs/{jid}/pause")
def jobs_pause(jid: str):
    db.execute("UPDATE jobs SET enabled=0,status='PAUSED',updated_at=? WHERE id=?", (_now(), jid))
    scheduler_module.reload_jobs()
    return {"ok": True, "message": "任务已暂停"}


@app.post("/api/jobs/{jid}/resume")
def jobs_resume(jid: str):
    rows = db.execute("SELECT * FROM jobs WHERE id=?", (jid,), fetch=True)
    if not rows:
        raise HTTPException(404, "定时任务不存在")
    job = _job_to_dict(rows[0])
    repeat_count = max(0, int(job.get("repeat_count") or 0))
    if repeat_count > 0 and max(0, int(job.get("total_runs") or 0)) >= repeat_count:
        db.execute("UPDATE jobs SET enabled=0,status='COMPLETED',updated_at=? WHERE id=?", (_now(), jid))
        scheduler_module.reload_jobs()
        raise HTTPException(400, "定时任务执行次数已完成，请编辑任务增加执行次数后再恢复")
    db.execute("UPDATE jobs SET enabled=1,status='ACTIVE',updated_at=? WHERE id=?", (_now(), jid))
    scheduler_module.reload_jobs()
    return {"ok": True, "message": "任务已恢复"}


@app.post("/api/jobs/{jid}/run_now")
def jobs_run_now(jid: str):
    rows = db.execute("SELECT * FROM jobs WHERE id=?", (jid,), fetch=True)
    if not rows:
        raise HTTPException(404, "定时任务不存在")
    job = _job_to_dict(rows[0])
    task_ids = _dispatch_job_once(job, f"job:{jid}")
    return {"ok": True, "task_ids": task_ids, "task_count": len(task_ids)}


@app.delete("/api/jobs/{jid}")
def jobs_delete(jid: str):
    db.execute("DELETE FROM jobs WHERE id=?", (jid,))
    scheduler_module.reload_jobs()
    return {"ok": True}


# ===== 系统状态 =====
@app.get("/api/status")
def status():
    redis_ok = redis_queue.available()
    qlen = redis_queue.queue_len() if redis_ok else 0
    task_rows = db.execute(
        "SELECT status, COUNT(*) c FROM tasks "
        "WHERE COALESCE(module,'ui')<>'api' GROUP BY status", fetch=True) or []
    stats = {db.to_dict(r)["status"]: db.to_dict(r)["c"] for r in task_rows}
    job_rows = db.execute(
        "SELECT COUNT(*) c FROM jobs WHERE COALESCE(module,'ui')<>'api'", fetch=True)
    job_count = db.to_dict(job_rows[0])["c"] if job_rows else 0
    # 检查 worker 进程是否存活
    worker_alive = _check_worker_alive()
    return {
        "redis": redis_ok,
        "queue_length": qlen,
        "task_stats": stats,
        "job_count": job_count,
        "project_count": len(project_service.list_projects()),
        "allure_cli": settings.ALLURE_CLI,
        "worker_alive": worker_alive,
        "allow_python_edit": settings.ALLOW_PYTHON_EDIT,
    }


def _check_worker_alive() -> bool:
    """检查 worker 是否存活，优先使用支持跨容器的 Redis 心跳。"""
    try:
        return redis_queue.worker_count() > 0
    except Exception:
        pass
    try:
        import os as _os
        for _pid in _os.listdir("/proc"):
            if _pid.isdigit():
                try:
                    with open(f"/proc/{_pid}/cmdline", "rb") as _f:
                        if b"backend.worker" in _f.read():
                            return True
                except Exception:
                    pass
    except Exception:
        pass
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", "backend.worker"],
            capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _count_workers() -> int:
    """统计当前存活的 worker 数，优先使用支持跨容器的 Redis 心跳。"""
    try:
        return redis_queue.worker_count()
    except Exception:
        pass
    try:
        import os as _os
        _count = 0
        for _pid in _os.listdir("/proc"):
            if _pid.isdigit():
                try:
                    with open(f"/proc/{_pid}/cmdline", "rb") as _f:
                        if b"backend.worker" in _f.read():
                            _count += 1
                except Exception:
                    pass
        if _count > 0:
            return _count
    except Exception:
        pass
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", "backend.worker"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return len([p for p in result.stdout.split("\n") if p.strip()])
        return 0
    except Exception:
        return 0


def _start_worker_process(count: int = 1) -> dict:
    """在后台启动新的 worker 进程。

    使用 subprocess.Popen 启动 `python -m backend.worker`，
    重定向 stdout/stderr 到日志文件，start_new_session=True 让进程独立于父进程。
    """
    import subprocess as _sp
    import sys as _sys

    started = []
    errors = []
    worker_log_dir = os.path.join(settings.LOGS_DIR, "workers")
    os.makedirs(worker_log_dir, exist_ok=True)

    for i in range(count):
        try:
            log_file = os.path.join(
                worker_log_dir,
                f"worker_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time())}_{i}.log"
            )
            log_fp = open(log_file, "a", encoding="utf-8")
            # start_new_session=True：让 worker 进程脱离父进程会话组，
            # 即使 API 请求结束或 uvicorn 重载，worker 也不会被杀死
            proc = _sp.Popen(
                [_sys.executable, "-m", "backend.worker"],
                cwd=settings.ROOT,
                stdout=log_fp,
                stderr=_sp.STDOUT,
                stdin=_sp.DEVNULL,
                start_new_session=True,
                # 确保 worker 命令行包含 "backend.worker" 字样，
                # 这样 _check_worker_alive 才能检测到
            )
            started.append({"pid": proc.pid, "log": log_file})
        except Exception as e:
            errors.append(str(e))

    # 等待 1.5s 让 worker 进程完成初始化
    time.sleep(1.5)

    return {
        "started": started,
        "errors": errors,
        "alive_count": _count_workers(),
    }


def _start_redis() -> dict:
    """自动启动 Redis 服务。

    按优先级尝试以下启动方式：
    1. `brew services start redis`（推荐，brew 托管进程，开机自启）
    2. `redis-server --daemonize yes`（直接启动守护进程）
    3. 兜底：查找 redis-server 完整路径后启动

    启动后等待最多 5s 确认 Redis 可用。
    """
    import subprocess as _sp
    import shutil as _shutil

    methods_tried = []
    started = False
    method = ""
    detail = ""

    # 方式1：brew services start redis（macOS Homebrew）
    brew_bin = _shutil.which("brew")
    if brew_bin:
        methods_tried.append("brew services")
        try:
            result = _sp.run(
                [brew_bin, "services", "start", "redis"],
                capture_output=True, text=True, timeout=15
            )
            out = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                method = "brew services start redis"
                detail = out
                started = True
            else:
                detail = f"brew services 失败: {out}"
        except Exception as e:
            detail = f"brew services 异常: {e}"

    # 方式2：redis-server --daemonize yes（直接启动）
    if not started:
        redis_bin = _shutil.which("redis-server")
        if redis_bin:
            methods_tried.append("redis-server --daemonize")
            try:
                # 读取配置的端口，确保启动在正确端口
                redis_port = int(settings.REDIS_CFG.get("port", 6379))
                result = _sp.run(
                    [redis_bin, "--daemonize", "yes", "--port", str(redis_port)],
                    capture_output=True, text=True, timeout=10
                )
                out = (result.stdout + result.stderr).strip()
                if result.returncode == 0:
                    method = f"redis-server --daemonize yes --port {redis_port}"
                    detail = out
                    started = True
                else:
                    detail = f"{detail} | redis-server 失败: {out}".strip(" |")
            except Exception as e:
                detail = f"{detail} | redis-server 异常: {e}".strip(" |")

    # 等待 Redis 完全就绪（最多 5s）
    ping_ok = False
    for _ in range(10):
        try:
            import redis as _redis
            client = _redis.Redis(
                host=settings.REDIS_CFG.get("host", "localhost"),
                port=int(settings.REDIS_CFG.get("port", 6379)),
                db=int(settings.REDIS_CFG.get("db", 0)),
            )
            client.ping()
            ping_ok = True
            break
        except Exception:
            time.sleep(0.5)

    return {
        "started": started,
        "ping_ok": ping_ok,
        "method": method,
        "methods_tried": methods_tried,
        "detail": detail,
    }


def _kill_zombie_processes():
    """杀掉僵尸 pytest 和 playwright driver 进程"""
    import subprocess as _sp
    killed = []
    try:
        # Docker 容器内的 pytest 命令行通常不包含项目目录名。
        result = _sp.run(
            ["pgrep", "-f", "python.*-m pytest|pytest.*--alluredir|pytest.*tests"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.split("\n") if p.strip()]
        for pid in pids:
            try:
                _sp.run(["kill", "-9", pid], timeout=3)
                killed.append(f"pytest({pid})")
            except Exception:
                pass
        # 查找 playwright driver 进程
        result = _sp.run(
            ["pgrep", "-f", "playwright.*driver"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.split("\n") if p.strip()]
        for pid in pids:
            try:
                _sp.run(["kill", "-9", pid], timeout=3)
                killed.append(f"playwright({pid})")
            except Exception:
                pass
    except Exception:
        pass
    return killed


@app.post("/api/worker/restart")
def worker_restart(worker_count: int = 1, auto_start: bool = True):
    """重启 Worker：检查 Redis → 杀僵尸进程 → 清队列 → 重派 pending 任务 → 自动启动新 worker。

    参数：
    - worker_count: 启动的 worker 进程数（默认 1，UI 测试建议 1 个）
    - auto_start: 是否自动启动新 worker 进程（默认 True）

    若 Redis 不可用，会先尝试自动启动 Redis（brew services / redis-server --daemonize）。
    """
    import subprocess as _sp

    # 0. 检查 Redis 是否可用，不可用则自动启动
    redis_result = None
    if not redis_queue.available():
        redis_result = _start_redis()
    redis_ok = redis_queue.available()

    # 1. 杀掉僵尸 pytest 和 playwright 进程
    killed = _kill_zombie_processes()

    # 2. 杀掉旧的 worker 进程（确保完全重启，避免旧配置残留）
    old_worker_count = 0
    try:
        result = _sp.run(
            ["pgrep", "-f", "backend.worker"],
            capture_output=True, text=True, timeout=3
        )
        old_pids = [p.strip() for p in result.stdout.split("\n") if p.strip()]
        old_worker_count = len(old_pids)
        for pid in old_pids:
            try:
                _sp.run(["kill", "-9", pid], timeout=3)
                killed.append(f"worker({pid})")
            except Exception:
                pass
    except Exception:
        pass

    # 等待旧进程完全退出
    time.sleep(1)

    # 3. 清空 Redis 队列残留（使用 settings 中配置的队列名）
    queue_name = settings.REDIS_CFG.get("queue", "test_tasks")
    queue_cleared = 0
    if redis_ok:
        try:
            import redis as _redis
            client = _redis.Redis(
                host=settings.REDIS_CFG.get("host", "localhost"),
                port=int(settings.REDIS_CFG.get("port", 6379)),
                db=int(settings.REDIS_CFG.get("db", 0)),
            )
            queue_cleared = client.llen(queue_name) or 0
            client.delete(queue_name)
        except Exception:
            pass

    # 4. 把所有 pending 状态的任务重新入队
    pending_tasks = db.execute(
        "SELECT id FROM tasks WHERE status='pending' ORDER BY started_at",
        fetch=True
    ) or []
    redispatched = []
    if redis_ok:
        for row in pending_tasks:
            tid = db.to_dict(row)["id"]
            try:
                redis_queue.push_task({"task_id": tid})
                redispatched.append(tid)
            except Exception:
                pass

    # 5. 自动启动新 worker 进程
    start_result = None
    if auto_start and redis_ok:
        start_result = _start_worker_process(count=worker_count)
    elif auto_start and not redis_ok:
        start_result = {"started": [], "errors": ["Redis 不可用，跳过 worker 启动"], "alive_count": 0}

    # 6. 检查 worker 是否存活
    worker_alive = _check_worker_alive()
    alive_count = _count_workers()

    message_parts = []
    if redis_result:
        if redis_result["ping_ok"]:
            message_parts.append(f"✅ Redis 已启动 ({redis_result['method']})")
        else:
            message_parts.append(f"❌ Redis 启动失败 ({redis_result['detail'][:80]})")
    else:
        message_parts.append("Redis 已在线")
    message_parts.append(f"杀掉 {len(killed)} 个进程")
    message_parts.append(f"清理队列 {queue_cleared} 条")
    message_parts.append(f"重派 {len(redispatched)} 个任务")
    if start_result:
        if start_result.get("started"):
            pids = [str(s["pid"]) for s in start_result["started"]]
            message_parts.append(f"启动 {len(start_result['started'])} 个 worker (PID: {', '.join(pids)})")
        if start_result.get("errors"):
            message_parts.append(f"启动失败 {len(start_result['errors'])} 次")
    message_parts.append(f"当前存活 worker: {alive_count} 个")

    return {
        "ok": True,
        "redis_ok": redis_ok,
        "redis_result": redis_result,
        "killed_processes": killed,
        "queue_cleared": queue_cleared,
        "redispatched": redispatched,
        "worker_alive": worker_alive,
        "worker_count": alive_count,
        "start_result": start_result,
        "old_worker_count": old_worker_count,
        "message": " | ".join(message_parts),
    }


@app.post("/api/worker/stop_task/{tid}")
def worker_stop_and_clean(tid: str):
    """停止指定任务：杀掉其 pytest 进程 + 标记为 stopped"""
    # 标记为 stopped
    db.execute("UPDATE tasks SET status='stopped', finished_at=? WHERE id=?",
               (time.strftime("%Y-%m-%d %H:%M:%S"), tid))

    # 杀掉该任务的 pytest 进程
    killed = []
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", f"TASK_ID={tid}"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.split("\n") if p.strip()]
        for pid in pids:
            try:
                _sp.run(["kill", "-9", pid], timeout=3)
                killed.append(pid)
            except Exception:
                pass
    except Exception:
        pass

    return {"ok": True, "task_id": tid, "killed_pids": killed}


# ===================== 日志管理 =====================

@app.get("/api/logs/stats")
def logs_stats():
    """获取所有日志目录的统计信息（大小、文件数、最早/最新时间）。"""
    from backend import log_cleanup
    return log_cleanup.get_stats()


@app.post("/api/logs/cleanup")
def logs_cleanup(retention_days: int = 30, dry_run: bool = False):
    """手动清理超过保留天数的日志文件。

    参数：
    - retention_days: 保留最近多少天的日志（默认 30）
    - dry_run: 试运行模式，只统计不实际删除（默认 false）
    """
    from backend import log_cleanup
    return log_cleanup.cleanup_old_logs(retention_days=retention_days, dry_run=dry_run)


@app.get("/api/logs/config")
def logs_config_get():
    """获取自动清理配置。"""
    from backend import log_cleanup
    return log_cleanup.get_config()


@app.post("/api/logs/config")
def logs_config_set(enabled: bool = None, retention_days: int = None):
    """更新自动清理配置。

    参数（都是可选，只更新传入的字段）：
    - enabled: 是否启用自动清理
    - retention_days: 保留天数
    """
    from backend import log_cleanup
    return log_cleanup.set_config(enabled=enabled, retention_days=retention_days)


@app.get("/api/notifications")
def notification_logs(project_id: str = "", limit: int = 100):
    params = []
    where = "1=1"
    if project_id:
        where += " AND project_id=?"
        params.append(project_id)
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM notification_logs WHERE {where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
        fetch=True,
    )
    result = []
    for row in rows or []:
        item = db.to_dict(row)
        for field in ("content", "response_info"):
            try:
                item[field] = json.loads(item.get(field) or "{}")
            except (TypeError, ValueError):
                item[field] = {}
        result.append(item)
    return result


@app.delete("/api/notifications/batch")
def notification_logs_batch_delete(body: NotificationBatchDeleteIn):
    deleted = 0
    errors = []
    for raw_id in body.ids or []:
        notification_id = str(raw_id or "").strip()
        if not notification_id:
            continue
        try:
            rows = db.execute("SELECT id FROM notification_logs WHERE id=?", (notification_id,), fetch=True)
            if not rows:
                errors.append({"id": notification_id, "error": "通知记录不存在"})
                continue
            db.execute("DELETE FROM notification_logs WHERE id=?", (notification_id,))
            deleted += 1
        except Exception as exc:
            errors.append({"id": notification_id, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


@app.get("/api/report-notifications/targets")
def report_notification_targets():
    """List enabled third-party integrations without exposing credentials."""
    from backend import feishu

    return {"targets": feishu.notification_targets()}


@app.post("/api/report-notifications/send")
def send_report_notifications(body: ReportNotificationSendIn):
    """Send selected UI automation reports to selected integrations."""
    from backend import feishu

    report_ids = list(dict.fromkeys(
        safe_identifier(item, label="task_id") for item in body.report_ids
    ))
    notification_ids = list(dict.fromkeys(
        str(item or "").strip() for item in body.notification_ids if str(item or "").strip()
    ))
    bots = feishu.select_bots(notification_ids)
    found_notification_ids = {str(bot.get("id") or "") for bot in bots}
    missing_notification_ids = [
        item for item in notification_ids if item not in found_notification_ids
    ]
    if not bots:
        raise HTTPException(400, "选择的通知不存在或已停用")

    placeholders = ",".join("?" for _ in report_ids)
    rows = db.execute(
        "SELECT t.*, p.name as project_name FROM tasks t "
        "LEFT JOIN projects p ON t.project_id=p.id "
        f"WHERE t.id IN ({placeholders}) AND COALESCE(t.module,'ui')<>'api'",
        tuple(report_ids),
        fetch=True,
    )
    tasks_by_id = {
        str(item["id"]): _normalize_task_report(item)
        for item in db.to_dicts(rows)
    }
    sent = 0
    failed = 0
    results = []
    for report_id in report_ids:
        task = tasks_by_id.get(report_id)
        if not task or not task.get("report_url"):
            failed += 1
            results.append({"report_id": report_id, "success": False, "reason": "报告不存在"})
            continue
        send_result = feishu.send_card_to_bots(task, bots)
        target_results = send_result.get("results") or []
        success_count = sum(1 for item in target_results if item.get("success"))
        failure_count = len(target_results) - success_count
        sent += success_count
        failed += failure_count
        results.append({
            "report_id": report_id,
            "success": bool(target_results) and failure_count == 0,
            "targets": target_results,
        })

    return {
        "report_count": len(report_ids),
        "notification_count": len(bots),
        "sent": sent,
        "failed": failed,
        "missing_notification_ids": missing_notification_ids,
        "results": results,
    }


# ===== Pages（页面对象）=====
def _require_python_edit_enabled():
    if not settings.ALLOW_PYTHON_EDIT:
        raise HTTPException(403, "生产环境已禁用在线 Python 文件编辑")


@app.get("/api/projects/{pid}/pages")
def pages_list(pid: str):
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    pages_dir = project_service.get_pages_dir(pid)
    result = []
    if os.path.isdir(pages_dir):
        for fn in sorted(os.listdir(pages_dir)):
            if fn.endswith(".py") and fn != "__init__.py":
                try:
                    fp = safe_child_path(
                        pages_dir, fn, allowed_suffixes=(".py",), filename_only=True
                    )
                except ValueError:
                    continue
                if not os.path.isfile(fp):
                    continue
                result.append({
                    "name": fn,
                    "size": os.path.getsize(fp),
                    "mtime": int(os.path.getmtime(fp)),
                })
    return result


@app.get("/api/projects/{pid}/pages/{filename}")
def pages_get(pid: str, filename: str):
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    pages_dir = project_service.get_pages_dir(pid)
    try:
        fp = safe_child_path(
            pages_dir, filename, allowed_suffixes=(".py",), filename_only=True
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not os.path.isfile(fp):
        raise HTTPException(404, "文件不存在")
    with open(fp, "r", encoding="utf-8") as f:
        return {"filename": filename, "content": f.read()}


class PageIn(BaseModel):
    content: str


@app.post("/api/projects/{pid}/pages/{filename}")
def pages_save(pid: str, filename: str, body: PageIn):
    _require_python_edit_enabled()
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    pages_dir = project_service.get_pages_dir(pid)
    os.makedirs(pages_dir, exist_ok=True)
    try:
        fp = safe_child_path(
            pages_dir, filename, allowed_suffixes=(".py",), filename_only=True
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with open(fp, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True}


@app.delete("/api/projects/{pid}/pages/{filename}")
def pages_delete(pid: str, filename: str):
    _require_python_edit_enabled()
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    pages_dir = project_service.get_pages_dir(pid)
    try:
        fp = safe_child_path(
            pages_dir, filename, allowed_suffixes=(".py",), filename_only=True
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if os.path.isfile(fp):
        os.remove(fp)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


# ===== Scripts（测试脚本）=====
@app.get("/api/projects/{pid}/scripts")
def scripts_list(pid: str):
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    scripts_dir = project_service.get_ui_test_dir(pid)
    result = []
    def _scan(d, prefix=""):
        if not os.path.isdir(d):
            return
        for fn in sorted(os.listdir(d)):
            fp = os.path.join(d, fn)
            rel = f"{prefix}/{fn}" if prefix else fn
            if os.path.islink(fp):
                continue
            if os.path.isdir(fp):
                _scan(fp, rel)
            elif fn.endswith(".py") and fn != "__init__.py":
                result.append({
                    "name": rel,
                    "size": os.path.getsize(fp),
                    "mtime": int(os.path.getmtime(fp)),
                })
    _scan(scripts_dir)
    return result


@app.get("/api/projects/{pid}/scripts/{path:path}")
def scripts_get(pid: str, path: str):
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    scripts_dir = project_service.get_ui_test_dir(pid)
    fp = _safe_child_path(scripts_dir, path)
    if not os.path.isfile(fp):
        raise HTTPException(404, "文件不存在")
    with open(fp, "r", encoding="utf-8") as f:
        return {"filename": path, "content": f.read()}


class ScriptIn(BaseModel):
    content: str


def _safe_child_path(root: str, path: str) -> str:
    try:
        return safe_child_path(root, path, allowed_suffixes=(".py",))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{pid}/scripts/{path:path}")
def scripts_save(pid: str, path: str, body: ScriptIn):
    _require_python_edit_enabled()
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    scripts_dir = project_service.get_ui_test_dir(pid)
    fp = _safe_child_path(scripts_dir, path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True}


@app.delete("/api/projects/{pid}/scripts/{path:path}")
def scripts_delete(pid: str, path: str):
    _require_python_edit_enabled()
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    scripts_dir = project_service.get_ui_test_dir(pid)
    fp = _safe_child_path(scripts_dir, path)
    if os.path.isfile(fp):
        os.remove(fp)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


@app.get("/api/projects/{pid}/stats")
def project_stats(pid: str):
    """获取项目统计信息：用例数、pages数、脚本数"""
    proj = project_service.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    # 用例数
    try:
        case_count = len(case_service.list_cases(pid, "ui"))
    except Exception:
        case_count = 0
    # pages 数
    pages_dir = project_service.get_pages_dir(pid)
    page_count = len([f for f in os.listdir(pages_dir) if f.endswith(".py") and f != "__init__.py"]) if os.path.isdir(pages_dir) else 0
    # 脚本数
    scripts_dir = project_service.get_ui_test_dir(pid)
    script_count = 0
    for root, dirs, files in os.walk(scripts_dir):
        script_count += len([f for f in files if f.endswith(".py") and f != "__init__.py"])
    return {"case_count": case_count, "page_count": page_count, "script_count": script_count}


@app.get("/api/logs")
def legacy_logs():
    """兼容旧前端：返回最新任务日志。"""
    ids = log_store.list_logs()
    return {"log": log_store.read(ids[-1]) if ids else ""}


# ===== 前端地址路由 =====
UI_PAGE_ROUTES = {
    "dashboard", "agent", "projects", "cases", "test-cases", "ui-scenes", "web-recorder",
    "webRecorder", "run", "load-tests", "tasks", "executions", "reports",
    "jobs", "settings", "environment", "test-suites", "notifications", "loadrunner", "loadrunner-manual",
}


@app.get("/{page_path}", response_class=HTMLResponse)
def ui_page_route(page_path: str):
    """支持直接访问、刷新 UI 自动化各功能页面的地址路由。"""
    if page_path not in UI_PAGE_ROUTES:
        raise HTTPException(404, "页面不存在")
    return index()
