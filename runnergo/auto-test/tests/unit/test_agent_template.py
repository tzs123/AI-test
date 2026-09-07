from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agent_flow_draft_uses_stable_step_renderer():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "function agentRenderFlowSteps(steps)" in html
    assert "${agentRenderFlowSteps(generatedFlow.steps)}" in html
    assert "agent-flow-step-value" in html
    assert "generatedFlow.steps||[]).map((step,index)=>`<div class=\"flex gap-3 border rounded p-2\"" not in html


def test_agent_execute_button_is_blocked_when_variables_are_missing():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "const hasMissingVariables = reviewVariables.length > 0;" in html
    assert "const canExecuteReady = task.status==='ready' && !hasMissingVariables" in html
    assert "缺少变量，不能执行" in html
    assert "(needsExecutionApproval||hasMissingVariables)&&reviewCaseFile" in html


def test_web_text_assertion_editor_supports_runtime_expression_and_operator():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="webStepExpected"' in html
    assert 'id="webStepAssertionOperator"' in html
    assert "step.expected=$('#webStepExpected')?.value||''" in html
    assert "step.operator=String($('#webStepAssertionOperator')?.value||step.operator||'contains')" in html
    assert "${apiName}" in html
    assert "${dbCount}" in html
    assert "const WEB_RUNTIME_REFERENCE_SCOPES" in html
    assert "if(!isWebRuntimeReference(match[1]))" in html
    assert "接口/数据库提取变量或固定期望值" in html
    assert "页面实际值" in html


def test_api_and_database_steps_use_runnergo_test_object_picker_and_simple_assertions():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "function loadRunnergoTestObjects(force=false)" in html
    assert "引用 API 测试模块中的${title}" in html
    assert "test_object_ref=ref" in html
    assert "请选择 API 测试模块中的接口测试对象" in html
    assert "请选择 API 测试模块中的 SQL 测试对象" in html
    assert "返回字段" in html
    assert "比较方式" in html
    assert "期望值 / 变量" in html
    assert "function readWebDataExtractions(kind)" in html
    assert "提取响应数据" in html
    assert "变量名" in html
    assert "返回字段" in html
    assert "保存完整结果到变量" not in html
    assert "webApiSaveAs" not in html
    assert "webDbSaveAs" not in html
    assert "webApiHeaders" not in html
    assert "webDbConnection" not in html
    assert "webApiExtract" not in html
    assert "webDbExtract" not in html


def test_new_scenes_no_longer_offer_reverse_page_extraction():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "onclick=\"addWebComponent('extract')\"" not in html
    assert '<option value="extract">页面取数</option>' not in html
    assert "['api','接口执行'], ['db','数据库查询'], ['extract','页面取数']" not in html
    assert "元素文本/数字断言" in html


def test_scene_components_support_explicit_before_or_after_insertion():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "function openWebRecorderInsertPositionDialog(step)" in html
    assert 'id="webInsertTarget"' in html
    assert 'name="webInsertPosition"' in html
    assert "const insertIndex=_webRecorderSteps.length" in html
    assert "await appendWebRecorderStep(step,insertIndex)" in html


def test_error_assertion_component_defaults_to_intercepting_any_visible_prompt():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "step={...step,name:'禁止出现任意页面提示',value:''};" in html
    assert "任意可见页面提示都会命中" in html
    assert "禁止出现的错误关键词（可留空表示任意错误提示）" not in html


def test_imported_yaml_recording_supports_continuous_insert_position():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="webRecordInsertTarget"' in html
    assert 'id="webRecordInsertBefore"' in html
    assert 'id="webRecordInsertAfter"' in html
    assert "function webRecorderRecordingInsertIndex()" in html
    assert "record_insert_index:recordInsertIndex" in html
    assert "body:JSON.stringify({record_insert_index:recordInsertIndex})" in html
    assert "setWebRecorderRecordingInsertControlsDisabled(true)" in html


def test_web_recorder_trackpad_scroll_is_accumulated_and_debounced():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "let _webRecorderWheelPending = null;" in html
    assert "let _webRecorderWheelTimer = null;" in html
    assert "const WEB_RECORDER_WHEEL_DEBOUNCE_MS = 100;" in html
    assert "const WEB_RECORDER_PAGE_SCROLL_MIN_DELTA = 96;" in html
    assert "function resetWebRecorderWheelState()" in html
    assert "function flushWebRecorderWheel()" in html
    assert "_webRecorderWheelPending.deltaX+=delta.deltaX;" in html
    assert "_webRecorderWheelPending.deltaY+=delta.deltaY;" in html
    assert "setTimeout(flushWebRecorderWheel,WEB_RECORDER_WHEEL_DEBOUNCE_MS)" in html
    assert "Math.abs(deltaY)<WEB_RECORDER_PAGE_SCROLL_MIN_DELTA" in html
    assert "Math.abs(deltaX)<WEB_RECORDER_PAGE_SCROLL_MIN_DELTA" in html

    wheel_handler = html.split("function webRecorderFrameWheel(event){", 1)[1].split(
        "function webRecorderFramePointerDown(event){", 1
    )[0]
    assert "_webRecorderBusy" not in wheel_handler
    assert "_webRecorderRefreshing" not in wheel_handler


def test_web_recorder_pending_scroll_is_cleared_on_context_changes():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    for function_name in (
        "webRecorderModeChanged",
        "navigateWebRecorder",
        "navigateWebRecorderHistory",
        "switchWebRecorderTab",
        "closeWebRecorderTab",
        "stopWebRecorder",
        "resumeWebRecorderDraft",
        "discardWebRecorderDraft",
    ):
        function_body = html.split(f"function {function_name}(", 1)[1].split("\n}", 1)[0]
        assert "resetWebRecorderWheelState();" in function_body
