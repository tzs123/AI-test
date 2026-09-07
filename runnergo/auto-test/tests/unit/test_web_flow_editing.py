import yaml
from fastapi import HTTPException

from backend.web_recorder import (
    FlowImportIn,
    FlowSaveIn,
    RecordingStartIn,
    get_web_flow,
    import_web_flow,
    save_web_flow,
    start_recording,
    _standalone_debug_step,
)


def _flow(**overrides):
    payload = {
        "flow_version": 2,
        "name": "登录流程",
        "description": "编辑前描述",
        "base_url": "https://example.com",
        "browser": {"viewport": {"width": 1280, "height": 720}},
        "steps": [{"id": "step_1", "action": "goto", "url": "https://example.com"}],
        "metadata": {"source": "browser_recorder", "created_at": "2026-07-01 10:00:00"},
    }
    payload.update(overrides)
    return payload


def test_get_web_flow_returns_structured_case(monkeypatch):
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    monkeypatch.setattr(
        "backend.web_recorder.case_service.get_case",
        lambda project_id, module, filename: yaml.safe_dump(_flow(), allow_unicode=True),
    )

    result = get_web_flow("default", "test_login.yaml")

    assert result["filename"] == "test_login.yaml"
    assert result["flow"]["name"] == "登录流程"
    assert result["flow"]["steps"][0]["action"] == "goto"


def test_get_web_flow_rejects_legacy_yaml(monkeypatch):
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    monkeypatch.setattr(
        "backend.web_recorder.case_service.get_case",
        lambda project_id, module, filename: "name: legacy\nrequest: {}\n",
    )

    try:
        get_web_flow("default", "test_legacy.yaml")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "YAML 编辑" in exc.detail
    else:
        raise AssertionError("legacy YAML should not enter the visual scene builder")


def test_save_web_flow_updates_original_file_and_preserves_created_at(monkeypatch):
    saved = {}
    existing = yaml.safe_dump(_flow(), allow_unicode=True, sort_keys=False)
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    monkeypatch.setattr(
        "backend.web_recorder.case_service.get_case",
        lambda project_id, module, filename: existing,
    )

    def capture_save(project_id, module, filename, content):
        saved.update(
            project_id=project_id,
            module=module,
            filename=filename,
            content=content,
        )
        return f"/tmp/{filename}"

    monkeypatch.setattr("backend.web_recorder.case_service.save_case", capture_save)

    result = save_web_flow(
        FlowSaveIn(
            project_id="default",
            name="登录流程（已编辑）",
            filename="test_login.yaml",
            description="编辑后描述",
            base_url="https://example.com",
            viewport={"width": 390, "height": 844},
            steps=[
                {"id": "step_1", "action": "goto", "url": "https://example.com"},
                {"id": "step_2", "action": "click", "name": "点击登录"},
            ],
            runtime_assertions=[{
                "afterStepId": "step_2",
                "type": "assert_text",
                "expected": "${data.expected_submit_result}",
                "name": "校验提交结果",
            }],
        )
    )

    persisted = yaml.safe_load(saved["content"])
    assert saved["filename"] == "test_login.yaml"
    assert persisted["name"] == "登录流程（已编辑）"
    assert len(persisted["steps"]) == 2
    assert persisted["metadata"]["created_at"] == "2026-07-01 10:00:00"
    assert persisted["metadata"]["updated_at"]
    assert persisted["runtime_assertions"][0]["expected"] == "${data.expected_submit_result}"
    assert result["flow"] == persisted


def test_save_web_flow_as_rejects_existing_file(monkeypatch):
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    monkeypatch.setattr(
        "backend.web_recorder.case_service.get_case",
        lambda project_id, module, filename: yaml.safe_dump(_flow(), allow_unicode=True),
    )

    def fail_save(*_args):
        raise AssertionError("save_case should not overwrite an existing save-as target")

    monkeypatch.setattr("backend.web_recorder.case_service.save_case", fail_save)

    try:
        save_web_flow(
            FlowSaveIn(
                project_id="default",
                name="登录流程副本",
                filename="test_login.yaml",
                base_url="https://example.com",
                viewport={"width": 1280, "height": 720},
                steps=[{"id": "step_1", "action": "goto", "url": "https://example.com"}],
                overwrite=False,
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "YAML 文件已存在" in exc.detail
    else:
        raise AssertionError("save-as should reject an existing YAML filename")


def test_import_web_flow_validates_and_persists_yaml(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )

    def capture_save(project_id, module, filename, content):
        saved.update(project_id=project_id, module=module, filename=filename, content=content)
        return f"/tmp/{filename}"

    monkeypatch.setattr("backend.web_recorder.case_service.save_case", capture_save)
    result = import_web_flow(FlowImportIn(
        project_id="default",
        filename="login-flow.yml",
        content=yaml.safe_dump({
            "flow_version": 2,
            "name": "导入登录流程",
            "base_url": "https://example.com",
            "steps": [
                {"type": "goto", "url": "https://example.com"},
                {"action": "wait", "seconds": 0.1},
                {"action": "assert_error", "value": "${dataAssets.testData.OCR}", "timeout": 1000},
            ],
        }, allow_unicode=True),
    ))

    persisted = yaml.safe_load(saved["content"])
    assert saved["filename"] == "test_login_flow.yml"
    assert persisted["steps"][0]["action"] == "goto"
    assert persisted["steps"][0]["id"] == "imported_step_1"
    assert persisted["steps"][2]["action"] == "assert_error"
    assert persisted["steps"][2]["value"] == "${dataAssets.testData.OCR}"
    assert persisted["metadata"]["source"] == "imported_yaml"
    assert result["flow"] == persisted


def test_import_web_flow_rejects_unsupported_action(monkeypatch):
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    try:
        import_web_flow(FlowImportIn(
            project_id="default",
            filename="invalid.yaml",
            content="flow_version: 2\nsteps:\n  - action: launch_missile\n",
        ))
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "不支持的 action" in exc.detail
    else:
        raise AssertionError("unsupported action should be rejected")


def test_start_recording_seeds_existing_case_steps(monkeypatch):
    captured = {}
    initial_steps = [{"id": "step_1", "action": "goto", "url": "https://example.com"}]

    class FakeSession:
        def call(self, command):
            assert command == "snapshot"
            return {"session_id": "wrec_test", "steps": initial_steps}

    def fake_start(project_id, url, viewport, initial_steps=None):
        captured.update(
            project_id=project_id,
            url=url,
            viewport=viewport,
            initial_steps=initial_steps,
        )
        return FakeSession()

    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id, "base_url": "https://example.com", "envs": {}},
    )
    monkeypatch.setattr("backend.web_recorder.recording_manager.start", fake_start)

    result = start_recording(
        RecordingStartIn(
            project_id="default",
            url="https://example.com",
            viewport={"width": 1280, "height": 720},
            initial_steps=initial_steps,
        )
    )

    assert captured["initial_steps"] == initial_steps
    assert result["steps"] == initial_steps


