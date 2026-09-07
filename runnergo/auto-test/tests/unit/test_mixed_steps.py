import json
import sqlite3

import pytest

from core import mixed_steps
from core.mixed_steps import RuntimeDataContext, execute_api_step, execute_database_step, get_path


def test_runtime_context_resolves_nested_outputs_and_interpolation():
    context = RuntimeDataContext({"local": {"user_id": 7}})
    context.assign("login", {"body": {"token": "abc"}}, category="api")

    assert context.resolve("${api.login.body.token}") == "abc"
    assert context.resolve("Bearer ${login.body.token}") == "Bearer abc"
    assert context.resolve("${user_id}") == 7


def test_json_path_supports_object_and_array_access():
    source = {"data": {"items": [{"id": 11}, {"id": 12}]}}
    assert get_path(source, "$.data.items[1].id") == 12
    assert get_path(source, "data.items[0].id") == 11


def test_api_step_passes_request_data_and_extracts_response(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.com/login"
        text = '{"data":{"token":"t-1"}}'

        def json(self):
            return json.loads(self.text)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("core.mixed_steps.requests.request", request)
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext({"local": {"user": "runner"}})
    result = execute_api_step(
        {
            "method": "POST",
            "url": "https://example.com/login",
            "json": {"username": "${user}"},
            "assert": {"status": 200, "path": "$.data.token", "equals": "t-1"},
            "extract": {"token": "$.data.token"},
            "save_as": "login",
        },
        context,
    )

    assert calls[0][0:2] == ("POST", "https://example.com/login")
    assert calls[0][2]["json"] == {"username": "runner"}
    assert result["extracted"] == {"token": "t-1"}
    assert context.resolve("${token}") == "t-1"
    assert context.resolve("${api.login.body.data.token}") == "t-1"


def test_api_step_reports_business_failure_without_overriding_transport_success(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"code":"500","success":false,"msg":"timestamp expired"}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = execute_api_step({
        "method": "GET",
        "url": "https://example.test/profile",
        "_object_warnings": ["动态认证头使用固定值"],
    }, RuntimeDataContext())

    assert result["status"] == 200
    assert result["business"] == {
        "success": False,
        "code": "500",
        "message": "timestamp expired",
        "signal": "success",
    }
    assert result["warnings"] == ["动态认证头使用固定值"]


def test_api_object_scripts_generate_request_data_and_publish_post_results(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"id":9}}'

        def json(self):
            return json.loads(self.text)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("core.mixed_steps.requests.request", request)
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext()

    result = execute_api_step({
        "action": "api",
        "method": "GET",
        "url": "https://example.test/profile",
        "headers": {"X-Token": "${token}"},
        "_scripts": {
            "pre": 'vars.set("token", "generated-token"); console.info("token ready");',
            "post": 'vars.set("responseId", response.json().data.id); pm.test("status", function () { assert(response.status === 200, "status code"); });',
        },
    }, context)

    assert calls[0][2]["headers"]["X-Token"] == "generated-token"
    assert context.resolve("${token}") == "generated-token"
    assert context.resolve("${responseId}") == 9
    assert result["scripts"]["pre"]["logs"] == ["info: token ready"]
    assert result["scripts"]["post"]["assertions"][-1]["name"] == "status"


def test_api_pre_script_failure_skips_network_and_keeps_diagnostics(monkeypatch):
    calls = []
    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: calls.append(True))
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    with pytest.raises(ValueError, match="前置脚本执行失败") as captured:
        execute_api_step({
            "action": "api",
            "method": "GET",
            "url": "https://example.test/profile",
            "_scripts": {"pre": 'eval("1 + 1")'},
        }, RuntimeDataContext())

    assert calls == []
    assert captured.value.runtime_result["scripts"]["pre"]["ok"] is False


