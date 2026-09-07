import json
import os

from fastapi import Request

from backend import app as app_module
from backend import executor
from backend import runnergo_test_objects as objects
from backend.auth.service import Credential


def test_converts_api_test_object_to_runtime_step():
    converted = objects.convert_test_object({
        "team_id": "team-1",
        "target_id": "api-1",
        "target_type": "api",
        "name": "登录接口",
        "method": "POST",
        "url": "https://example.test/login",
        "request": {
            "header": {"parameter": [
                {"is_checked": 1, "key": "X-Tenant", "value": "{{tenant}}"},
                {"is_checked": 2, "key": "Disabled", "value": "no"},
            ]},
            "query": {"parameter": [{"is_checked": 1, "key": "debug", "value": "1"}]},
            "body": {"mode": "json", "raw": '{"username":"{{username}}"}'},
            "auth": {"type": "bearer", "bearer": {"key": "secret-token"}},
            "assert": [
                {"is_checked": 1, "response_type": 3, "compare": "Equal", "val": "200"},
                {"is_checked": 1, "response_type": 2, "var": "$.data.name", "compare": "Includes", "val": "runner"},
            ],
            "regex": [
                {"is_checked": 1, "type": 1, "var": "{{token}}", "express": "$.data.token"},
            ],
        },
    })

    step = converted["step"]
    assert step["method"] == "POST"
    assert step["headers"]["X-Tenant"] == "${tenant}"
    assert step["headers"]["Authorization"] == "Bearer secret-token"
    assert step["params"] == {"debug": "1"}
    assert step["json"] == {"username": "${username}"}
    assert step["assert"][0] == {"path": "$.status", "operator": "equals", "expected": "200"}
    assert step["extract"] == {"token": "$.data.token"}


def test_api_object_merges_global_defaults_headers_cookies_and_assertions():
    converted = objects.convert_test_object({
        "team_id": "team-1",
        "target_id": "api-global-1",
        "target_type": "api",
        "name": "全局配置接口",
        "method": "GET",
        "url": "{{base_url}}/profile",
        "variable": [
            {"key": "tenant", "value": "object-tenant"},
        ],
        "global_variable": {
            "variable": [
                {"is_checked": 1, "key": "base_url", "value": "https://global.example"},
                {"is_checked": 1, "key": "tenant", "value": "global-tenant"},
            ],
            "header": {"parameter": [
                {"is_checked": 1, "key": "X-Global", "value": "{{tenant}}"},
                {"is_checked": 1, "key": "X-Scope", "value": "global"},
            ]},
            "cookie": {"parameter": [
                {"is_checked": 1, "key": "session", "value": "{{session_id}}"},
            ]},
            "assert": [
                {"is_checked": 1, "response_type": 1, "var": "X-Request-ID", "compare": "NotNULL"},
            ],
        },
        "request": {
            "header": {"parameter": [
                {"is_checked": 1, "key": "X-Scope", "value": "request"},
            ]},
            "assert": [
                {"is_checked": 1, "response_type": 3, "compare": "Equal", "val": "200"},
            ],
        },
    })

    step = converted["step"]
    assert step["url"] == "${base_url}/profile"
    assert step["_object_variables"] == {
        "base_url": "https://global.example",
        "tenant": "object-tenant",
    }
    assert step["headers"]["X-Global"] == "${tenant}"
    assert step["headers"]["X-Scope"] == "request"
    assert step["headers"]["Cookie"] == "session=${session_id}"
    assert step["assert"] == [
        {"path": "$.headers.X-Request-ID", "operator": "not_equals", "expected": ""},
        {"path": "$.status", "operator": "equals", "expected": "200"},
    ]


def test_api_object_preserves_variable_basic_auth_for_runtime_resolution():
    converted = objects.convert_test_object({
        "team_id": "team-1",
        "target_id": "api-basic-1",
        "target_type": "api",
        "name": "Basic Auth 接口",
        "url": "https://example.test/profile",
        "global_variable": {
            "variable": [
                {"is_checked": 1, "key": "username", "value": "runner"},
                {"is_checked": 1, "key": "password", "value": "secret"},
            ],
        },
        "request": {
            "auth": {
                "type": "basic",
                "basic": {"username": "{{username}}", "password": "{{password}}"},
            },
        },
    })

    assert converted["step"]["_basic_auth"] == {
        "username": "${username}",
        "password": "${password}",
    }
    assert "Authorization" not in converted["step"]["headers"]


