"""AI Agent 工作流执行器：状态机 + 步骤执行 + 工具调度。

状态流转：
    CREATED -> ANALYZING -> GENERATING_DATA -> GENERATING_CASE
    -> RUNNING -> ANALYZING(失败分析) -> COMPLETED / FAILED / STOPPED
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

from backend import db, settings
from backend.agent import planner
from backend.agent.analyzer import analyze_task_failures
from backend.agent.tools import run_tool
from backend.agent.state import (
    ANALYZING,
    COMPLETED,
    CREATED,
    FAILED,
    FAILURE_ANALYZING,
    GENERATING_CASE,
    GENERATING_DATA,
    RUNNING,
    STOPPED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCESS,
    TERMINAL_WORKFLOW_STATUSES,
)


_active_runs: set[str] = set()
_stop_requests: set[str] = set()
_run_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _json_dump(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _json_load(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


# ============ 任务 CRUD ============

def create_agent_workflow_task(
    *,
    task_name: str = "",
    user_requirement: str,
    project_id: str = "default",
    auto_execute: bool = True,
    created_by: str = "",
    start: bool = True,
) -> dict:
    """创建 AI Agent 工作流任务（状态 CREATED）。"""
    user_requirement = str(user_requirement or "").strip()
    if len(user_requirement) < 4:
        raise ValueError("测试需求至少需要 4 个字符")
    task_id = uuid.uuid4().hex[:12]
    now = _now()
    db.execute(
        "INSERT INTO agent_tasks("
        "id, task_name, user_requirement, project_id, status, auto_execute, plan, dimension_plan, "
        "progress, error_message, created_by, created_at, updated_at, started_at, finished_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            str(task_name or "")[:120],
            user_requirement,
            project_id or "default",
            CREATED,
            int(bool(auto_execute)),
            "{}",
            "{}",
            0,
            "",
            created_by or "",
            now,
            now,
            None,
            None,
        ),
    )
    if start and auto_execute:
        start_workflow_task(task_id)
    return get_workflow_task(task_id)


def _serialize_workflow_task(row: Any) -> dict:
    item = db.to_dict(row)
    item["plan"] = _json_load(item.get("plan"))
    item["dimension_plan"] = _json_load(item.get("dimension_plan"))
    item["auto_execute"] = bool(item.get("auto_execute"))
    return item


def get_workflow_task(task_id: str) -> Optional[dict]:
    rows = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,), fetch=True)
    if not rows:
        return None
    item = _serialize_workflow_task(rows[0])
    item["steps"] = get_workflow_timeline(task_id)
    item["memory"] = list_agent_memory(task_id)
    item["results"] = get_execution_results(task_id)
    return item


def list_workflow_tasks(project_id: str = "", limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    if project_id:
        rows = db.execute(
            "SELECT * FROM agent_tasks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
            fetch=True,
        )
    else:
        rows = db.execute(
            "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
    return [_serialize_workflow_task(row) for row in rows]


def update_workflow_task_status(
    task_id: str,
    status: str,
    *,
    progress: Optional[int] = None,
    error_message: str = "",
) -> None:
    """更新任务状态（供外部与状态机使用）。"""
    fields: list[str] = []
    params: list[Any] = []
    if status:
        fields.append("status=?")
        params.append(status)
    if progress is not None:
        fields.append("progress=?")
        params.append(int(progress))
    if error_message is not None:
        fields.append("error_message=?")
        params.append(str(error_message)[:2000])
    if status in TERMINAL_WORKFLOW_STATUSES:
        fields.append("finished_at=?")
        params.append(_now())
    if not fields:
        return
    fields.append("updated_at=?")
    params.append(_now())
    db.execute(
        f"UPDATE agent_tasks SET {', '.join(fields)} WHERE id=?",
        tuple(params) + (task_id,),
    )


def delete_workflow_task(task_id: str) -> None:
    with _run_lock:
        _stop_requests.add(task_id)
        _active_runs.discard(task_id)
    db.execute("DELETE FROM agent_steps WHERE agent_task_id=?", (task_id,))
    db.execute("DELETE FROM agent_memory WHERE agent_task_id=?", (task_id,))
    db.execute("DELETE FROM test_execution_result WHERE agent_task_id=?", (task_id,))
    db.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))


def recover_workflow_tasks() -> None:
    """服务重启后，把中断的非终态工作流标记为 FAILED。"""
    rows = db.execute(
        "SELECT id FROM agent_tasks WHERE status NOT IN ('COMPLETED','FAILED','STOPPED')",
        fetch=True,
    ) or []
    for row in rows:
        update_workflow_task_status(
            row["id"], FAILED, progress=100, error_message="服务重启导致任务中断，请重新发起任务"
        )


# ============ 步骤 ============

def _add_step(
    task_id: str,
    *,
    phase: str,
    step_type: str,
    action: str,
    tool: str = "",
    payload: Optional[dict] = None,
) -> int:
    """新增步骤，返回 step id。"""
    max_no = db.execute(
        "SELECT COALESCE(MAX(step_no), 0) AS max_no FROM agent_steps WHERE agent_task_id=?",
        (task_id,),
        fetch=True,
    )
    step_no = int(max_no[0]["max_no"]) + 1 if max_no else 1
    now = _now()
    db.execute(
        "INSERT INTO agent_steps("
        "agent_task_id, step_no, phase, type, action, tool, status, input_payload, "
        "output_payload, error_message, started_at, finished_at, duration) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            step_no,
            phase,
            step_type,
            str(action or "")[:300],
            tool,
            STEP_PENDING,
            _json_dump(payload or {}),
            "{}",
            "",
            None,
            None,
            0,
        ),
    )
    rows = db.execute(
        "SELECT id FROM agent_steps WHERE agent_task_id=? AND step_no=?",
        (task_id, step_no),
        fetch=True,
    )
    return int(rows[0]["id"]) if rows else step_no


def _update_step(
    step_id: int,
    *,
    status: Optional[str] = None,
    output: Optional[dict] = None,
    error_message: str = "",
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if status:
        fields.append("status=?")
        params.append(status)
    if output is not None:
        fields.append("output_payload=?")
        params.append(_json_dump(output))
    if error_message:
        fields.append("error_message=?")
        params.append(str(error_message)[:2000])
    if status in {STEP_SUCCESS, STEP_FAILED, STEP_SKIPPED}:
        fields.append("finished_at=?")
        params.append(_now())
    if status == STEP_RUNNING:
        fields.append("started_at=?")
        params.append(_now())
    if not fields:
        return
    db.execute(
        f"UPDATE agent_steps SET {', '.join(fields)} WHERE id=?",
        tuple(params) + (step_id,),
    )
    if status in {STEP_SUCCESS, STEP_FAILED, STEP_SKIPPED}:
        started = db.execute(
            "SELECT started_at FROM agent_steps WHERE id=?", (step_id,), fetch=True
        )
        if started and started[0]["started_at"]:
            try:
                duration = time.mktime(time.strptime(_now(), "%Y-%m-%d %H:%M:%S")) - time.mktime(
                    time.strptime(started[0]["started_at"], "%Y-%m-%d %H:%M:%S")
                )
                db.execute(
                    "UPDATE agent_steps SET duration=? WHERE id=?",
                    (round(max(duration, 0), 2), step_id),
                )
            except (ValueError, TypeError):
                pass


def get_workflow_timeline(task_id: str) -> list[dict]:
    """按执行顺序返回步骤 Timeline。"""
    rows = db.execute(
        "SELECT * FROM agent_steps WHERE agent_task_id=? ORDER BY step_no",
        (task_id,),
        fetch=True,
    ) or []
    steps = []
    for row in rows:
        item = db.to_dict(row)
        item["input_payload"] = _json_load(item.get("input_payload"))
        item["output_payload"] = _json_load(item.get("output_payload"))
        steps.append(item)
    return steps


def get_execution_results(task_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM test_execution_result WHERE agent_task_id=? ORDER BY id",
        (task_id,),
        fetch=True,
    ) or []
    results = []
    for row in rows:
        item = db.to_dict(row)
        item["screenshots"] = _json_load(item.get("screenshots"), [])
        item["failure_analysis"] = _json_load(item.get("failure_analysis"))
        results.append(item)
    return results


def list_agent_memory(task_id: str) -> list[dict]:
    from backend.agent.memory import list_memory
    return list_memory(task_id)


# ============ 工作流执行 ============

def _stop_requested(task_id: str) -> bool:
    return task_id in _stop_requests


def _mark_remaining_steps_skipped(task_id: str) -> None:
    db.execute(
        "UPDATE agent_steps SET status=?, finished_at=?, error_message='已停止，后续步骤跳过' "
        "WHERE agent_task_id=? AND status IN ('pending','running')",
        (STEP_SKIPPED, _now(), task_id),
    )


def _ui_task_terminal(status: str) -> bool:
    return str(status or "").lower() in {"success", "failed", "stopped"}


def _wait_ui_task(agent_task_id: str, ui_task_id: str, timeout: int = 1800) -> Optional[dict]:
    """轮询等待 UI 子任务进入终态，返回任务行；超时或被停止时返回 None。"""
    deadline = time.time() + max(1, int(timeout))
    while time.time() < deadline:
        if _stop_requested(agent_task_id):
            return None
        rows = db.execute(
            "SELECT id, status, passed, failed, skipped, total, duration, report_url, finished_at "
            "FROM tasks WHERE id=?",
            (ui_task_id,),
            fetch=True,
        )
        if rows and _ui_task_terminal(rows[0]["status"]):
            return db.to_dict(rows[0])
        time.sleep(5)
    return None


def _ui_failure_artifacts(ui_task_id: str) -> dict:
    """读取 UI 子任务失败步骤、截图和精简日志，供 Agent 报告/失败分析使用。"""
    artifacts = {
        "failure_details": [],
        "screenshots": [],
        "execution_steps": [],
        "step_counts": {},
        "logs": "",
    }
    steps_path = os.path.join(settings.RUNTIME_DIR, "task_steps", f"{ui_task_id}.json")
    try:
        with open(steps_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        artifacts["step_counts"] = payload.get("counts") or {}
        for step in payload.get("steps") or []:
            screenshot = step.get("screenshot") or ""
            artifacts["execution_steps"].append({
                "index": step.get("step_index") or step.get("index"),
                "action": step.get("action") or "",
                "name": step.get("name") or "",
                "status": step.get("status") or "",
                "duration": step.get("duration") or 0,
                "error": step.get("error") or "",
                "screenshot": screenshot,
                "log": step.get("log") or "",
            })
            if step.get("status") != "failed":
                continue
            detail = {
                "case": payload.get("flow_name") or payload.get("case_file") or "",
                "step_index": step.get("step_index") or step.get("index"),
                "step": step.get("name") or step.get("action") or "",
                "error": step.get("error") or step.get("log") or "",
                "screenshot": screenshot,
            }
            artifacts["failure_details"].append(detail)
            if screenshot:
                artifacts["screenshots"].append(screenshot)
    except (OSError, json.JSONDecodeError):
        pass

    log_path = os.path.join(settings.LOGS_DIR, f"{ui_task_id}.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        lines = []
        for line in text.splitlines():
            if (
                "FAILED " in line
                or "AssertionError" in line
                or "❌" in line
                or "输入值未稳定保存" in line
                or "执行完成 | 状态=failed" in line
            ):
                lines.append(line)
        artifacts["logs"] = "\n".join(lines[-80:])[:4000]
        if not artifacts["failure_details"]:
            failed_cases = re.findall(r"FAILED\s+(tests/ui/[^\s]+)", text)
            errors = re.findall(r"(AssertionError: .+)", text)
            for index, case in enumerate(dict.fromkeys(failed_cases), start=1):
                error = errors[-1] if errors else ""
                artifacts["failure_details"].append({
                    "case": case,
                    "step_index": "",
                    "step": "UI 用例执行失败",
                    "error": error,
                    "screenshot": "",
                })
    except OSError:
        pass
    return artifacts


def _update_execution_result(*, agent_task_id: str, step_id: int, result: dict) -> None:
    """把等待后的真实结果回写 test_execution_result，供报告使用。"""
    failure_analysis = dict(result.get("failure_analysis") or {})
    if result.get("failure_details"):
        failure_analysis["failure_details"] = result.get("failure_details")
    if result.get("execution_steps"):
        failure_analysis["execution_steps"] = result.get("execution_steps")
    if result.get("step_counts"):
        failure_analysis["step_counts"] = result.get("step_counts")
    db.execute(
        "UPDATE test_execution_result SET status=?, summary=?, logs=?, screenshots=?, api_response=?, failure_analysis=? "
        "WHERE agent_task_id=? AND step_id=?",
        (
            str(result.get("status") or "failed")[:50],
            str(result.get("summary") or result.get("message") or result.get("note") or "")[:1000],
            str(result.get("logs") or "")[:4000],
            _json_dump(result.get("screenshots") or []),
            str(result.get("response") or result.get("report_url") or "")[:4000],
            _json_dump(failure_analysis),
            agent_task_id,
            step_id,
        ),
    )


def start_workflow_task(task_id: str, *, force: bool = False) -> dict:
    """启动工作流（后台线程执行）。"""
    task = get_workflow_task(task_id)
    if not task:
        raise ValueError("工作流任务不存在")
    if task["status"] in TERMINAL_WORKFLOW_STATUSES and not force:
        raise ValueError(f"任务已处于终态（{task['status']}），如需重跑请重建任务")
    with _run_lock:
        if task_id in _active_runs:
            return {"task_id": task_id, "status": "already_running"}
        _active_runs.add(task_id)
        _stop_requests.discard(task_id)
    threading.Thread(
        target=_run_workflow,
        args=(task_id,),
        daemon=True,
        name=f"ai-agent-workflow-{task_id}",
    ).start()
    return {"task_id": task_id, "status": "started"}


def stop_workflow_task(task_id: str) -> dict:
    """请求停止工作流。"""
    task = get_workflow_task(task_id)
    if not task:
        raise ValueError("工作流任务不存在")
    _stop_requests.add(task_id)
    update_workflow_task_status(task_id, STOPPED)
    _mark_remaining_steps_skipped(task_id)
    with _run_lock:
        _active_runs.discard(task_id)
    return {"task_id": task_id, "status": "stopped"}


def _run_workflow(task_id: str) -> None:
    try:
        task = get_workflow_task(task_id)
        if not task:
            return
        requirement = task["user_requirement"]
        project_id = task["project_id"] or "default"

        # ---- 1. 分析需求 + 生成测试计划 ----
        update_workflow_task_status(task_id, ANALYZING, progress=5)
        analyze_step = _add_step(task_id, phase="analyze", step_type="ai", action="分析需求")
        _update_step(analyze_step, status=STEP_RUNNING)
        _update_step(analyze_step, status=STEP_SUCCESS, output={"message": "需求解析完成"})
        if _stop_requested(task_id):
            return

        plan = planner.build_workflow_plan(requirement, project_id=project_id)
        db.execute(
            "UPDATE agent_tasks SET plan=?, dimension_plan=?, updated_at=? WHERE id=?",
            (_json_dump(plan.get("steps")), _json_dump(plan.get("dimensions")), _now(), task_id),
        )
        plan_step = _add_step(
            task_id, phase="plan", step_type="ai", action="生成测试计划",
            payload={"summary": plan.get("summary"), "source": plan.get("source")},
        )
        _update_step(plan_step, status=STEP_SUCCESS, output={
            "summary": plan.get("summary"),
            "source": plan.get("source"),
            "steps": plan.get("steps", []),
        })

        # ---- 2. 创建测试数据 ----
        data_steps = [s for s in plan.get("steps", []) if s.get("type") == "data"]
        if data_steps:
            update_workflow_task_status(task_id, GENERATING_DATA, progress=20)
        for step in data_steps:
            if _stop_requested(task_id):
                return
            step_id = _add_step(
                task_id, phase="data", step_type="data", action=step.get("action", ""),
                tool=step.get("tool", "generate_data"),
                payload=dict(step.get("payload") or {}),
            )
            _update_step(step_id, status=STEP_RUNNING)
            payload = dict(step.get("payload") or {})
            payload.setdefault("project_id", project_id)
            result = run_tool(
                str(step.get("tool") or "generate_test_data"),
                agent_task_id=task_id,
                step_id=step_id,
                **payload,
            )
            _update_step(
                step_id,
                status=STEP_SUCCESS if result.get("status") == "success" else STEP_FAILED,
                output=result,
                error_message=str(result.get("error") or ""),
            )

        # ---- 3. 生成测试用例 ----
        update_workflow_task_status(task_id, GENERATING_CASE, progress=35)
        case_step = _add_step(
            task_id, phase="case", step_type="case", action="生成测试用例",
            tool="create_case", payload={"requirement": requirement},
        )
        _update_step(case_step, status=STEP_RUNNING)
        case_result = run_tool(
            "create_case",
            agent_task_id=task_id,
            step_id=case_step,
            requirement=requirement,
            dimension_plan=plan.get("dimensions"),
            project_id=project_id,
        )
        _update_step(
            case_step,
            status=STEP_SUCCESS if case_result.get("status") == "success" else STEP_FAILED,
            output=case_result,
            error_message=str(case_result.get("error") or ""),
        )
        if _stop_requested(task_id):
            return

        # ---- 4. 执行测试 ----
        run_steps = [
            s for s in plan.get("steps", [])
            if s.get("type") in {"api", "ui", "app", "security", "performance"}
        ]
        if run_steps:
            update_workflow_task_status(task_id, RUNNING, progress=50)
        for step in run_steps:
            if _stop_requested(task_id):
                return
            step_id = _add_step(
                task_id, phase="run", step_type=step.get("type", "run"),
                action=step.get("action", ""),
                tool=step.get("tool", "run_api_test"),
                payload=dict(step.get("payload") or {}),
            )
            _update_step(step_id, status=STEP_RUNNING)
            payload = dict(step.get("payload") or {})
            tool = step.get("tool", "run_api_test")
            if tool in {"run_ui_test", "run_web_test", "run_app_test", "run_security_test", "generate_from_api"}:
                payload.setdefault("project_id", project_id)
            if tool in {"run_api_test", "run_ui_test", "run_web_test"}:
                payload.setdefault("base_url", "")
            result = run_tool(
                tool,
                agent_task_id=task_id,
                step_id=step_id,
                **payload,
            )
            if result.get("status") == "dispatched" and result.get("task_id"):
                ui_task_id = str(result["task_id"])
                ui_timeout = int(os.environ.get("AI_AGENT_EXECUTION_TIMEOUT", "1800") or 1800)
                ui_task = _wait_ui_task(task_id, ui_task_id, timeout=ui_timeout)
                if ui_task is None:
                    if _stop_requested(task_id):
                        continue
                    result.update(
                        status="failed",
                        error=f"等待 UI 任务 {ui_task_id} 超过 {ui_timeout}s 未完成（worker 可能未消费或执行超时）",
                    )
                else:
                    ui_status = str(ui_task.get("status") or "failed").lower()
                    result.update(
                        status="success" if ui_status == "success" else "failed",
                        task_id=ui_task_id,
                        ui_status=ui_status,
                        summary={
                            "total": int(ui_task.get("total") or 0),
                            "passed": int(ui_task.get("passed") or 0),
                            "failed": int(ui_task.get("failed") or 0),
                            "skipped": int(ui_task.get("skipped") or 0),
                        },
                        report_url=ui_task.get("report_url") or "",
                        note="UI 自动化已执行完成（等待真实结果）",
                    )
                    if ui_status != "success":
                        result.update(_ui_failure_artifacts(ui_task_id))
                    if ui_status == "stopped":
                        result["error"] = "UI 子任务已被停止，结果不完整"
                _update_step(
                    step_id,
                    status=STEP_SUCCESS if result.get("status") in {"success", "skipped"} else STEP_FAILED,
                    output=result,
                    error_message=str(result.get("error") or ""),
                )
                try:
                    _update_execution_result(
                        agent_task_id=task_id,
                        step_id=step_id,
                        result=result,
                    )
                except Exception:  # noqa: BLE001 - 回写失败不影响工作流
                    pass
                continue
            _update_step(
                step_id,
                status=STEP_SUCCESS if result.get("status") in {"success", "dispatched", "skipped"} else STEP_FAILED,
                output=result,
                error_message=str(result.get("error") or ""),
            )
        if _stop_requested(task_id):
            return

        # ---- 5. AI 分析失败原因 ----
        failed_count = sum(
            1 for r in get_execution_results(task_id) if r.get("status") == "failed"
        )
        if failed_count:
            update_workflow_task_status(task_id, FAILURE_ANALYZING, progress=75)
            analysis_step = _add_step(
                task_id, phase="analysis", step_type="ai", action="AI 分析失败原因",
                payload={"failed_count": failed_count},
            )
            _update_step(analysis_step, status=STEP_RUNNING)
            analyses = analyze_task_failures(task_id)
            _update_step(
                analysis_step,
                status=STEP_SUCCESS,
                output={"failed_count": failed_count, "analyses": analyses},
            )

        # ---- 6. 生成报告 ----
        update_workflow_task_status(task_id, ANALYZING, progress=90)
        report_step = _add_step(task_id, phase="report", step_type="report", action="生成测试报告", tool="create_report")
        _update_step(report_step, status=STEP_RUNNING)
        results = [
            {
                "tool": r.get("tool"),
                "target": r.get("target"),
                "status": r.get("status"),
                "summary": r.get("summary"),
                "error": r.get("failure_analysis", {}).get("reason") or "",
                "suggestion": r.get("failure_analysis", {}).get("suggestion") or "",
                "failure_details": (r.get("failure_analysis", {}) or {}).get("failure_details") or [],
                "execution_steps": (r.get("failure_analysis", {}) or {}).get("execution_steps") or [],
                "step_counts": (r.get("failure_analysis", {}) or {}).get("step_counts") or {},
            }
            for r in get_execution_results(task_id)
        ]
        report_result = run_tool(
            "create_report",
            agent_task_id=task_id,
            step_id=report_step,
            title=f"AI Agent 测试报告 - {requirement[:40]}",
            results=results,
        )
        _update_step(
            report_step,
            status=STEP_SUCCESS if report_result.get("status") == "success" else STEP_FAILED,
            output=report_result,
            error_message=str(report_result.get("error") or ""),
        )
        if _stop_requested(task_id):
            return

        # ---- 完成 ----
        total_steps = len(get_workflow_timeline(task_id))
        failed_steps = sum(1 for s in get_workflow_timeline(task_id) if s.get("status") == STEP_FAILED)
        if failed_steps:
            update_workflow_task_status(task_id, FAILED, progress=100, error_message=f"{failed_steps} 个步骤执行失败，详见报告")
        else:
            update_workflow_task_status(task_id, COMPLETED, progress=100)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务落库为 FAILED
        try:
            update_workflow_task_status(
                task_id, FAILED, progress=100, error_message=f"工作流异常: {exc}"
            )
            db.execute(
                "UPDATE agent_steps SET status=?, error_message=? "
                "WHERE agent_task_id=? AND status IN ('pending','running')",
                (STEP_SKIPPED, f"工作流异常: {str(exc)[:500]}", task_id),
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        with _run_lock:
            _active_runs.discard(task_id)
            _stop_requests.discard(task_id)
