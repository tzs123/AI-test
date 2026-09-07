from backend import settings, step_store


def test_step_store_tracks_progress_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    flow = {
        "flow_version": 2,
        "name": "导入场景",
        "steps": [
            {"id": "open", "action": "goto", "name": "打开页面"},
            {"id": "submit", "action": "click", "name": "点击提交"},
            {"id": "result", "action": "assert_text", "name": "校验结果"},
        ],
    }

    state = step_store.initialize("task_demo", flow, case_file="test_demo.yaml")
    assert state["total"] == 3
    assert state["counts"]["pending"] == 3
    assert state["steps"][2]["is_assertion"] is True
    assert state["steps"][2]["assertion_matched"] is None

    step_store.update_step("task_demo", 1, status="passed", duration=0.2, screenshot="/screenshots/1.png")
    step_store.update_step("task_demo", 2, status="failed", error="元素定位失败")
    state = step_store.finalize("task_demo", "failed", error="第 2 步失败")

    assert state["status"] == "failed"
    assert state["steps"][0]["status"] == "passed"
    assert state["steps"][1]["status"] == "failed"
    assert state["steps"][2]["status"] == "skipped"
    assert state["completed"] == 3
    assert step_store.read("task_demo")["error"] == "第 2 步失败"

    step_store.delete("task_demo")
    assert step_store.read("task_demo") == {}


def test_step_store_keeps_parameterized_iterations_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    flow = {
        "flow_version": 2,
        "name": "参数化登录",
        "steps": [
            {"id": "phone", "action": "fill", "name": "输入手机号"},
            {"id": "code", "action": "fill", "name": "输入验证码"},
        ],
    }

    state = step_store.initialize(
        "task_parameterized",
        flow,
        case_file="test_login.yaml",
        iteration_index=1,
        iteration_count=3,
        iteration_label="data-1",
        iteration_data={"testData": {"phone": "18800000001", "code": "111111"}},
        iteration_data_rows=[
            {"testData": {"phone": "18800000001", "code": "111111"}},
            {"testData": {"phone": "18800000002", "code": "222222"}},
            {"testData": {"phone": "18800000003", "code": "333333"}},
        ],
    )
    assert state["total"] == 6
    assert len(state["iterations"]) == 3
    assert state["iterations"][1]["data"]["testData"]["code"] == "222222"
    assert state["iterations"][2]["data"]["testData"]["phone"] == "18800000003"

    step_store.update_step(
        "task_parameterized",
        1,
        iteration_index=1,
        status="passed",
        diagnostics={"expected": "18800000001"},
    )
    step_store.update_step(
        "task_parameterized",
        2,
        iteration_index=1,
        status="passed",
        diagnostics={"expected": "111111"},
    )
    step_store.finalize("task_parameterized", "success", iteration_index=1)
    step_store.initialize(
        "task_parameterized",
        flow,
        case_file="test_login.yaml",
        iteration_index=2,
        iteration_count=3,
        iteration_label="data-2",
        iteration_data={"testData": {"phone": "18800000002", "code": "222222"}},
    )
    step_store.update_step(
        "task_parameterized",
        1,
        iteration_index=2,
        status="passed",
        diagnostics={"expected": "18800000002"},
    )

    state = step_store.read("task_parameterized")
    first_phone = next(
        step for step in state["steps"]
        if step["iteration_index"] == 1 and step["step_index"] == 1
    )
    second_phone = next(
        step for step in state["steps"]
        if step["iteration_index"] == 2 and step["step_index"] == 1
    )
    assert first_phone["diagnostics"]["expected"] == "18800000001"
    assert second_phone["diagnostics"]["expected"] == "18800000002"
    assert state["iterations"][0]["status"] == "passed"
    assert state["iterations"][1]["status"] == "running"
    assert state["iterations"][0]["data"]["testData"]["code"] == "111111"
    assert state["iterations"][1]["data"]["testData"]["code"] == "222222"