def test_api_object_preserves_scripts_and_warns_about_static_dynamic_auth_headers():
    converted = objects.convert_test_object({
        "team_id": "team-1",
        "target_id": "api-risk-1",
        "target_type": "api",
        "name": "动态签名接口",
        "url": "https://example.test/profile",
        "request": {
            "event": {
                "pre_script": "pm.variables.set('timestamp', Date.now())",
                "test": "pm.test('ok', function () {})",
            },
            "header": {"parameter": [
                {"is_checked": 1, "key": "X-API-Timestamp", "value": "1784278971184"},
                {"is_checked": 1, "key": "X-API-Signature", "value": "fixed-signature"},
                {"is_checked": 1, "key": "X-Dynamic-Nonce", "value": "{{nonce}}"},
            ]},
        },
    })

    step = converted["step"]
    assert step["_scripts"] == {
        "pre": "pm.variables.set('timestamp', Date.now())",
        "post": "pm.test('ok', function () {})",
    }
    warnings = step["_object_warnings"]
    assert any("X-API-Timestamp" in item and "X-API-Signature" in item for item in warnings)
    assert all("X-Dynamic-Nonce" not in item for item in warnings)
    assert all("不会执行" not in item for item in warnings)


def test_converts_sql_object_but_reference_contains_no_password():
    detail = {
        "team_id": "team-1",
        "target_id": "sql-1",
        "target_type": "sql",
        "name": "用户查询",
        "sql_detail": {
            "sql_string": "SELECT id, name FROM users WHERE id={{user_id}}",
            "sql_database_info": {
                "type": "mysql", "host": "db", "port": 3306,
                "user": "runner", "password": "db-secret", "db_name": "app",
            },
            "assert": [{"is_checked": 1, "field": "name", "index": 0, "compare": "Equal", "val": "runner"}],
            "regex": [{"is_checked": 1, "var": "userName", "field": "name", "index": 0}],
        },
    }

    reference = objects.collect_references({"steps": [{
        "action": "db",
        "test_object_ref": {"team_id": "team-1", "target_id": "sql-1", "target_type": "sql", "name": "用户查询"},
    }]})[0]
    converted = objects.convert_test_object(detail)

    assert "password" not in json.dumps(reference)
    assert converted["step"]["connection"]["password"] == "db-secret"
    assert converted["step"]["sql"] == "SELECT id, name FROM users WHERE id=${user_id}"
    assert converted["step"]["assert"][0]["path"] == "$.rows[0].name"
    assert converted["step"]["extract"] == {"userName": "$.rows[0].name"}


def test_sql_object_reads_sql_specific_default_variables():
    converted = objects.convert_test_object({
        "team_id": "team-1",
        "target_id": "sql-vars-1",
        "target_type": "sql",
        "name": "变量查询",
        "global_variable": {
            "variable": [{"is_checked": 1, "key": "user_id", "value": "1"}],
        },
        "sql_detail": {
            "sql_string": "SELECT name FROM users WHERE id={{user_id}}",
            "sql_variable": {
                "variable": [{"is_checked": 1, "key": "user_id", "value": "2"}],
            },
            "sql_database_info": {"type": "mysql"},
        },
    })

    assert converted["step"]["_object_variables"] == {"user_id": "2"}


def test_list_test_objects_forwards_team_header_and_filters(monkeypatch):
    captured = {}

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"code": 0, "data": {"targets": [
                {"team_id": "team-1", "target_id": "folder-1", "target_type": "folder", "name": "目录"},
                {"team_id": "team-1", "target_id": "api-1", "target_type": "api", "name": "查询接口", "method": "GET"},
                {"team_id": "team-1", "target_id": "sql-1", "target_type": "sql", "name": "用户查询"},
            ]}}

    def fake_get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(objects.requests, "get", fake_get)
    result = objects.list_test_objects("jwt-token", "team-1")

    assert [item["target_type"] for item in result] == ["api", "sql"]
    assert captured["headers"]["CurrentTeamID"] == "team-1"
    assert captured["headers"]["Authorization"] == "jwt-token"
    assert captured["params"] == {"team_id": "team-1"}


def test_list_teams_does_not_require_a_preselected_team(monkeypatch):
    captured = {}

    class Response:
        ok = True

        def __init__(self, url):
            self.url = url

        def json(self):
            if self.url.endswith("/setting/get"):
                return {"code": 0, "data": {"settings": {"current_team_id": "team-1"}}}
            return {"code": 0, "data": {"teams": [
                {"team_id": "team-1", "name": "默认团队", "type": 1, "sort": 2},
                {"team_id": "", "name": "无效团队"},
            ]}}

    def fake_get(url, **kwargs):
        captured.setdefault("calls", []).append({"url": url, **kwargs})
        return Response(url)

    monkeypatch.setattr(objects.requests, "get", fake_get)
    result = objects.list_teams("jwt-token")

    assert result == [{
        "team_id": "team-1", "name": "默认团队", "type": 1, "sort": 2,
        "is_current": True,
    }]
    assert [call["url"].rsplit("/", 2)[-2:] for call in captured["calls"]] == [
        ["team", "list"], ["setting", "get"],
    ]
    assert all(call["headers"]["CurrentTeamID"] == "" for call in captured["calls"])
    assert all(call["params"] == {} for call in captured["calls"])


