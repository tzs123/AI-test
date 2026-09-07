"""AI Agent 核心 / 数据智能体 / 失败分析 / 安全智能体 / 记忆 单元测试。"""
import json
import os
import sys
import threading
import time
import http.server

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import agent, db, settings


REGISTER_DOC = {
    "openapi": "3.0.0",
    "paths": {
        "/user/register": {
            "post": {
                "summary": "用户注册",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["username", "phone", "password", "email", "age"],
                                "properties": {
                                    "username": {"type": "string"},
                                    "phone": {"type": "string"},
                                    "password": {"type": "string"},
                                    "email": {"type": "string", "format": "email"},
                                    "age": {"type": "integer"},
                                },
                            }
                        }
                    }
                },
            }
        }
    },
}


def _temporary_database(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "platform.db"))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", True)
    db.init_db()


def _wait_status(task_id, terminal, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = agent.get_workflow_task(task_id)
        if task and task["status"] in terminal:
            return task
        time.sleep(0.2)
    return agent.get_workflow_task(task_id)


def _wait_trace(task_id, timeout=5):
    from backend.agent.core.executor import get_execution_log

    deadline = time.time() + timeout
    while time.time() < deadline:
        trace = get_execution_log(task_id)
        if trace:
            return trace
        time.sleep(0.1)
    return get_execution_log(task_id)


# ===== 工具注册表（Tool Registry） =====

def test_tool_registry_contains_unified_tools():
    from backend.agent.core.tool_registry import get_tool, list_tools

    names = {t["name"] for t in list_tools()}
    for required in (
        "generate_test_data", "generate_test_case", "run_api_test", "run_web_test",
        "run_app_test", "run_security_test", "run_browser_agent", "analyze_failure",
        "generate_from_api", "create_report",
    ):
        assert required in names, f"缺少工具 {required}"
    for removed in ("generate_data", "create_case", "run_ui_test"):
        assert removed not in names, f"旧工具 {removed} 不应继续暴露"
    tool = get_tool("analyze_failure")
    assert tool.execute(logs="NoSuchElement: 登录按钮")["status"] in {"success", "failed"}


def test_execute_tool_records_result(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    from backend.agent.core.tool_registry import execute_tool

    result = execute_tool(
        "generate_test_data", asset_type="USER", count=5,
        fields=["username", "phone"], agent_task_id="task-x", project_id="default",
    )
    assert result["status"] == "success"
    assert result["count"] == 5
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM test_execution_result WHERE agent_task_id='task-x'",
        fetch=True,
    )
    assert rows[0]["n"] == 1


# ===== ReAct 核心 =====

def test_decision_engine_queue_and_observer():
    from backend.agent.core.decision_engine import build_action_queue, decide_next_action
    from backend.agent.core.observer import observe

    queue = build_action_queue("测试登录功能")
    assert len(queue) >= 2
    assert queue[0]["tool"] == "generate_test_data"
    small = [{"index": 0, "tool": "a"}, {"index": 1, "tool": "b"}, {"index": 2, "tool": "c"}]
    assert decide_next_action({"queue": small, "done_indices": [0], "skipped_indices": []})["index"] == 1
    assert decide_next_action({"queue": small, "done_indices": {0, 1}, "skipped_indices": {2}}) is None

    observation = observe({"status": "success", "asset_id": "data_1", "count": 10})
    assert observation["status"] == "success"
    assert observation["facts"]["asset_id"] == "data_1"


def test_react_plan_runs_discovered_cases_with_ui_executor(monkeypatch):
    from backend.agent.core import planner as react_planner

    monkeypatch.setattr(
        react_planner,
        "discover_cases",
        lambda *_args, **_kwargs: [{
            "name": "loan_apply.yaml",
            "title": "贷款申请全流程",
            "base_url": "http://example.test/home",
        }],
    )
    monkeypatch.setattr(
        react_planner,
        "build_workflow_plan",
        lambda *_args, **_kwargs: {
            "steps": [
                {
                    "step": 1,
                    "type": "ui",
                    "tool": "run_web_test",
                    "action": "执行需求相关 UI 自动化测试",
                    "payload": {},
                },
                {
                    "step": 2,
                    "type": "report",
                    "tool": "create_report",
                    "action": "生成测试报告",
                    "payload": {},
                },
            ],
            "dimensions": {},
        },
    )

    plan = react_planner.build_react_plan("测试额度申请", project_id="default")
    real_case_steps = [
        step for step in plan["steps"]
        if "loan_apply.yaml" in json.dumps(step, ensure_ascii=False)
    ]

    assert real_case_steps
    assert all(step["tool"] == "run_web_test" for step in real_case_steps)
    assert all(step["type"] == "ui" for step in real_case_steps)
    assert not any(
        step.get("tool") == "run_browser_agent"
        and (step.get("payload") or {}).get("case_file") == "loan_apply.yaml"
        for step in plan["steps"]
    )


def test_reflection_decides_retry_then_adjust():
    from backend.agent.core.reflection import reflect

    history = []
    first = reflect("t1", {"status": "failed", "summary": "接口超时"}, history, action_key="run_api_test:0")
    assert first["decision"] == "retry"
    history.append(first)
    second = reflect("t1", {"status": "failed", "summary": "接口超时"}, history, action_key="run_api_test:0")
    assert second["decision"] == "retry"
    history.append(second)
    third = reflect("t1", {"status": "failed", "summary": "接口超时"}, history, action_key="run_api_test:0")
    assert third["decision"] in {"adjust", "finish"}


def test_failure_analyzer_identifies_unresolved_test_data_variable():
    from backend.agent.analyzer import analyze_failure

    result = analyze_failure(
        logs="AssertionError: 输入值未稳定保存: 期望 '${VERIFICATION_CODE}'，实际 ''",
        context="工具=run_web_test 目标=验证码输入",
    )
    assert result["category"] == "测试数据变量未解析"
    assert result["level"] == "HIGH"
    assert "VERIFICATION_CODE" in result["suggestion"]


def test_react_workflow_runs_to_terminal(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    from backend.agent import tools as agent_tools
    from backend.agent import planner as workflow_planner
    from backend.agent.core import planner as react_planner
    from backend.agent.core.executor import get_execution_log, start_react_run

    monkeypatch.setattr(workflow_planner, "is_configured", lambda: False)
    monkeypatch.setattr(react_planner, "build_react_plan", lambda *_args, **_kwargs: {
        "steps": [
            {
                "step": 1,
                "type": "data",
                "tool": "generate_test_data",
                "action": "生成测试数据",
                "payload": {"asset_type": "USER", "count": 1, "fields": ["username", "phone"]},
            },
            {
                "step": 2,
                "type": "ui",
                "tool": "run_web_test",
                "action": "执行 UI 用例",
                "payload": {},
            },
        ],
        "dimensions": {},
    })

    # 单测环境不真正派发 UI 执行任务，避免阻塞
    def _stub_ui_test(**kwargs):
        return {"status": "skipped", "message": "单测环境不执行 UI 任务"}
    monkeypatch.setitem(agent_tools.TOOLS["run_ui_test"], "handler", _stub_ui_test)
    monkeypatch.setitem(agent_tools.TOOLS["run_web_test"], "handler", _stub_ui_test)

    task = agent.create_agent_workflow_task(
        task_name="react-test",
        user_requirement="测试登录功能",
        project_id="default",
        auto_execute=False,
        start=False,
    )
    start_react_run(task["id"])
    finished = _wait_status(task["id"], {"COMPLETED", "FAILED", "STOPPED"})
    assert finished is not None
    trace = _wait_trace(task["id"])
    trace_types = {item["trace_type"] for item in trace}
    assert trace_types >= {"thought", "action", "observation"}
    assert finished["status"] in {"COMPLETED", "FAILED"}
    # 报告应已生成
    report_rows = db.execute(
        "SELECT COUNT(*) AS n FROM test_execution_result WHERE agent_task_id=? AND tool='create_report'",
        (task["id"],), fetch=True,
    )
    assert report_rows[0]["n"] >= 1


# ===== 数据智能体（Data Agent） =====

def test_data_agent_generate_from_api(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    from backend.data_agent import generate_from_api

    result = generate_from_api(
        swagger_doc=REGISTER_DOC,
        api="POST /user/register",
        mode=["normal", "boundary", "security"],
        count=50,
        project_id="default",
    )
    assert result["asset_id"].startswith("api_data_")
    assert result["data_count"] > 0
    assert result["method"] == "POST"
    assert result["api"] == "/user/register"
    case_types = {c["case_type"] for c in result["cases"]}
    assert "normal" in case_types and "boundary" in case_types and "security" in case_types
    # 边界用例应包含 age 的负数/0
    boundary_params = {c["parameter"] for c in result["cases"] if c["case_type"] == "boundary"}
    assert "age" in boundary_params
    # 资产落库
    rows = db.execute("SELECT COUNT(*) AS n FROM test_assets WHERE id=?", (result["asset_id"],), fetch=True)
    assert rows[0]["n"] == 1


def test_agent_generated_data_syncs_to_test_data_center(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setenv("TEST_DATA_CENTER_API_URL", "http://testhub-backend:8000")
    monkeypatch.setenv("TEST_DATA_CENTER_AGENT_TOKEN", "runnergo-local-agent-token")

    published = {}

    class _FakeResponse:
        status_code = 201

        def json(self):
            return {"id": 42, "tags": ["ai-generated"]}

    def _fake_post(url, json=None, headers=None, timeout=None):
        published["url"] = url
        published["payload"] = json
        published["token"] = (headers or {}).get("X-Agent-Token")
        return _FakeResponse()

    monkeypatch.setattr("backend.agent.tools.requests.post", _fake_post)

    from backend.agent.tools import generate_test_data

    result = generate_test_data(
        asset_type="USER",
        count=2,
        fields=["username", "phone"],
        project_id="default",
    )

    assert result["status"] == "success"
    data_center_asset = result["data_center_asset"]
    assert data_center_asset["synced"] is True
    assert data_center_asset["asset_id"] == 42
    assert published["url"].endswith("/api/core/test-data-assets/agent-publish/")
    assert published["token"] == "runnergo-local-agent-token"
    assert len(published["payload"]["rows"]) == 2
    # API 成功后不得再跨容器直写共享 SQLite 文件
    assert not (tmp_path / "testhub.sqlite3").exists()


def test_agent_data_sync_fails_without_api(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.delenv("TEST_DATA_CENTER_API_URL", raising=False)
    monkeypatch.delenv("TEST_DATA_ASSETS_DB_PATH", raising=False)

    from backend.agent.tools import _sync_rows_to_test_data_center

    result = _sync_rows_to_test_data_center(asset_type="USER", name="x", rows=[{"a": 1}])
    assert result["synced"] is False
    assert result["reason"] == "api_url_missing"


def test_ui_agent_prepares_parameterized_yaml_binding(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    case_path = tmp_path / "login.yaml"
    case_path.write_text(
        """
steps:
  - id: phone-primary
    type: input
    name: 输入手机号
  - id: phone-confirm
    type: input
    name: 再次输入手机号
  - id: password
    type: input
    name: 输入密码
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.agent.tools._ui_case_path", lambda *_args: str(case_path))
    captured = {}

    def fake_generate_test_data(**kwargs):
        captured.update(kwargs)
        return {
            "status": "success",
            "count": kwargs["count"],
            "data_center_asset": {"synced": True, "asset_id": 101, "requirement_id": 202},
        }

    monkeypatch.setattr("backend.agent.tools.generate_test_data", fake_generate_test_data)
    from backend.agent.tools import prepare_ui_data_binding

    result = prepare_ui_data_binding(
        project_id="default",
        case_files=["login.yaml"],
        agent_task_id="task-7",
        count=3,
    )

    binding = result["bindings"][0]
    assert captured["count"] == 3
    assert captured["fields"] == ["phone", "phone_2", "password"]
    assert captured["binding"]["target_type"] == "ui_automation"
    assert captured["binding"]["target_case_id"] == binding["target_case_id"]
    assert binding["runtime_override"] == [
        {
            "stepId": "phone-primary",
            "stepPath": "steps.0",
            "runtimeValue": "${dataAssets.agent_task_7_1.phone}",
        },
        {
            "stepId": "phone-confirm",
            "stepPath": "steps.1",
            "runtimeValue": "${dataAssets.agent_task_7_1.phone_2}",
        },
        {
            "stepId": "password",
            "stepPath": "steps.2",
            "runtimeValue": "${dataAssets.agent_task_7_1.password}",
        },
    ]


def test_ui_agent_keeps_yaml_value_when_reused_asset_lacks_semantic_field(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    case_path = tmp_path / "login.yaml"
    case_path.write_text(
        """
steps:
  - id: phone
    type: input
    name: 输入手机
    value: ${dataAssets.generatedData.phone}
  - id: verification-code
    type: input
    name: 输入验证码
    value: ${dataAssets.generatedData.code}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.agent.tools._ui_case_path", lambda *_args: str(case_path))
    from backend.agent.tools import prepare_ui_data_binding

    result = prepare_ui_data_binding(
        project_id="default",
        case_files=["login.yaml"],
        agent_task_id="task-18",
        count=3,
        existing_bindings=[{
            "target_id": "login.yaml",
            "alias": "agent_ui_yaml_18_1",
            "asset_id": 101,
            "fields": ["phone", "name"],
        }],
    )

    binding = result["bindings"][0]
    assert binding["fields"] == ["phone"]
    assert binding["runtime_override"] == [{
        "stepId": "phone",
        "stepPath": "steps.0",
        "runtimeValue": "${dataAssets.agent_ui_yaml_18_1.phone}",
    }]
    assert all(item["stepId"] != "verification-code" for item in binding["runtime_override"])


def test_agent_generated_data_uses_base_generator_for_duplicate_fields(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "backend.agent.tools._sync_rows_to_test_data_center",
        lambda **_kwargs: {"synced": True, "asset_id": 1},
    )
    from backend.agent.tools import generate_test_data

    result = generate_test_data(
        asset_type="CUSTOM",
        count=2,
        fields=["phone", "phone_2", "password_3"],
    )
    asset = db.execute(
        "SELECT payload FROM test_assets WHERE id=?",
        (result["asset_id"],),
        fetch=True,
    )[0]
    rows = json.loads(asset["payload"])["rows"]

    assert all(row["phone"].startswith("1") and len(row["phone"]) == 11 for row in rows)
    assert all(row["phone_2"].startswith("1") and len(row["phone_2"]) == 11 for row in rows)
    assert all(row["password_3"].startswith("Passw0rd@") for row in rows)


def test_run_ui_test_keeps_unbound_cases_in_mixed_batch(monkeypatch):
    from backend.agent.tools import run_ui_test

    created = []
    dispatched = []

    def fake_create_task(**kwargs):
        created.append(kwargs)
        return f"task-{len(created)}"

    monkeypatch.setattr("backend.executor.create_task", fake_create_task)
    monkeypatch.setattr("backend.executor.dispatch", lambda task_id: dispatched.append(task_id) or False)
    monkeypatch.setattr("backend.agent.tools._ui_case_path", lambda *_args: "/tmp/login.yaml")

    result = run_ui_test(
        project_id="default",
        case_files=["login.yaml", "health.yaml"],
        data_bindings=[{
            "case_file": "login.yaml",
            "runtime_override": [{"stepId": "username", "runtimeValue": "${dataAssets.login.username}"}],
            "runtime_variables": {"USER": "${dataAssets.login.username}"},
        }],
    )

    assert result["task_ids"] == ["task-1", "task-2"]
    assert [item["case_files"] for item in created] == [["login.yaml"], ["health.yaml"]]
    assert created[0]["runtime_override"][0]["stepId"] == "username"
    assert created[1]["runtime_override"] == []
    assert dispatched == ["task-1", "task-2"]


def test_run_ui_test_passes_requested_parameter_row_limit(monkeypatch):
    from backend.agent.tools import run_ui_test
    from backend.executor import PARAMETER_ROW_LIMIT_VARIABLE

    created = []
    monkeypatch.setattr(
        "backend.executor.create_task",
        lambda **kwargs: created.append(kwargs) or "task-1",
    )
    monkeypatch.setattr("backend.executor.dispatch", lambda _task_id: False)
    monkeypatch.setattr("backend.agent.tools._ui_case_path", lambda *_args: "/tmp/login.yaml")

    run_ui_test(
        project_id="default",
        case_files=["login.yaml"],
        runtime_variables={"VISIBLE": "yes"},
        test_data_count=3,
    )

    assert created[0]["runtime_variables"] == {
        "VISIBLE": "yes",
        PARAMETER_ROW_LIMIT_VARIABLE: 3,
    }


def test_data_agent_auto_selects_first_api_when_missing():
    from backend.data_agent import generate_from_api

    result = generate_from_api(swagger_doc=REGISTER_DOC, api="")
    assert result["status"] == "success"
    assert result["asset_id"].startswith("api_data_")
    assert result["api"] == "/user/register"


# ===== 失败分析（Failure Analyzer） =====

def test_analyzer_solve_and_autofix():
    from backend.analyzer import auto_fix, solve

    result = solve(logs="Timeout waiting for element: 登录按钮")
    assert result["error_type"] in {"TIMEOUT", "ELEMENT_CHANGED", "NETWORK_ERROR"}
    assert result["solution"]
    fix = auto_fix({"error_type": "ELEMENT_CHANGED", "reason": "按钮DOM变化", "solution": "更新定位"})
    assert fix["error_type"] == "ELEMENT_CHANGED"
    assert fix["auto_fixable"] is True
    assert fix["patch"]


def test_analyzer_batch():
    from backend.analyzer import analyze_batch

    results = analyze_batch([
        {"logs": "NoSuchElement", "context": "登录页"},
        {"logs": "500 Internal Server Error", "api_response": "500"},
    ])
    assert len(results) == 2
    assert all("error_type" in item for item in results)


# ===== 安全智能体（Security Agent） =====

class _VulnHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if "OR" in self.path or "DROP" in self.path or "UNION" in self.path:
            body = b"SQL syntax error near '1'"
        elif "etc" in self.path or "passwd" in self.path:
            body = b"root:x:0:0:root:/root:/bin/bash"
        else:
            body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def vuln_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _VulnHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_security_agent_scan(monkeypatch, tmp_path, vuln_server):
    _temporary_database(monkeypatch, tmp_path)
    from backend.security_agent import scan

    result = scan(api_doc="", target=f"{vuln_server}/api/user?id=1&name=test")
    assert result["status"] == "success"
    assert result["finding_count"] >= 1
    assert result["risk"] == "HIGH"
    assert result["type"] == "SQL Injection"
    assert result["parameter"] == "id"


# ===== 项目级记忆 =====

def test_project_memory(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    from backend.agent.core.memory import (
        list_project_memory,
        recall_project,
        remember_project,
        remember_system_fact,
    )

    remember_project("proj-a", "env", {"base_url": "https://test.example.com"})
    assert recall_project("proj-a", "env") == {"base_url": "https://test.example.com"}
    remember_system_fact("proj-a", "api", {"path": "/user/login"})
    remember_system_fact("proj-a", "api", {"path": "/cart/add"})
    facts = recall_project("proj-a", "fact:api")
    assert isinstance(facts, list) and len(facts) == 2
    assert len(list_project_memory("proj-a")) == 2
    # 项目记忆与任务记忆隔离
    assert len(agent.list_memory("proj-a")) == 0


# ===== 浏览器视觉 Agent（YAML 用例执行） =====

def test_case_runner_loads_yaml_and_locators(tmp_path):
    from backend.browser_agent.case_runner import _locators_of, load_case

    case_yaml = tmp_path / "flow.yaml"
    case_yaml.write_text(
        "base_url: http://example.com\n"
        "steps:\n"
        "- id: s1\n  action: goto\n  url: http://example.com\n"
        "- id: s2\n  action: fill\n  name: 输入 手机\n  value: '13800138000'\n"
        "  element:\n    role: textbox\n    accessible_name: 手机\n"
        "    locators:\n    - {strategy: placeholder, value: 请输入手机号}\n    - {strategy: id, value: van-field-1-input}\n",
        encoding="utf-8",
    )
    case = load_case(str(case_yaml))
    assert case["base_url"] == "http://example.com"
    assert len(case["steps"]) == 2
    info = _locators_of(case["steps"][1]["element"])
    assert info["role"] == "textbox"
    assert info["name"] == "手机"
    assert info["placeholder"] == "请输入手机号"
    assert info["element_id"] == "van-field-1-input"


def test_case_runner_requires_case_file():
    from backend.browser_agent.case_runner import load_case

    with pytest.raises(ValueError):
        load_case("not_exist_case.yaml")


def test_browser_agent_accepts_case_file(monkeypatch, tmp_path):
    from backend.browser_agent import run_browser_agent
    from backend.browser_agent import case_runner

    def _fake_run_case(**kwargs):
        assert kwargs["case_file"] == "demo.yaml"
        return {"status": "success", "case_file": "demo.yaml", "passed": 1, "failed": 0,
                "trace": [{"step_no": 1, "status": "success"}], "screenshots": []}

    monkeypatch.setattr(case_runner, "run_browser_case", _fake_run_case)
    result = run_browser_agent(url="http://example.com", goal="测试", case_file="demo.yaml")
    assert result["status"] == "success"
