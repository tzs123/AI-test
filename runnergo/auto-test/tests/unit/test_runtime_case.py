from core.runtime_case import (
    RuntimeCase,
    inject_runtime_assertions,
    public_input_fields,
    resolve_dynamic_value,
)


def sample_flow():
    return {
        "flow_version": 2,
        "steps": [
            {
                "id": "phone-step",
                "action": "fill",
                "name": "输入手机号",
                "element": {"name": "手机号输入框"},
                "value": "13800000000",
            },
            {
                "id": "group-step",
                "type": "custom",
                "steps": [
                    {
                        "id": "code-step",
                        "action": "send_keys",
                        "name": "输入验证码",
                        "element": {"accessible_name": "验证码"},
                        "keys": "123456",
                    }
                ],
            },
        ],
    }


def test_scans_nested_input_actions_and_elements():
    fields = public_input_fields(sample_flow())

    assert [field["stepId"] for field in fields] == ["phone-step", "code-step"]
    assert fields[0]["element"] == "手机号输入框"
    assert fields[1]["defaultValue"] == "123456"
    assert fields[1]["stepPath"] == "steps.1.steps.0"


def test_runtime_case_does_not_mutate_source_and_records_final_values():
    source = sample_flow()
    runtime = RuntimeCase(source, [{
        "stepId": "phone-step",
        "stepPath": "steps.0",
        "runtimeValue": "${random_phone}",
    }]).build()

    assert source["steps"][0]["value"] == "13800000000"
    final_value = runtime.case_data["steps"][0]["value"]
    assert final_value.isdigit() and len(final_value) == 11
    assert runtime.execution_data[0]["source"] == "runtimeOverride"
    assert runtime.execution_data[0]["finalValue"] == final_value
    assert runtime.execution_data[1]["source"] == "defaultValue"


def test_empty_runtime_value_is_an_explicit_override():
    runtime = RuntimeCase(sample_flow(), [{
        "stepId": "phone-step",
        "runtimeValue": "",
    }]).build()

    assert runtime.case_data["steps"][0]["value"] == ""
    assert runtime.execution_data[0]["source"] == "runtimeOverride"


def test_supported_dynamic_values_are_resolved():
    result = resolve_dynamic_value(
        "${timestamp}|${uuid}|${random_phone}|${random_string(12)}|${UNKNOWN}"
    )
    timestamp, uuid_value, phone, random_text, unknown = result.split("|")
    assert timestamp.isdigit() and len(timestamp) >= 13
    assert len(uuid_value) == 36
    assert phone.isdigit() and len(phone) == 11
    assert len(random_text) == 12
    assert unknown == "${UNKNOWN}"


def test_injects_data_driven_assertion_after_configured_step():
    source = {
        **sample_flow(),
        "runtime_assertions": [
            {
                "afterStepId": "phone-step",
                "type": "assert_text",
                "expected": "${data.expected_submit_result}",
                "name": "校验提交结果",
            }
        ],
    }

    runtime = RuntimeCase(source, [{
        "stepId": "phone-step",
        "runtimeValue": "${data.phone}",
    }]).build()

    steps = runtime.case_data["steps"]
    assert steps[0]["id"] == "phone-step"
    assert steps[1]["action"] == "assert_text"
    assert steps[1]["value"] == "${data.expected_submit_result}"
    assert steps[1]["runtime_generated_assertion"] is True
    assert steps[2]["id"] == "group-step"
    assert source["steps"][1]["id"] == "group-step"


def test_injects_error_assertion_after_configured_step_without_expected_text():
    source = {
        **sample_flow(),
        "runtime_assertions": [
            {
                "afterStepId": "phone-step",
                "type": "assert_error",
                "name": "校验 OCR 识别错误",
            }
        ],
    }

    runtime = RuntimeCase(source).build()

    steps = runtime.case_data["steps"]
    assert steps[1]["action"] == "assert_error"
    assert steps[1]["value"] == ""
    assert steps[1]["runtime_generated_assertion"] is True


def test_rejects_runtime_assertion_with_missing_target_step():
    source = {
        **sample_flow(),
        "runtime_assertions": [
            {"afterStepId": "missing-step", "type": "assert_text", "expected": "${data.result}"}
        ],
    }

    try:
        inject_runtime_assertions(source)
    except ValueError as exc:
        assert "找不到插入步骤" in str(exc)
    else:
        raise AssertionError("missing runtime assertion target should fail")