def test_api_network_failure_keeps_resolved_request_url(monkeypatch):
    class FailingSession:
        def request(self, _method, _url, **_kwargs):
            raise TimeoutError("request timed out")

    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext()
    context.http_session = FailingSession()

    with pytest.raises(TimeoutError) as captured:
        execute_api_step({
            "action": "api",
            "method": "GET",
            "url": "http://127.0.0.1:18080/health",
        }, context)

    assert captured.value.runtime_result["url"] == "http://127.0.0.1:18080/health"


def test_api_post_script_assertion_keeps_response_and_applies_variables(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"id":9}}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext()

    with pytest.raises(AssertionError, match="后置脚本执行失败") as captured:
        execute_api_step({
            "action": "api",
            "method": "GET",
            "url": "https://example.test/profile",
            "_scripts": {
                "post": 'vars.set("responseId", response.json().data.id); assert(false, "business rule");',
            },
        }, context)

    assert context.resolve("${responseId}") == 9
    assert captured.value.runtime_result["status"] == 200
    assert captured.value.runtime_result["scripts"]["post"]["ok"] is False


def test_database_step_is_read_only_and_transfers_values(tmp_path):
    path = tmp_path / "app.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO users(id, name) VALUES(?, ?)", (3, "runner"))
    connection.commit()
    connection.close()

    context = RuntimeDataContext()
    result = execute_database_step(
        {
            "connection": {"driver": "sqlite", "path": str(path)},
            "sql": "SELECT id, name FROM users WHERE id = ?",
            "params": [3],
            "save_as": "user_query",
            "extract": {"user_id": "$.rows[0].id", "user_name": "$.first.name"},
        },
        context,
        {},
    )

    assert result["row_count"] == 1
    assert context.resolve("${user_id}") == 3
    assert context.resolve("${db.user_query.first.name}") == "runner"

    with pytest.raises(ValueError, match="只读 SQL"):
        execute_database_step(
            {
                "connection": {"driver": "sqlite", "path": str(path)},
                "sql": "DELETE FROM users",
            },
            context,
            {},
        )


def test_api_and_database_assertions_support_runtime_operators(monkeypatch, tmp_path):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"runnergo"},"message":"success"}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext({"local": {"expected_name": "runnergo"}})

    api_result = execute_api_step(
        {
            "method": "GET",
            "url": "https://example.test/profile",
            "assert": [
                {"path": "$.data.name", "expected": "${expected_name}", "operator": "equals"},
                {"path": "$.message", "expected": "cess", "operator": "ends_with"},
            ],
            "save_as": "profile",
        },
        context,
    )

    assert api_result["body"]["data"]["name"] == "runnergo"
    assert context.resolve("${api.profile.body.data.name}") == "runnergo"

    path = tmp_path / "profile.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(name TEXT)")
    connection.execute("INSERT INTO users(name) VALUES(?)", ("runnergo",))
    connection.commit()
    connection.close()

    db_result = execute_database_step(
        {
            "connection": {"driver": "sqlite", "path": str(path)},
            "sql": "SELECT name FROM users",
            "assert": {
                "path": "$.first.name",
                "expected": "${api.profile.body.data.name}",
                "operator": "equals",
            },
            "save_as": "profile_db",
        },
        context,
        {},
    )

    assert db_result["first"]["name"] == "runnergo"
    assert context.resolve("${db.profile_db.first.name}") == "runnergo"


def test_api_step_resolves_runnergo_test_object_reference(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"runnergo"}}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_JSON", json.dumps({
        "team-1:api-1": {
            "target_type": "api",
            "step": {
                "action": "api",
                "method": "GET",
                "url": "https://example.test/profile",
                "headers": {"Authorization": "Bearer object-secret"},
            },
        },
    }))
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)
    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    result = execute_api_step({
        "action": "api",
        "test_object_ref": {"team_id": "team-1", "target_id": "api-1", "target_type": "api", "name": "用户接口"},
        "assert": [{"path": "$.data.name", "operator": "equals", "expected": "runnergo"}],
        "save_as": "profile",
    }, RuntimeDataContext())

    assert result["body"]["data"]["name"] == "runnergo"
    assert result["test_object_ref"]["target_id"] == "api-1"


