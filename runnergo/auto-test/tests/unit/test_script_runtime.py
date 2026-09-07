import pytest

from core.script_runtime import (
    ScriptAssertionError,
    ScriptExecutionError,
    run_script,
)


def test_script_runtime_supports_runnergo_variables_logs_and_response_helpers():
    result = run_script(
        """
        console.log("status", response.status);
        vars.set("token", response.json().data.token);
        pm.test("status ok", function () { assert(response.status === 200, "status"); });
        """,
        variables={"seed": "ready"},
        request={"method": "GET", "url": "https://example.test"},
        response={"status": 200, "code": 200, "headers": {}, "body": '{"data":{"token":"t-1"}}'},
    )

    assert result["variables"] == {"token": "t-1"}
    assert result["logs"] == ["log: status 200"]
    assert result["assertions"] == [
        {"name": "status", "passed": True, "message": "status"},
        {"name": "status ok", "passed": True, "message": "status ok"},
    ]


def test_script_runtime_returns_changes_on_assertion_failure():
    with pytest.raises(ScriptAssertionError) as captured:
        run_script('vars.set("seen", true); assert(false, "business rule");')

    assert captured.value.result["variables"] == {"seen": True}
    assert captured.value.result["assertions"][0]["passed"] is False


def test_script_runtime_blocks_dynamic_code_and_infinite_loops():
    with pytest.raises(ScriptExecutionError):
        run_script('eval("1 + 1")')

    with pytest.raises(ScriptExecutionError):
        run_script('vars.set("value", [].filter.constructor("return 1")());')

    with pytest.raises(ScriptExecutionError, match="超时"):
        run_script("while (true) {}")
