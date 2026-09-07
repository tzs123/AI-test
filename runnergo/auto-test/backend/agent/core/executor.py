"""ReAct 执行器：Agent Planner -> Action -> Observation -> Reflection -> Retry。

每个循环写入 agent_execution_log（大脑轨迹）与 agent_memory（任务记忆），
失败时自动调用失败分析并生成报告，状态写入 agent_tasks 与现有 v2 工作流兼容。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from backend import db
from backend.agent.core.decision_engine import (
    build_action_queue,
    decide_next_action,
    generate_thought,
)
from backend.agent.core.observer import observe, observation_text
from backend.agent.core.reflection import reflect
from backend.agent.core.tool_registry import execute_tool
from backend.agent.core.memory import remember_system_fact
from backend.agent.executor import (
    get_execution_results,
    get_workflow_task,
    get_workflow_timeline,
    _ui_failure_artifacts,
    _update_execution_result,
    _wait_ui_task,
    update_workflow_task_status,
)
from backend.agent.state import (
    ANALYZING,
    COMPLETED,
    FAILED,
    FAILURE_ANALYZING,
    RUNNING,
    STOPPED,
)


_react_active_runs: set[str] = set()
_react_stop_requests: set[str] = set()
_react_lock = threading.Lock()


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


# ============ 大脑轨迹（agent_execution_log） ============

def append_execution_log(
    task_id: str,
    *,
    trace_type: str,
    action: str = "",
    tool: str = "",
    params: Optional[dict] = None,
    result: Optional[dict] = None,
    status: str = "success",
) -> None:
    """写入一条 ReAct 轨迹（thought/action/observation/reflection）。"""
    max_no = db.execute(
        "SELECT COALESCE(MAX(step_no), 0) AS max_no FROM agent_execution_log WHERE agent_task_id=?",
        (task_id,),
        fetch=True,
    )
    step_no = int(max_no[0]["max_no"]) + 1 if max_no else 1
    db.execute(
        "INSERT INTO agent_execution_log("
        "agent_task_id, step_no, trace_type, action, tool, params, result, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            step_no,
            trace_type,
            str(action or "")[:2000],
            str(tool or ""),
            _json_dump(params or {}),
            _json_dump(result or {}),
            status,
            _now(),
        ),
    )
    try:
        from backend.agent.memory import remember
        remember(task_id, f"trace:{step_no}", {
            "trace_type": trace_type,
            "action": str(action or "")[:1000],
            "tool": tool,
            "result": result or {},
        })
    except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
        pass


def get_execution_log(task_id: str) -> list[dict]:
    """按执行顺序返回大脑轨迹。"""
    rows = db.execute(
        "SELECT * FROM agent_execution_log WHERE agent_task_id=? ORDER BY step_no, id",
        (task_id,),
        fetch=True,
    ) or []
    items = []
    for row in rows:
        item = db.to_dict(row)
        item["params"] = _json_load(item.get("params"))
        item["result"] = _json_load(item.get("result"))
        items.append(item)
    return items


def clear_execution_log(task_id: str) -> None:
    db.execute("DELETE FROM agent_execution_log WHERE agent_task_id=?", (task_id,))


def _active_plan_steps(task_id: str) -> list[dict]:
    task = get_workflow_task(task_id)
    if not task:
        return []
    plan = task.get("plan")
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict):
        return list(plan.get("steps") or [])
    return []


def get_react_timeline(task_id: str) -> list[dict]:
    """把 ReAct trace 映射成前端 Timeline 步骤。"""
    plan_steps = _active_plan_steps(task_id)
    trace = get_execution_log(task_id)
    by_action: dict[int, dict] = {}
    for item in trace:
        params = item.get("params") or {}
        action_index = params.get("_action_index")
        if action_index is None:
            continue
        try:
            action_index = int(action_index)
        except (TypeError, ValueError):
            continue
        current = by_action.setdefault(action_index, {"status": "PENDING", "output_payload": {}})
        if item.get("trace_type") == "action":
            current["status"] = "RUNNING"
            current["started_at"] = item.get("created_at")
        elif item.get("trace_type") == "observation":
            current["status"] = "SUCCESS" if item.get("status") in {"success", "skipped"} else "FAILED"
            current["finished_at"] = item.get("created_at")
            current["output_payload"] = item.get("result") or {}
        elif item.get("trace_type") == "reflection" and item.get("status") == "failed":
            current["status"] = "FAILED"
            current["error_message"] = item.get("action") or ""
    timeline = []
    for index, step in enumerate(plan_steps, 1):
        state = by_action.get(index - 1, {})
        timeline.append({
            "id": f"react-step-{index}",
            "step": index,
            "step_no": index,
            "phase": step.get("type") or "",
            "type": step.get("type") or "",
            "action": step.get("action") or step.get("tool") or "",
            "tool": step.get("tool") or "",
            "tool_name": step.get("tool") or "",
            "status": state.get("status") or "PENDING",
            "input_payload": step.get("payload") or {},
            "output_payload": state.get("output_payload") or {},
            "error_message": state.get("error_message") or "",
            "started_at": state.get("started_at") or "",
            "finished_at": state.get("finished_at") or "",
            "created_at": state.get("started_at") or "",
        })
    return timeline or get_workflow_timeline(task_id)


# ============ ReAct 运行控制 ============

def start_react_run(task_id: str, *, force: bool = False) -> dict:
    """在后台线程启动 ReAct 自主决策循环。"""
    task = get_workflow_task(task_id)
    if not task:
        raise ValueError("工作流任务不存在")
    with _react_lock:
        if task_id in _react_active_runs and not force:
            return {"task_id": task_id, "status": "already_running"}
        if force:
            clear_execution_log(task_id)
            db.execute("DELETE FROM test_execution_result WHERE agent_task_id=?", (task_id,))
            db.execute(
                "UPDATE agent_tasks SET error_message='', progress=0, finished_at=NULL, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
        _react_active_runs.add(task_id)
        _react_stop_requests.discard(task_id)
    threading.Thread(
        target=run_react_workflow,
        args=(task_id,),
        daemon=True,
        name=f"ai-agent-react-{task_id}",
    ).start()
    return {"task_id": task_id, "status": "started"}


def stop_react_run(task_id: str) -> dict:
    """请求停止 ReAct 循环。"""
    task = get_workflow_task(task_id)
    if not task:
        raise ValueError("工作流任务不存在")
    _react_stop_requests.add(task_id)
    update_workflow_task_status(task_id, STOPPED)
    with _react_lock:
        _react_active_runs.discard(task_id)
    return {"task_id": task_id, "status": "stopped"}


def _react_stop_requested(task_id: str) -> bool:
    return task_id in _react_stop_requests


# ============ ReAct 主循环 ============

def run_react_workflow(task_id: str) -> dict:
    try:
        task = get_workflow_task(task_id)
        if not task:
            return {"task_id": task_id, "status": "failed", "error": "任务不存在"}
        requirement = task.get("user_requirement") or ""
        project_id = task.get("project_id") or "default"

        # ---- Thought: 分析需求，生成计划 ----
        update_workflow_task_status(task_id, ANALYZING, progress=5)
        from backend.agent.core.planner import build_react_plan
        plan = build_react_plan(requirement, project_id=project_id)
        db.execute(
            "UPDATE agent_tasks SET plan=?, dimension_plan=?, updated_at=? WHERE id=?",
            (_json_dump(plan.get("steps")), _json_dump(plan.get("dimensions")), _now(), task_id),
        )
        append_execution_log(
            task_id, trace_type="thought",
            action=f"需求「{requirement}」需要验证，已拆解为 {len(plan.get('steps') or [])} 个执行动作",
        )

        queue = build_action_queue(requirement, plan)
        done: set[int] = set()
        skipped: set[int] = set()
        history: list[dict] = []
        failures: list[dict] = []
        latest_generated_count = 1
        total_actions = len(queue)
        action_progress_base = 10

        while True:
            if _react_stop_requested(task_id):
                append_execution_log(task_id, trace_type="reflection", action="收到停止请求，退出执行")
                break
            action = decide_next_action({
                "queue": queue,
                "done_indices": done,
                "skipped_indices": skipped,
            })
            if action is None:
                break
            index = int(action.get("index", -1))
            tool = str(action.get("tool") or "")
            params = dict(action.get("params") or {})
            params["_action_index"] = index

            # Thought
            thought = generate_thought(requirement, action, context=history)
            append_execution_log(task_id, trace_type="thought", action=thought, tool=tool)
            history.append({"trace_type": "thought", "action": thought, "tool": tool})

            # Action
            update_workflow_task_status(
                task_id, RUNNING,
                progress=min(90, action_progress_base + int(80 * (len(done) + len(skipped)) / max(total_actions, 1))),
            )
            if tool in {"generate_test_data", "generate_test_case", "run_web_test",
                        "run_app_test", "run_browser_agent", "run_security_test", "generate_from_api"}:
                params.setdefault("project_id", project_id)
            if tool in {"run_api_test", "run_web_test"}:
                params.setdefault("base_url", "")
            append_execution_log(
                task_id, trace_type="action", action=str(action.get("action") or tool),
                tool=tool, params=params,
            )
            tool_params = {key: value for key, value in params.items() if not key.startswith("_")}
            if tool == "run_web_test" and tool_params.get("case_files"):
                from backend.agent.tools import prepare_ui_data_binding
                binding_result = prepare_ui_data_binding(
                    project_id=project_id,
                    case_files=list(tool_params.get("case_files") or []),
                    agent_task_id=task_id,
                    count=latest_generated_count,
                )
                tool_params["data_bindings"] = binding_result.get("bindings") or []
                append_execution_log(
                    task_id,
                    trace_type="observation",
                    action=f"已为 {binding_result.get('count', 0)} 个 UI YAML 生成并绑定参数化测试数据",
                    tool="generate_test_data",
                    result=binding_result,
                    status=binding_result.get("status", "failed"),
                )
            result = execute_tool(tool, agent_task_id=task_id, step_id=index + 1, **tool_params)
            if tool == "generate_test_data" and result.get("status") == "success":
                latest_generated_count = max(1, int(result.get("count") or 1))
            if result.get("status") == "dispatched" and result.get("task_id"):
                ui_task_ids = [str(item) for item in (result.get("task_ids") or [result["task_id"]])]
                ui_timeout = int(__import__("os").environ.get("AI_AGENT_EXECUTION_TIMEOUT", "1800") or 1800)
                ui_tasks = [
                    _wait_ui_task(task_id, ui_task_id, timeout=ui_timeout)
                    for ui_task_id in ui_task_ids
                ]
                if any(item is None for item in ui_tasks):
                    result.update(
                        status="failed",
                        error=f"等待 UI 任务超过 {ui_timeout}s 未完成（worker 可能未消费或执行超时）",
                    )
                else:
                    ui_statuses = [str(item.get("status") or "failed").lower() for item in ui_tasks]
                    ui_status = "success" if all(item == "success" for item in ui_statuses) else "failed"
                    result.update(
                        status="success" if ui_status == "success" else "failed",
                        task_id=ui_task_ids[0],
                        task_ids=ui_task_ids,
                        ui_status=ui_status,
                        summary={
                            key: sum(int(item.get(key) or 0) for item in ui_tasks)
                            for key in ("total", "passed", "failed", "skipped")
                        },
                        report_url=ui_tasks[0].get("report_url") or "",
                        report_urls=[item.get("report_url") or "" for item in ui_tasks],
                        note="UI 自动化已执行完成（等待真实结果）",
                    )
                    if ui_status != "success":
                        result.update(_ui_failure_artifacts(ui_task_ids[0]))
                    if "stopped" in ui_statuses:
                        result["error"] = "UI 子任务已被停止，结果不完整"
                try:
                    _update_execution_result(
                        agent_task_id=task_id,
                        step_id=index + 1,
                        result=result,
                    )
                except Exception:  # noqa: BLE001
                    pass
            observation = observe(result)
            append_execution_log(
                task_id, trace_type="observation",
                action=observation_text(observation), tool=tool,
                result=result, status=observation.get("status", "failed"),
            )
            history.append({"trace_type": "observation", "action": observation_text(observation), "tool": tool})
            if result.get("status") in {"success", "dispatched", "ok"}:
                done.add(index)
                facts = observation.get("facts") or {}
                if facts.get("asset_id"):
                    remember_system_fact(project_id, "api", {
                        "tool": tool,
                        "asset_id": facts["asset_id"],
                        "count": facts.get("count") or facts.get("data_count"),
                    })
                continue

            if result.get("status") == "skipped":
                skipped.add(index)
                continue

            # ---- Reflection: 失败反思 ----
            reflection = reflect(
                task_id, observation, history,
                action_key=f"{tool}:{index}", requirement=requirement,
            )
            decision = reflection.get("decision", "adjust")
            append_execution_log(
                task_id, trace_type="reflection",
                action=f"{reflection.get('reason', '')}；建议：{reflection.get('suggestion', '')}",
                tool=tool, status="failed",
            )
            history.append({
                "trace_type": "reflection",
                "action": reflection.get("reason", ""),
                "tool": tool,
                "decision": reflection.get("decision"),
                "action_key": f"{tool}:{index}",
            })
            failures.append({
                "tool": tool,
                "action": str(action.get("action") or ""),
                "result": result,
                "reflection": reflection,
            })
            if decision == "retry":
                continue  # 同一步骤重新执行
            if decision == "adjust":
                skipped.add(index)
                continue
            done.add(index)
            if decision == "finish":
                break

        # ---- 失败分析 ----
        failed_count = sum(1 for r in get_execution_results(task_id) if r.get("status") == "failed")
        if failures or failed_count:
            update_workflow_task_status(task_id, FAILURE_ANALYZING, progress=92)
            append_execution_log(
                task_id, trace_type="thought",
                action=f"发现 {max(failed_count, len(failures))} 条失败，调用失败分析定位根因",
            )
            from backend.agent.analyzer import analyze_task_failures
            analyses = analyze_task_failures(task_id)
            append_execution_log(
                task_id, trace_type="observation",
                action=f"失败分析完成：{len(analyses)} 条结论",
                result={"analyses": analyses},
                status="success",
            )
            for analysis in analyses[:5]:
                remember_system_fact(project_id, "failure", {
                    "reason": analysis.get("reason", ""),
                    "level": analysis.get("level", ""),
                    "suggestion": analysis.get("suggestion", ""),
                })

        # ---- 报告 ----
        update_workflow_task_status(task_id, ANALYZING, progress=97)
        append_execution_log(task_id, trace_type="thought", action="汇总执行结果，生成测试报告")
        results = [
            {
                "tool": r.get("tool"),
                "target": r.get("target"),
                "status": r.get("status"),
                "summary": r.get("summary"),
                "error": (r.get("failure_analysis") or {}).get("reason") or "",
                "suggestion": (r.get("failure_analysis") or {}).get("suggestion") or "",
                "failure_details": (r.get("failure_analysis") or {}).get("failure_details") or [],
                "execution_steps": (r.get("failure_analysis") or {}).get("execution_steps") or [],
                "step_counts": (r.get("failure_analysis") or {}).get("step_counts") or {},
            }
            for r in get_execution_results(task_id)
        ]
        report_result = execute_tool(
            "create_report",
            agent_task_id=task_id,
            step_id=0,
            title=f"AI Agent ReAct 测试报告 - {requirement[:40]}",
            results=results,
        )
        append_execution_log(
            task_id, trace_type="observation",
            action="测试报告已生成", tool="create_report",
            result=report_result,
            status=report_result.get("status", "failed"),
        )
        if _react_stop_requested(task_id):
            update_workflow_task_status(task_id, STOPPED, progress=100)
        elif failures:
            update_workflow_task_status(
                task_id, FAILED, progress=100,
                error_message=f"ReAct 执行完成，{len(failures)} 条动作失败，详见报告",
            )
        else:
            update_workflow_task_status(task_id, COMPLETED, progress=100)
        return {"task_id": task_id, "status": "completed", "actions": total_actions, "failures": len(failures)}
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务落库为 FAILED
        try:
            update_workflow_task_status(task_id, FAILED, progress=100, error_message=f"ReAct 执行异常: {exc}")
            append_execution_log(task_id, trace_type="reflection", action=f"ReAct 执行异常: {exc}", status="failed")
        except Exception:  # noqa: BLE001
            pass
        return {"task_id": task_id, "status": "failed", "error": str(exc)}
    finally:
        with _react_lock:
            _react_active_runs.discard(task_id)
            _react_stop_requests.discard(task_id)