def test_api_object_defaults_are_fallbacks_and_extractions_write_shared_context(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://object.test/users/42"
        text = '{"data":{"name":"runnergo","token":"shared-token"}}'

        def json(self):
            return json.loads(self.text)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_JSON", json.dumps({
        "team-1:api-defaults": {
            "target_type": "api",
            "step": {
                "action": "api",
                "method": "POST",
                "url": "${base_url}/users/${user_id}",
                "headers": {"X-Tenant": "${tenant}"},
                "_basic_auth": {"username": "${api_user}", "password": "${api_password}"},
                "json": {"user_id": "${user_id}"},
                "assert": [{
                    "path": "$.data.name",
                    "operator": "equals",
                    "expected": "${expected_name}",
                }],
                "extract": {"api_token": "$.data.token"},
                "_object_variables": {
                    "base_url": "https://object.test",
                    "user_id": 7,
                    "tenant": "object-team",
                    "api_user": "object-user",
                    "api_password": "object-password",
                    "expected_name": "runnergo",
                },
            },
        },
    }))
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)
    monkeypatch.setattr("core.mixed_steps.requests.request", request)
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    shared_context = RuntimeDataContext({"local": {"user_id": 42}})

    result = execute_api_step({
        "action": "api",
        "test_object_ref": {
            "team_id": "team-1",
            "target_id": "api-defaults",
            "target_type": "api",
            "name": "默认变量接口",
        },
        "extract": {"api_name": "$.data.name"},
        "save_as": "profile",
    }, shared_context)

    assert calls[0][0:2] == ("POST", "https://object.test/users/42")
    assert calls[0][2]["headers"] == {"X-Tenant": "object-team"}
    assert calls[0][2]["auth"] == ("object-user", "object-password")
    assert calls[0][2]["json"] == {"user_id": 42}
    assert result["extracted"] == {
        "api_token": "shared-token",
        "api_name": "runnergo",
    }
    assert shared_context.resolve("${api_token}") == "shared-token"
    assert shared_context.resolve("${api_name}") == "runnergo"
    assert shared_context.resolve("${api.profile.body.data.token}") == "shared-token"
    assert shared_context.lookup("tenant") is mixed_steps._MISSING


def test_database_step_resolves_runnergo_sql_object_reference(monkeypatch, tmp_path):
    path = tmp_path / "reference.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(name TEXT)")
    connection.execute("INSERT INTO users(name) VALUES('runnergo')")
    connection.commit()
    connection.close()
    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_JSON", json.dumps({
        "team-1:sql-1": {
            "target_type": "sql",
            "step": {
                "action": "db",
                "sql": "SELECT name FROM users",
                "connection": {"driver": "sqlite", "path": str(path)},
            },
        },
    }))
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)

    result = execute_database_step({
        "action": "db",
        "test_object_ref": {"team_id": "team-1", "target_id": "sql-1", "target_type": "sql", "name": "用户查询"},
        "assert": [{"path": "$.first.name", "operator": "equals", "expected": "runnergo"}],
    }, RuntimeDataContext(), {})

    assert result["first"]["name"] == "runnergo"