def test_standalone_debug_api_does_not_require_browser(monkeypatch):
    import json

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"runnergo"}}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = _standalone_debug_step(
        {
            "action": "api",
            "name": "查询用户",
            "method": "GET",
            "url": "https://example.test/profile",
            "assert": [{"path": "$.data.name", "operator": "equals", "expected": "runnergo"}],
            "extract": {"user_name": "$.data.name"},
        },
    )

    assert result["ok"] is True
    assert result["standalone"] is True
    assert result["diagnostics"]["selected_strategy"] == "api"
    assert result["diagnostics"]["status"] == 200
    assert result["result"]["body"]["data"]["name"] == "runnergo"
    assert result["extracted"] == {"user_name": "runnergo"}


def test_standalone_debug_surfaces_business_failure_and_object_warnings(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"code":"500","success":false,"msg":"timestamp expired"}'

        def json(self):
            return {"code": "500", "success": False, "msg": "timestamp expired"}

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = _standalone_debug_step({
        "action": "api",
        "method": "GET",
        "url": "https://example.test/profile",
        "_object_warnings": ["动态认证头使用固定值"],
    })

    assert result["ok"] is True
    assert result["business_success"] is False
    assert result["diagnostics"]["business_code"] == "500"
    assert result["warnings"] == ["动态认证头使用固定值"]


def test_standalone_debug_surfaces_api_script_outputs(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"id":9}}'

        def json(self):
            return {"data": {"id": 9}}

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = _standalone_debug_step({
        "action": "api",
        "method": "GET",
        "url": "https://example.test/profile",
        "_scripts": {
            "post": 'console.log("done"); vars.set("responseId", response.json().data.id);',
        },
    })

    assert result["ok"] is True
    assert result["scripts"]["post"]["logs"] == ["log: done"]
    assert result["runtime_context"]["local"]["responseId"] == 9


def test_standalone_debug_resolves_runnergo_api_reference(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"runnergo"}}'

        def json(self):
            return {"data": {"name": "runnergo"}}

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    monkeypatch.setattr(
        "backend.web_recorder.runnergo_test_objects.resolve_references",
        lambda *_args, **_kwargs: {
            "team-1:api-1": {
                "step": {
                    "action": "api",
                    "method": "GET",
                    "url": "https://example.test/profile",
                },
            },
        },
    )

    result = _standalone_debug_step({
        "action": "api",
        "test_object_ref": {
            "team_id": "team-1",
            "target_id": "api-1",
            "target_type": "api",
            "name": "用户接口",
        },
        "extract": {"user_name": "$.data.name"},
    })

    assert result["ok"] is True
    assert result["result"]["url"] == "https://example.test/profile"
    assert result["extracted"] == {"user_name": "runnergo"}


def test_standalone_debug_api_keeps_response_when_assertion_fails(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"runnergo"}}'

        def json(self):
            return {"data": {"name": "runnergo"}}

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = _standalone_debug_step({
        "action": "api",
        "method": "GET",
        "url": "https://example.test/profile",
        "assert": [{"path": "$.data.name", "operator": "equals", "expected": "wrong"}],
    })

    assert result["ok"] is False
    assert "断言失败" in result["error"]
    assert result["result"]["status"] == 200
    assert result["diagnostics"]["status"] == 200


def test_standalone_debug_database_does_not_require_browser(tmp_path):
    import sqlite3

    path = tmp_path / "debug.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO users(id, name) VALUES(?, ?)", (7, "runnergo"))
    connection.commit()
    connection.close()

    result = _standalone_debug_step({
        "action": "db",
        "name": "查询用户",
        "connection": {"driver": "sqlite", "path": str(path)},
        "sql": "SELECT id, name FROM users WHERE id = ?",
        "params": [7],
        "assert": [{"path": "$.first.name", "operator": "equals", "expected": "runnergo"}],
        "extract": {"user_id": "$.first.id"},
    })

    assert result["ok"] is True
    assert result["diagnostics"]["selected_strategy"] == "database"
    assert result["diagnostics"]["row_count"] == 1
    assert result["result"]["first"]["name"] == "runnergo"
    assert result["extracted"] == {"user_id": 7}


def test_standalone_debug_rejects_page_steps_without_browser():
    try:
        _standalone_debug_step({"action": "assert_text", "expected": "ok"})
    except ValueError as exc:
        assert "只支持接口和数据库步骤" in str(exc)
    else:
        raise AssertionError("page steps must use browser-session debugging")
