from fastapi import Request
from fastapi.testclient import TestClient
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend import app as app_module
from backend.auth import service


def _request(method="GET", headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "method": method,
        "scheme": "http",
        "server": ("localhost", 9999),
        "path": "/api/projects",
        "query_string": b"",
        "headers": raw_headers,
    })


def test_extracts_explicit_header_before_cookie():
    request = _request(headers={
        "Authorization": "Bearer header-token",
        "Cookie": "token=cookie-token",
    })
    credential = service.extract_credential(request)
    assert credential.token == "header-token"
    assert credential.source == "authorization"


def test_cookie_mutation_requires_same_origin():
    valid = _request(method="POST", headers={
        "Host": "runnergo.example:9999",
        "Origin": "http://runnergo.example:9999",
        "Cookie": "token=value",
    })
    invalid = _request(method="POST", headers={
        "Host": "runnergo.example:9999",
        "Origin": "http://evil.example",
        "Cookie": "token=value",
    })
    assert service.cookie_request_has_valid_origin(valid) is True
    assert service.cookie_request_has_valid_origin(invalid) is False


def test_cookie_mutation_accepts_forwarded_public_port():
    request = _request(method="POST", headers={
        "Host": "auto-test:8000",
        "X-Forwarded-Host": "127.0.0.1:9999",
        "Origin": "http://127.0.0.1:9999",
        "Cookie": "token=value",
    })
    assert service.cookie_request_has_valid_origin(request) is True


def test_health_is_public_but_application_routes_require_auth(monkeypatch):
    monkeypatch.setattr(app_module.settings, "AUTH_REQUIRED", True)
    client = TestClient(app_module.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 401


def test_fake_token_is_rejected(monkeypatch):
    monkeypatch.setattr(app_module.settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(service, "validate_credential", lambda _token: None)
    client = TestClient(app_module.app)

    response = client.get("/api/projects", headers={"Authorization": "Bearer fake"})
    assert response.status_code == 401


def test_internal_agent_route_requires_shared_token(monkeypatch):
    monkeypatch.setattr(app_module.settings, "AUTH_REQUIRED", False)
    monkeypatch.setenv("TEST_DATA_CENTER_AGENT_TOKEN", "shared-secret")
    client = TestClient(app_module.app)

    response = client.post(
        "/api/agent/internal/orchestrate-ui",
        json={"case_files": ["login.yaml"]},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "内部 Agent 令牌无效"


def test_internal_terminal_task_returns_failure_evidence(monkeypatch):
    monkeypatch.setattr(
        app_module.executor,
        "_get_task",
        lambda _task_id: {
            "id": "ui-task-1",
            "status": "failed",
            "case_files": '["login.yaml"]',
            "runtime_override": "[]",
            "runtime_data": "[]",
            "runtime_variables": "{}",
        },
    )
    monkeypatch.setattr(
        app_module.step_store,
        "read",
        lambda _task_id: {
            "status": "failed",
            "steps": [{
                "index": 2,
                "step_index": 2,
                "name": "输入验证码",
                "status": "failed",
                "error": "AssertionError: 输入值未稳定保存",
            }],
        },
    )
    monkeypatch.setattr(
        app_module.log_store,
        "read",
        lambda _task_id: "pytest output\nAssertionError: 输入值未稳定保存",
    )

    result = app_module.internal_ui_task_detail("ui-task-1")

    assert result["case_files"] == ["login.yaml"]
    assert result["failure_steps"][0]["name"] == "输入验证码"
    assert "AssertionError" in result["log_tail"]


def test_internal_ui_orchestration_generates_bindings_and_dispatches(monkeypatch):
    from backend.agent import tools as agent_tools

    monkeypatch.setattr(app_module.settings, "AUTH_REQUIRED", True)
    monkeypatch.setenv("TEST_DATA_CENTER_AGENT_TOKEN", "shared-secret")
    monkeypatch.setattr(app_module.executor, "_case_file_path", lambda *_args: __file__)
    calls = {}

    def fake_prepare(**kwargs):
        calls["prepare"] = kwargs
        return {
            "bindings": [{
                "case_file": "login.yaml",
                "alias": "agent_testhub_1",
                "runtime_override": [{
                    "stepId": "input-user",
                    "runtimeValue": "${dataAssets.agent_testhub_1.username}",
                }],
                "asset": {"data_center_asset": {"synced": True, "asset_id": 9}},
            }],
        }

    def fake_run(**kwargs):
        calls["run"] = kwargs
        return {"task_id": "ui-task-1", "task_ids": ["ui-task-1"]}

    monkeypatch.setattr(agent_tools, "prepare_ui_data_binding", fake_prepare)
    monkeypatch.setattr(agent_tools, "run_ui_test", fake_run)
    client = TestClient(app_module.app)

    response = client.post(
        "/api/agent/internal/orchestrate-ui",
        headers={"X-Agent-Token": "shared-secret"},
        json={
            "project_id": "default",
            "case_files": ["login.yaml"],
            "test_data_count": 4,
            "parent_task_id": "testhub-17",
        },
    )

    assert response.status_code == 200
    assert response.json()["task_ids"] == ["ui-task-1"]
    assert response.json()["test_data_count"] == 4
    assert calls["prepare"]["count"] == 4
    assert calls["run"]["data_bindings"][0]["alias"] == "agent_testhub_1"


def test_internal_ui_orchestration_reuses_supplied_agent_binding(monkeypatch):
    from backend.agent import tools as agent_tools

    monkeypatch.setattr(app_module.settings, "AUTH_REQUIRED", True)
    monkeypatch.setenv("TEST_DATA_CENTER_AGENT_TOKEN", "shared-secret")
    monkeypatch.setattr(app_module.executor, "_case_file_path", lambda *_args: __file__)
    calls = {}

    def fake_prepare(**kwargs):
        calls["prepare"] = kwargs
        return {"bindings": kwargs["existing_bindings"]}

    monkeypatch.setattr(agent_tools, "prepare_ui_data_binding", fake_prepare)
    monkeypatch.setattr(
        agent_tools,
        "run_ui_test",
        lambda **kwargs: {"task_id": "ui-task-1", "task_ids": ["ui-task-1"], **kwargs},
    )
    binding = {
        "target_id": "login.yaml",
        "target_case_id": 7,
        "alias": "agent_ui_yaml_1_1",
        "asset_id": 9,
        "requirement_id": 11,
        "fields": ["username"],
    }
    client = TestClient(app_module.app)

    response = client.post(
        "/api/agent/internal/orchestrate-ui",
        headers={"X-Agent-Token": "shared-secret"},
        json={
            "case_files": ["login.yaml"],
            "data_bindings": [binding],
        },
    )

    assert response.status_code == 200
    assert calls["prepare"]["existing_bindings"] == [binding]


def test_concurrent_validation_shares_one_permission_lookup(monkeypatch):
    service.clear_auth_cache()
    calls = 0
    calls_lock = threading.Lock()

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"code": "0", "data": {"id": "user-1"}}

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        # Keep the leader in-flight long enough for the other callers to join.
        time.sleep(0.1)
        return Response()

    monkeypatch.setattr(service.requests, "get", fake_get)
    token = "concurrent-token"
    barrier = threading.Barrier(4)

    def validate():
        barrier.wait()
        return service.validate_credential(token)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _item: validate(), range(4)))

    assert calls == 1
    assert results == [{"id": "user-1"}] * 4