def test_team_vum_quota_and_consumption_use_authenticated_management_api(monkeypatch):
    calls = []

    class Response:
        ok = True

        def __init__(self, method):
            self.method = method

        def json(self):
            if self.method == "GET":
                return {"code": 0, "data": {"team_id": "team-1", "available_vum_num": 5000}}
            return {"code": 0, "data": {
                "team_id": "team-1",
                "source_id": "run-1",
                "consumed_vum_num": 6,
                "available_vum_num": 4994,
                "already_consumed": False,
            }}

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return Response(method)

    monkeypatch.setattr(objects.requests, "request", fake_request)

    quota = objects.get_team_vum("jwt-token", "team-1")
    consumed = objects.consume_team_vum("jwt-token", "team-1", "run-1", 6)

    assert quota["available_vum_num"] == 5000
    assert consumed["available_vum_num"] == 4994
    assert calls[0]["headers"]["CurrentTeamID"] == "team-1"
    assert calls[1]["json"] == {
        "team_id": "team-1", "source_id": "run-1", "vum_num": 6,
    }


def test_runnergo_team_route_uses_current_login_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.auth_service, "extract_credential", lambda _request: Credential("jwt-token", "cookie"))
    monkeypatch.setattr(app_module.runnergo_test_objects, "list_teams", lambda token: captured.setdefault("token", token) and [{"team_id": "team-1", "name": "默认团队"}])
    request = Request({
        "type": "http", "method": "GET", "scheme": "http",
        "server": ("localhost", 8000), "path": "/api/runnergo-teams", "query_string": b"",
        "headers": [],
    })

    result = app_module.runnergo_team_list(request)

    assert captured["token"] == "jwt-token"
    assert result == {"teams": [{"team_id": "team-1", "name": "默认团队"}]}


def test_task_object_bundle_is_private_and_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "TEST_OBJECT_SECRET_DIR", str(tmp_path / "secrets"))
    bundle = {"team-1:sql-1": {"step": {"connection": {"password": "secret"}}}}

    path = executor.store_test_object_bundle("task_1", bundle)

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert executor._consume_test_object_bundle("task_1") == bundle
    assert not os.path.exists(path)


def test_run_resolves_references_without_storing_token_in_task(monkeypatch):
    captured = {}
    flow = {
        "flow_version": 2,
        "steps": [{
            "action": "api",
            "test_object_ref": {
                "team_id": "team-1", "target_id": "api-1",
                "target_type": "api", "name": "登录接口",
            },
        }],
    }
    bundle = {"team-1:api-1": {"target_type": "api", "step": {"url": "https://example.test"}}}

    monkeypatch.setattr(app_module.project_service, "get_project", lambda _pid: {"id": "default", "envs": {}})
    monkeypatch.setattr(app_module.case_service, "get_case", lambda *_args: json.dumps(flow))
    monkeypatch.setattr(app_module.auth_service, "extract_credential", lambda _request: Credential("jwt-secret", "authorization"))
    monkeypatch.setattr(app_module.runnergo_test_objects, "resolve_references", lambda payloads, token: captured.setdefault("resolved", (payloads, token)) and bundle)

    def fake_create_task(*args, **kwargs):
        captured["task_args"] = args
        captured["task_kwargs"] = kwargs
        return "task_1"

    monkeypatch.setattr(app_module.executor, "create_task", fake_create_task)
    monkeypatch.setattr(app_module.executor, "store_test_object_bundle", lambda task_id, value: captured.update(task_id=task_id, bundle=value))

    class Thread:
        def __init__(self, *args, **kwargs):
            captured["thread"] = (args, kwargs)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(app_module.threading, "Thread", Thread)
    request = Request({
        "type": "http", "method": "POST", "scheme": "http",
        "server": ("localhost", 8000), "path": "/api/run", "query_string": b"",
        "headers": [(b"authorization", b"jwt-secret")],
    })

    result = app_module.run(app_module.RunIn(
        project_id="default", module="ui", case_files=["cases/ui/test_ref.yaml"]
    ), request)

    assert result["task_id"] == "task_1"
    assert captured["bundle"] == bundle
    assert captured["resolved"][1] == "jwt-secret"
    assert "jwt-secret" not in json.dumps(captured["task_args"])
    assert "jwt-secret" not in json.dumps(captured["task_kwargs"])
