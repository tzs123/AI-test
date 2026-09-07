import json
import os
import sys

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import agent, db, settings


ASSETS = [
    {
        "filename": "test_login.yaml",
        "name": "用户登录流程",
        "description": "验证账号密码登录",
        "base_url": "https://example.com",
        "flow_version": 2,
        "step_count": 4,
        "step_names": ["打开登录页", "输入账号", "输入密码", "点击登录"],
    },
    {
        "filename": "test_cart.yaml",
        "name": "购物车流程",
        "description": "搜索商品并加入购物车",
        "base_url": "https://example.com",
        "flow_version": 2,
        "step_count": 5,
        "step_names": ["搜索商品", "打开详情", "加入购物车"],
    },
]


def _temporary_database(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "platform.db"))
    db.init_db()


def _insert_element(*, element_id, name, tag, role, fingerprint, page_url="https://example.com/login"):
    db.execute(
        "INSERT INTO web_elements(id,project_id,name,page_url,tag_name,element_role,signature,"
        "locators,fingerprint,element_context,usage_count,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            element_id, "default", name, page_url, tag, role, f"sig-{element_id}",
            json.dumps([{"strategy": "id", "value": element_id}]),
            json.dumps(fingerprint, ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            3, agent._now(), agent._now(),
        ),
    )


def test_ai_plan_only_accepts_existing_asset_names(monkeypatch):
    monkeypatch.setattr(
        agent,
        "_call_model",
        lambda *_args, **_kwargs: """{
          "title": "登录测试",
          "summary": "覆盖登录主流程",
          "risk_level": "high",
          "risks": ["认证失败"],
          "scenarios": [{"id":"SC-001","name":"登录","objective":"登录","priority":"P0","case_file":"invented.yaml","executable":true}],
          "selected_case_files": ["invented.yaml", "test_login.yaml"],
          "test_data": ["普通账号"],
          "execution_strategy": "先主流程",
          "coverage_gaps": []
        }""",
    )

    plan = agent.build_test_plan("测试用户登录", ASSETS)

    assert plan["planner_source"] == "ai"
    assert plan["selected_case_files"] == ["test_login.yaml"]
    assert "invented.yaml" not in plan["selected_case_files"]