def test_sql_object_defaults_are_fallbacks_and_extractions_write_shared_context(monkeypatch, tmp_path):
    path = tmp_path / "reference-defaults.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO users(id, name) VALUES(1, 'object-default')")
    connection.execute("INSERT INTO users(id, name) VALUES(2, 'runtime-value')")
    connection.commit()
    connection.close()
    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_JSON", json.dumps({
        "team-1:sql-defaults": {
            "target_type": "sql",
            "step": {
                "action": "db",
                "sql": "SELECT name FROM users WHERE id = ${user_id}",
                "connection": {"driver": "sqlite", "path": str(path)},
                "assert": [{
                    "path": "$.first.name",
                    "operator": "equals",
                    "expected": "${expected_name}",
                }],
                "extract": {"db_user_name": "$.first.name"},
                "_object_variables": {
                    "user_id": 1,
                    "expected_name": "object-default",
                },
            },
        },
    }))
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)
    shared_context = RuntimeDataContext({
        "local": {"user_id": 2, "expected_name": "runtime-value"},
    })

    result = execute_database_step({
        "action": "db",
        "test_object_ref": {
            "team_id": "team-1",
            "target_id": "sql-defaults",
            "target_type": "sql",
            "name": "默认变量查询",
        },
        "extract": {"db_user_name_copy": "$.rows[0].name"},
        "save_as": "selected_user",
    }, shared_context, {})

    assert result["first"]["name"] == "runtime-value"
    assert shared_context.resolve("${db_user_name}") == "runtime-value"
    assert shared_context.resolve("${db_user_name_copy}") == "runtime-value"
    assert shared_context.resolve("${db.selected_user.first.name}") == "runtime-value"


def test_runnergo_runtime_object_file_is_deleted_after_loading(monkeypatch, tmp_path):
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"team-1:api-1": {"target_type": "api", "step": {}}}))
    monkeypatch.delenv("RUNNERGO_TEST_OBJECTS_JSON", raising=False)
    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_FILE", str(path))
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)

    payload = mixed_steps._runtime_test_objects()

    assert "team-1:api-1" in payload
    assert not path.exists()


def test_runnergo_runtime_object_directory_isolated_per_xdist_worker(monkeypatch, tmp_path):
    secret_dir = tmp_path / "objects"
    secret_dir.mkdir(mode=0o700)
    payload = {"team-1:api-1": {"target_type": "api", "step": {}}}
    worker_path = secret_dir / "gw0.json"
    other_worker_path = secret_dir / "gw1.json"
    worker_path.write_text(json.dumps(payload))
    other_worker_path.write_text(json.dumps(payload))
    monkeypatch.delenv("RUNNERGO_TEST_OBJECTS_FILE", raising=False)
    monkeypatch.delenv("RUNNERGO_TEST_OBJECTS_JSON", raising=False)
    monkeypatch.setenv("RUNNERGO_TEST_OBJECTS_DIR", str(secret_dir))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setattr(mixed_steps, "_TEST_OBJECTS_CACHE", None)

    loaded = mixed_steps._runtime_test_objects()

    assert loaded == payload
    assert not worker_path.exists()
    assert other_worker_path.exists()


def test_web_flow_runner_preserves_javascript_template_literals():
    from core.web_flow_runner import WebFlowRunner

    runner = WebFlowRunner(None, {"variables": {"native": "must-not-replace"}})
    resolved = runner._resolve_runtime_step({
        "action": "api",
        "url": "https://example.test/${native}",
        "_scripts": {"pre": "const value = `${native}`;"},
    })

    assert resolved["url"] == "https://example.test/must-not-replace"
    assert resolved["_scripts"]["pre"] == "const value = `${native}`;"


def test_web_flow_runner_attaches_api_runtime_result_on_failure(monkeypatch):
    from core.web_flow_runner import WebFlowRunner

    failure = AssertionError("script failed")
    failure.runtime_result = {
        "status": 200,
        "url": "https://example.test/profile",
        "scripts": {"post": {"ok": False, "error": "script failed"}},
        "extracted": {},
    }
    monkeypatch.setattr("core.web_flow_runner.execute_api_step", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
    attached = []
    runner = WebFlowRunner(None, {})
    monkeypatch.setattr(runner, "_attach_runtime_data", lambda title, result: attached.append((title, result)))

    with pytest.raises(AssertionError, match="script failed"):
        runner.execute_step({"action": "api", "name": "用户接口", "url": "https://example.test/profile"})

    assert attached[0][0] == "用户接口"
    assert attached[0][1]["scripts"]["post"]["ok"] is False
    assert runner.last_locator_diagnostics["matched"] is False