def test_rule_planner_selects_semantically_related_case(monkeypatch):
    monkeypatch.setattr(agent, "_call_model", lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")))

    plan = agent.build_test_plan("验证搜索商品并加入购物车", ASSETS)

    assert plan["planner_source"] == "rules"
    assert plan["selected_case_files"][0] == "test_cart.yaml"
    assert plan["scenarios"][0]["executable"] is True


def test_model_selected_file_must_be_related_to_goal(monkeypatch):
    monkeypatch.setattr(
        agent,
        "_call_model",
        lambda *_args, **_kwargs: '{"selected_case_files":["test_login.yaml"],"r":"L"}',
    )

    plan = agent.build_test_plan("验证搜索商品并加入购物车", ASSETS)

    assert plan["planner_source"] == "ai"
    assert plan["selected_case_files"] == ["test_cart.yaml"]


def test_model_cannot_route_unmatched_goal_to_first_asset(monkeypatch):
    monkeypatch.setattr(agent, "_call_model", lambda *_args, **_kwargs: '{"i":[0],"r":"L"}')

    plan = agent.build_test_plan("验证导出报表审批流", ASSETS)

    assert plan["selected_case_files"] == []
    assert plan["coverage_gaps"]


def test_goal_terms_keep_business_words_containing_polite_particle():
    assert "申请" in agent._goal_terms("验证完整申请流程")


def test_agent_completes_plan_execute_analyse_loop(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo"})
    monkeypatch.setattr(agent, "_collect_case_assets", lambda _project_id: [ASSETS[0]])
    monkeypatch.setattr(agent, "_call_model", lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")))

    real_create_task = agent.executor.create_task

    def dispatch(execution_task_id):
        db.execute(
            "UPDATE tasks SET status='success',passed=1,failed=0,total=1,finished_at=? WHERE id=?",
            (agent._now(), execution_task_id),
        )
        return True

    monkeypatch.setattr(agent.executor, "create_task", real_create_task)
    monkeypatch.setattr(agent.executor, "dispatch", dispatch)
    monkeypatch.setattr(
        agent.analysis,
        "analyze_task",
        lambda execution_task_id: {
            "task_id": execution_task_id,
            "summary": {"total": 1, "passed": 1, "failed": 0, "broken": 0, "skipped": 0},
            "allure_report_url": f"/report/tasks/{execution_task_id}/report/",
            "failures": [],
            "attribution_summary": {},
            "top_suggestion": "",
        },
    )

    task = agent.create_agent_task(
        project_id="default",
        goal="验证用户登录流程",
        auto_execute=True,
        start=False,
    )
    agent._run_agent_task(task["id"])

    completed = agent.get_agent_task(task["id"])
    events = agent.list_agent_events(task["id"])
    assert completed["status"] == "completed"
    assert completed["progress"] == 100
    assert completed["plan"]["selected_case_files"] == ["test_login.yaml"]
    assert completed["result_summary"]["test_status"] == "passed"
    assert {event["phase"] for event in events} >= {"created", "planning", "execution", "analysis", "completed"}


def test_force_execute_refreshes_plan_from_latest_ui_assets(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        agent.project_service,
        "get_project",
        lambda project_id: {"id": project_id, "name": "Demo"},
    )
    latest_asset = {
        **ASSETS[1],
        "mtime": 1784812345,
        "content_sha256": "a" * 64,
    }
    monkeypatch.setattr(agent, "_collect_case_assets", lambda _project_id: [latest_asset])
    monkeypatch.setattr(
        agent,
        "_call_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    captured = []
    monkeypatch.setattr(agent, "_execute_plan", lambda task: captured.append(task))

    task = agent.create_agent_task(
        project_id="default",
        goal="验证搜索商品并加入购物车",
        auto_execute=False,
        start=False,
    )
    agent._update_task(
        task["id"],
        status="ready",
        plan={"selected_case_files": ["test_login.yaml"]},
    )

    agent._run_agent_task(task["id"], force_execute=True)

    assert captured
    refreshed = captured[0]
    assert refreshed["plan"]["selected_case_files"] == ["test_cart.yaml"]
    assert refreshed["plan"]["asset_snapshot"] == [{
        "filename": "test_cart.yaml",
        "mtime": 1784812345,
        "content_sha256": "a" * 64,
        "step_count": 5,
    }]
    assert agent.list_agent_events(task["id"])[-1]["payload"]["fresh_content"] is True


def test_failure_report_links_existing_defect():
    result = {
        "summary": {"total": 1, "passed": 0, "failed": 1, "skipped": 0},
        "allure_report_url": "/report/tasks/exec001/report/",
        "attribution_summary": {"被测系统缺陷": 1},
        "top_suggestion": "检查登录接口",
        "failures": [{
            "name": "test_login",
            "tc_id": "TC-LOGIN-01",
            "scenario": "登录失败",
            "error_message": "点击登录后返回 500",
            "attribution": {"category": "被测系统缺陷", "suggestion": "检查登录接口"},
        }],
    }

    summary, failure, draft = agent.build_failure_report(
        "exec001",
        result,
        [{"id": "bug001", "tc_id": "TC-LOGIN-01"}],
    )

    assert summary["test_status"] == "failed"
    assert failure["release_risk"] == "high"
    assert draft["items"][0]["linked_defect_id"] == "bug001"


def test_generate_flow_draft_from_captured_elements(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    _insert_element(
        element_id="username", name="用户名", tag="input", role="textbox",
        fingerprint={"tag": "input", "role": "textbox", "accessible_name": "用户名", "attrs": {"id": "username", "type": "text"}},
    )
    _insert_element(
        element_id="password", name="密码", tag="input", role="textbox",
        fingerprint={"tag": "input", "role": "textbox", "accessible_name": "密码", "attrs": {"id": "password", "type": "password"}},
    )
    _insert_element(
        element_id="login", name="登录", tag="button", role="button",
        fingerprint={"tag": "button", "role": "button", "accessible_name": "登录", "attrs": {"id": "login", "type": "submit"}},
    )
    monkeypatch.setattr(agent, "_call_model", lambda *_args, **_kwargs: '{"s":[0,1,2]}')

    draft = agent.generate_flow_draft("验证用户登录", "default", "https://example.com/login")

    assert draft["generator_source"] == "ai"
    assert [step["action"] for step in draft["flow"]["steps"]] == ["goto", "fill", "fill", "click"]
    assert "USERNAME" in draft["required_variables"]
    assert "PASSWORD" in draft["required_variables"]
    assert draft["flow"]["metadata"]["review_required"] is True


def test_generate_flow_draft_deduplicates_semantic_element_copies(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    for suffix, field_id in (("a", "van-field-1-input"), ("b", "van-field-5-input"), ("c", "van-field-9-input")):
        _insert_element(
            element_id=f"phone-{suffix}",
            name=f"手机_{suffix}",
            tag="input",
            role="textbox",
            fingerprint={
                "tag": "input",
                "role": "textbox",
                "accessible_name": "手机",
                "attrs": {"id": field_id, "name": "phone", "type": "tel"},
            },
        )
        _insert_element(
            element_id=f"code-{suffix}",
            name=f"验证码_{suffix}",
            tag="input",
            role="textbox",
            fingerprint={
                "tag": "input",
                "role": "textbox",
                "accessible_name": "验证码",
                "attrs": {"id": field_id.replace("field", "code"), "name": "password", "type": "text"},
            },
        )
    for suffix in ("a", "b"):
        _insert_element(
            element_id=f"agreement-{suffix}",
            name=f"复选框 本人已阅读并同意签署隐私政策_{suffix}",
            tag="div",
            role="checkbox",
            fingerprint={
                "tag": "div",
                "role": "checkbox",
                "accessible_name": "本人已阅读并同意签署隐私政策",
                "text": "本人已阅读并同意签署隐私政策",
                "attrs": {"id": "", "type": ""},
            },
        )
    _insert_element(
        element_id="phone-label",
        name="手机",
        tag="div",
        role="",
        fingerprint={
            "tag": "div",
            "role": "",
            "accessible_name": "手机",
            "text": "手机",
            "attrs": {"id": "", "type": ""},
        },
    )
    monkeypatch.setattr(agent, "_call_model", lambda *_args, **_kwargs: '{"s":[0,1,2,3,4,5,6,7]}')

    draft = agent.generate_flow_draft("验证手机验证码登录并勾选协议", "default", "https://example.com/login")
    names = [step["name"] for step in draft["flow"]["steps"]]

    assert sum("手机" in name for name in names) == 1
    assert sum("验证码" in name for name in names) == 1
    assert sum("同意签署隐私政策" in name for name in names) == 1
    assert len(draft["flow"]["steps"]) == 4
    assert [step.get("value") for step in draft["flow"]["steps"] if step["action"] == "fill"] == [
        "13800138000",
        "${VERIFICATION_CODE}",
    ]
    assert draft["required_variables"] == ["VERIFICATION_CODE"]


def test_generated_element_dedupe_collapses_generic_fields_across_frames():
    def field(name, *, page_url="https://example.com/home", frame_path=None, attrs=None):
        return {
            "id": f"{name}-{page_url}",
            "name": name,
            "page_url": page_url,
            "tag_name": "input",
            "element_role": "textbox",
            "fingerprint": {
                "tag": "input",
                "role": "textbox",
                "accessible_name": name,
                "attrs": attrs or {"type": "text"},
            },
            "element_context": {"frame_path": frame_path or []},
        }

    elements, removed = agent._dedupe_generated_elements([
        field("手机", page_url="https://example.com/home"),
        field("手机", page_url="https://example.com/fill", frame_path=["iframe-a"]),
        field("验证码", page_url="https://example.com/home"),
        field("验证码", page_url="https://example.com/fill", frame_path=["iframe-b"]),
        field("联系人1手机号", page_url="https://example.com/fill"),
        field("联系人2手机号", page_url="https://example.com/fill"),
    ])

    names = [item["name"] for item in elements]
    assert removed == 2
    assert names == ["手机", "验证码", "联系人1手机号", "联系人2手机号"]


def test_save_generated_flow_cleans_legacy_duplicate_steps(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo", "case_dir": "cases"})
    task = agent.create_agent_task(project_id="default", goal="测试额度申请", auto_execute=False, start=False)
    code_element = {
        "name": "验证码",
        "tag": "input",
        "role": "textbox",
        "accessible_name": "验证码",
        "fingerprint": {"tag": "input", "role": "textbox", "accessible_name": "验证码", "attrs": {"name": "password", "type": "text"}},
    }
    agreement = {
        "name": "本人已阅读并同意签署隐私政策",
        "tag": "div",
        "role": "checkbox",
        "accessible_name": "本人已阅读并同意签署隐私政策",
        "fingerprint": {"tag": "div", "role": "checkbox", "accessible_name": "本人已阅读并同意签署隐私政策", "attrs": {}},
    }
    flow = {
        "flow_version": 2,
        "name": "AI 额度申请",
        "base_url": "https://example.com/login",
        "steps": [
            {"action": "goto", "url": "https://example.com/login"},
            {"action": "fill", "name": "输入 验证码", "value": "${PASSWORD}", "element": code_element},
            {"action": "fill", "name": "输入 验证码", "value": "${PASSWORD}", "element": code_element},
            {"action": "check", "name": "勾选 本人已阅读并同意签署隐私政策", "element": agreement},
            {"action": "check", "name": "勾选 本人已阅读并同意签署隐私政策", "element": agreement},
        ],
        "metadata": {"required_variables": ["PASSWORD"], "review_required": True},
    }
    agent._update_task(
        task["id"],
        status="review_required",
        progress=40,
        plan={"generated_flow": flow, "generated_flow_filename": "test_ai_apply.yaml", "selected_case_files": []},
    )

    displayed = agent.get_agent_task(task["id"])
    displayed_flow = displayed["plan"]["generated_flow"]
    assert [step["action"] for step in displayed_flow["steps"]] == ["goto", "fill", "check"]
    assert displayed_flow["steps"][1]["value"] == "${VERIFICATION_CODE}"
    assert displayed["plan"]["required_variables"] == ["VERIFICATION_CODE"]

    saved = agent.save_generated_flow(task["id"])
    saved_flow = saved["plan"]["generated_flow"]

    assert [step["action"] for step in saved_flow["steps"]] == ["goto", "fill", "check"]
    assert saved_flow["steps"][1]["value"] == "${VERIFICATION_CODE}"
    assert saved_flow["metadata"]["required_variables"] == ["VERIFICATION_CODE"]
    assert saved_flow["metadata"]["deduplicated_steps"] == 2


def test_agent_generates_review_required_flow_when_no_case_matches(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo", "case_dir": "cases"})
    monkeypatch.setattr(agent, "_collect_case_assets", lambda _project_id: [])
    generated = {
        "flow": {"flow_version": 2, "name": "AI 登录", "steps": [{"action": "goto", "url": "https://example.com"}]},
        "filename": "test_ai_login.yaml",
        "confidence": 0.72,
        "required_variables": ["PASSWORD"],
        "sensitive_actions": [],
        "review_reasons": ["需要人工确认"],
        "generator_source": "rules",
    }
    monkeypatch.setattr(agent, "generate_flow_draft", lambda *_args, **_kwargs: generated)

    task = agent.create_agent_task(project_id="default", goal="验证新的登录流程", auto_execute=True, start=False)
    agent._run_agent_task(task["id"])

    reviewed = agent.get_agent_task(task["id"])
    assert reviewed["status"] == "review_required"
    assert reviewed["plan"]["generated_flow_filename"] == "test_ai_login.yaml"
    assert reviewed["execution_task_id"] in (None, "")


def test_saved_generated_flow_blocks_execution_until_variables_exist(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo", "case_dir": "cases"})
    task = agent.create_agent_task(project_id="default", goal="生成登录流程", auto_execute=False, start=False)
    flow = {
        "flow_version": 2,
        "name": "AI 登录",
        "base_url": "https://example.com",
        "browser": {"engine": "chromium", "viewport": {"width": 1440, "height": 900}},
        "steps": [
            {"action": "goto", "url": "https://example.com"},
            {"action": "fill", "value": "${PASSWORD}", "element": {"name": "密码"}},
        ],
    }
    plan = {"generated_flow": flow, "generated_flow_filename": "test_ai_login.yaml", "selected_case_files": []}
    agent._update_task(task["id"], status="review_required", progress=40, plan=plan)
    saved = agent.save_generated_flow(task["id"])
    monkeypatch.delenv("PASSWORD", raising=False)
    monkeypatch.setattr(agent.executor, "create_task", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")))

    agent._run_agent_task(task["id"], force_execute=True)

    blocked = agent.get_agent_task(task["id"])
    assert saved["status"] == "ready"
    assert blocked["status"] == "review_required"
    assert blocked["plan"]["missing_variables"] == ["PASSWORD"]


def test_missing_flow_variables_ignore_runtime_generators(monkeypatch):
    monkeypatch.delenv("PASSWORD", raising=False)
    monkeypatch.setattr(
        agent.case_service,
        "get_case",
        lambda *_args: "phone: ${random_phone_cn}\napply_id: ${random_apply_id}\npassword: ${PASSWORD}",
    )

    missing = agent._missing_flow_variables("default", ["test_flow.yaml"])

    assert missing == ["PASSWORD"]


def test_review_required_task_can_be_confirmed_after_data_is_ready(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo"})
    monkeypatch.setattr(agent.case_service, "get_case", lambda *_args: "phone: ${random_phone_cn}")
    started = []
    monkeypatch.setattr(
        agent,
        "start_agent_task",
        lambda task_id, **kwargs: started.append((task_id, kwargs)) or True,
    )
    task = agent.create_agent_task(project_id="default", goal="验证额度申请", auto_execute=False, start=False)
    plan = {"selected_case_files": ["test_apply.yaml"], "missing_variables": ["random_phone_cn"]}
    agent._update_task(task["id"], status="review_required", progress=45, plan=plan)

    confirmed = agent.execute_agent_task(task["id"])

    assert confirmed["status"] == "ready"
    assert "missing_variables" not in confirmed["plan"]
    assert started == [(task["id"], {"force_execute": True})]
    assert agent.list_agent_events(task["id"])[-1]["phase"] == "approval"


def test_review_required_task_stays_blocked_when_variable_is_missing(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo"})
    monkeypatch.setattr(agent.case_service, "get_case", lambda *_args: "password: ${PASSWORD}")
    monkeypatch.delenv("PASSWORD", raising=False)
    monkeypatch.setattr(
        agent,
        "start_agent_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    task = agent.create_agent_task(project_id="default", goal="验证登录流程", auto_execute=False, start=False)
    agent._update_task(
        task["id"],
        status="review_required",
        progress=45,
        plan={"selected_case_files": ["test_login.yaml"], "missing_variables": ["PASSWORD"]},
    )

    with pytest.raises(ValueError, match="仍缺少变量 PASSWORD"):
        agent.execute_agent_task(task["id"])

    blocked = agent.get_agent_task(task["id"])
    assert blocked["status"] == "review_required"
    assert blocked["plan"]["missing_variables"] == ["PASSWORD"]


def test_ready_task_stays_blocked_when_saved_flow_variable_is_missing(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.project_service, "get_project", lambda project_id: {"id": project_id, "name": "Demo"})
    monkeypatch.setattr(agent.case_service, "get_case", lambda *_args: "code: ${VERIFICATION_CODE}")
    monkeypatch.delenv("VERIFICATION_CODE", raising=False)
    monkeypatch.setattr(
        agent,
        "start_agent_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    task = agent.create_agent_task(project_id="default", goal="验证登录流程", auto_execute=False, start=False)
    agent._update_task(
        task["id"],
        status="ready",
        progress=45,
        plan={
            "selected_case_files": ["test_ai_login.yaml"],
            "generated_flow_saved": True,
            "generated_flow_filename": "test_ai_login.yaml",
            "missing_variables": ["VERIFICATION_CODE"],
        },
    )

    with pytest.raises(ValueError, match="仍缺少变量 VERIFICATION_CODE"):
        agent.execute_agent_task(task["id"])

    blocked = agent.get_agent_task(task["id"])
    assert blocked["status"] == "review_required"
    assert blocked["plan"]["missing_variables"] == ["VERIFICATION_CODE"]


def test_workflow_wait_ui_task_collects_real_result(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    import importlib
    wf = importlib.import_module("backend.agent.executor")

    wf_task = wf.create_agent_workflow_task(user_requirement="测试登录页面", auto_execute=False)
    wf_task_id = wf_task["id"]
    db.execute(
        "INSERT INTO tasks(id,project_id,module,case_files,status,passed,failed,skipped,total,env,triggered_by) "
        "VALUES('ui-task-1','default','ui','[]','running',0,0,0,0,'test','ai-agent')"
    )

    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            db.execute(
                "UPDATE tasks SET status='success', passed=3, failed=1, skipped=1, total=5 "
                "WHERE id='ui-task-1'"
            )

    monkeypatch.setattr(wf.time, "sleep", fake_sleep)

    task = wf._wait_ui_task(wf_task_id, "ui-task-1", timeout=30)
    assert task is not None
    assert task["status"] == "success"
    assert task["passed"] == 3
    assert task["total"] == 5

    result = {
        "status": "success",
        "task_id": "ui-task-1",
        "summary": {"total": 5, "passed": 3, "failed": 1, "skipped": 1},
        "logs": "FAILED tests/ui/test_login.py - AssertionError: 输入值未稳定保存",
        "screenshots": ["/screenshots/ui-task-1/003_failed.png"],
        "failure_details": [
            {
                "case": "test_login.yaml",
                "step_index": 3,
                "step": "输入 验证码",
                "error": "AssertionError: 输入值未稳定保存",
                "screenshot": "/screenshots/ui-task-1/003_failed.png",
            }
        ],
        "execution_steps": [
            {
                "index": 3,
                "action": "fill",
                "name": "输入 验证码",
                "status": "failed",
                "duration": 1.2,
                "error": "AssertionError: 输入值未稳定保存",
                "screenshot": "/screenshots/ui-task-1/003_failed.png",
                "log": "执行失败",
            }
        ],
    }
    db.execute(
        "INSERT INTO test_execution_result("
        "agent_task_id, step_id, tool, target, status, summary, logs, screenshots, api_response, failure_analysis, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (wf_task_id, 1, "run_ui_test", "run_ui_test", "dispatched", "dispatched", "", "[]", "", "{}", wf._now()),
    )
    wf._update_execution_result(agent_task_id=wf_task_id, step_id=1, result=result)
    rows = db.execute(
        "SELECT * FROM test_execution_result WHERE agent_task_id=? AND step_id=1",
        (wf_task_id,),
        fetch=True,
    )
    assert rows
    assert rows[0]["status"] == "success"
    assert "passed" in str(rows[0]["summary"])
    assert "输入值未稳定保存" in rows[0]["logs"]
    assert "003_failed.png" in rows[0]["screenshots"]
    assert "failure_details" in rows[0]["failure_analysis"]
    assert "execution_steps" in rows[0]["failure_analysis"]


def test_workflow_wait_ui_task_aborts_on_stop(monkeypatch, tmp_path):
    _temporary_database(monkeypatch, tmp_path)
    import importlib
    wf = importlib.import_module("backend.agent.executor")

    wf_task = wf.create_agent_workflow_task(user_requirement="测试登录页面", auto_execute=False)
    wf._stop_requests.add(wf_task["id"])
    try:
        task = wf._wait_ui_task(wf_task["id"], "ui-task-x", timeout=30)
        assert task is None
    finally:
        wf._stop_requests.discard(wf_task["id"])
