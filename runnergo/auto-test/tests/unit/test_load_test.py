import copy
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from backend import load_test
from backend.load_test import (
    BatchDeleteLoadRunsIn,
    LoadRunIn,
    LoadRunState,
    _allocate_vu_shards,
    _enrich_stored_summary,
    _flow_for_iteration,
    _materialize_test_objects,
    _merge_distributed_event,
    _normalize_config,
    _normalize_transaction_defs,
    _percentile,
    _planned_vum,
    _prepare_distributed_run,
    _resource_summary,
    classify_flow,
    export_run,
)
from core.mixed_steps import RuntimeDataContext, execute_api_step
from core.web_flow_runner import WebFlowRunner


def test_batch_delete_runs_removes_terminal_rows_and_cached_states(monkeypatch):
    state = LoadRunState(
        run_id="run-terminal",
        project_id="project-1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 1, "duration_seconds": 1},
    )
    state.status = "completed"
    calls = []

    def fake_execute(sql, params=(), fetch=False):
        calls.append((sql, params, fetch))
        if fetch:
            return [{"id": "run-terminal", "status": "completed"}]
        return []

    monkeypatch.setattr(load_test.db, "execute", fake_execute)
    monkeypatch.setattr(load_test, "_RUNS", {"run-terminal": state})

    result = load_test.batch_delete_runs(BatchDeleteLoadRunsIn(
        project_id="project-1",
        run_ids=["run-terminal", "missing", "run-terminal"],
    ))

    assert result["deleted"] == 1
    assert result["missing_ids"] == ["missing"]
    assert "run-terminal" not in load_test._RUNS
    assert calls[0][1] == ("project-1", "run-terminal", "missing")
    assert calls[1][1] == ("project-1", "run-terminal")


def test_batch_delete_runs_rejects_running_rows(monkeypatch):
    state = LoadRunState(
        run_id="run-running",
        project_id="project-1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 1, "duration_seconds": 1},
    )
    state.status = "running"

    monkeypatch.setattr(
        load_test.db,
        "execute",
        lambda *_args, fetch=False, **_kwargs: (
            [{"id": "run-running", "status": "running"}] if fetch else []
        ),
    )
    monkeypatch.setattr(load_test, "_RUNS", {"run-running": state})

    with pytest.raises(HTTPException) as exc_info:
        load_test.batch_delete_runs(BatchDeleteLoadRunsIn(
            project_id="project-1",
            run_ids=["run-running"],
        ))

    assert exc_info.value.status_code == 409
    assert "run-running" in load_test._RUNS


def test_classify_flow_distinguishes_protocol_ui_and_mixed():
    protocol = classify_flow({"steps": [{"action": "api"}, {"action": "db"}]})
    assert protocol == {
        "recommended_mode": "protocol",
        "step_count": 2,
        "browser_steps": 0,
        "api_steps": 1,
        "database_steps": 1,
        "has_browser": False,
    }

    ui = classify_flow({"steps": [{"action": "goto"}, {"action": "click"}]})
    assert ui["recommended_mode"] == "ui"
    assert ui["browser_steps"] == 2

    mixed = classify_flow({"steps": [{"action": "goto"}, {"action": "api"}]})
    assert mixed["recommended_mode"] == "mixed"
    assert mixed["browser_steps"] == 1
    assert mixed["api_steps"] == 1


@pytest.mark.parametrize(
    ("filename", "expected_mode"),
    [
        ("protocol_full_chain_template.yaml", "protocol"),
        ("protocol_post_full_chain_template.yaml", "protocol"),
        ("page_full_chain_template.yaml", "ui"),
        ("mixed_full_chain_template.yaml", "mixed"),
        ("mixed_post_full_chain_template.yaml", "mixed"),
    ],
)
def test_quick_load_templates_are_runnable_flow_v2_scenarios(filename, expected_mode):
    path = Path(__file__).resolve().parents[2] / "cases" / "ui" / filename
    flow = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert flow["flow_version"] == 2
    assert flow["steps"]
    assert classify_flow(flow)["recommended_mode"] == expected_mode
    assert "replace_me" not in path.read_text(encoding="utf-8").lower()


def test_quick_template_replaces_recorded_url_with_exact_runtime_target():
    runner = object.__new__(WebFlowRunner)
    runner.recorded_base_url = "https://example.com"
    runner.runtime_base_url = "http://172.16.0.88:9527/api/orders?status=pending"

    assert runner._runtime_url("https://example.com") == (
        "http://172.16.0.88:9527/api/orders?status=pending"
    )


def test_protocol_api_step_applies_runtime_target_to_absolute_template_url(monkeypatch):
    captured = []

    def fake_execute(step, _context):
        captured.append(step["url"])
        return {
            "status": 200,
            "url": step["url"],
            "headers": {},
            "body": {},
            "text": "",
            "extracted": {},
        }

    monkeypatch.setattr("core.web_flow_runner.execute_api_step", fake_execute)
    runner = WebFlowRunner(None, {
        "base_url": "https://example.com",
        "_runtime_base_url": "http://127.0.0.1:18080/api/orders?status=pending",
    })

    runner.execute_step({"action": "api", "method": "GET", "url": "https://example.com"})

    assert captured == ["http://127.0.0.1:18080/api/orders?status=pending"]
    assert runner.last_locator_diagnostics["url"] == captured[0]


def test_historical_report_marks_template_target_mismatch():
    enriched = _enrich_stored_summary({
        "elapsed_seconds": 10,
        "completed": 2,
        "failed": 2,
        "timeline": [{"second": 1, "completed": 2, "failed": 2}],
        "recent_errors": [{
            "type": "ReadTimeout",
            "message": "HTTPSConnectionPool(host='example.com', port=443): Read timed out.",
        }],
    }, {
        "mode": "protocol",
        "base_url": "http://172.16.0.88:9527/api/orders",
        "ramp_up_seconds": 0,
        "duration_seconds": 10,
    })

    assert enriched["target_integrity"]["status"] == "MISMATCH"
    assert enriched["target_integrity"]["expected_target"] == "http://172.16.0.88:9527"
    assert enriched["target_integrity"]["observed_targets"][0]["target"] == "https://example.com"


def test_summary_records_actual_protocol_request_target():
    state = LoadRunState(
        run_id="run-target",
        project_id="default",
        case_file="api.yaml",
        scenario_name="api",
        mode="protocol",
        flow={"steps": []},
        config={
            "virtual_users": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 1,
            "ramp_down_seconds": 0,
            "base_url": "http://127.0.0.1:18080/api",
            "sla": {},
        },
    )
    state.started_clock = 1.0
    state.finished_clock = 2.0
    samples = []
    state.sample_callback = samples.append
    state.record_iteration(
        vu_id=1,
        iteration=1,
        duration_ms=20,
        success=True,
        elapsed_seconds=0.5,
        step_results=[{
            "index": 1,
            "id": "api",
            "name": "接口",
            "action": "api",
            "status": "passed",
            "duration": 0.02,
            "diagnostics": {"status": 200, "url": "http://127.0.0.1:18080/api?token=secret"},
        }],
    )

    summary = state.summary()

    assert summary["request_targets"] == [{"target": "http://127.0.0.1:18080", "count": 1}]
    assert summary["target_integrity"]["status"] == "MATCHED"
    assert samples[0]["step_results"][0]["diagnostics"]["url"] == "http://127.0.0.1:18080"
    assert "secret" not in str(samples[0])


def test_planned_vum_integrates_ramp_and_steady_active_users():
    assert _planned_vum({
        "virtual_users": 5,
        "ramp_up_seconds": 10,
        "duration_seconds": 60,
        "ramp_down_seconds": 10,
    }) == 6
    assert _planned_vum({
        "virtual_users": 200,
        "ramp_up_seconds": 0,
        "duration_seconds": 60,
        "ramp_down_seconds": 0,
    }) == 200


def test_self_hosted_vum_mode_does_not_read_or_consume_team_balance(monkeypatch):
    monkeypatch.setattr(load_test, "VUM_QUOTA_ENFORCED", False)
    monkeypatch.setattr(
        load_test.runnergo_test_objects,
        "get_team_vum",
        lambda *_args, **_kwargs: pytest.fail("self-hosted mode must not read VUM balance"),
    )
    monkeypatch.setattr(
        load_test.runnergo_test_objects,
        "consume_team_vum",
        lambda *_args, **_kwargs: pytest.fail("self-hosted mode must not consume VUM balance"),
    )

    quota = load_test._team_vum_quota("token", "team-1")
    consumed = load_test._consume_team_vum("token", "team-1", "run-1", 5000)

    assert quota == {
        "team_id": "team-1",
        "available_vum_num": None,
        "enforced": False,
    }
    assert consumed["consumed_vum_num"] == 0
    assert consumed["available_vum_num"] is None
    assert consumed["enforced"] is False


def test_managed_vum_mode_keeps_remote_quota_enforcement(monkeypatch):
    monkeypatch.setattr(load_test, "VUM_QUOTA_ENFORCED", True)
    monkeypatch.setattr(
        load_test.runnergo_test_objects,
        "get_team_vum",
        lambda token, team_id: {"team_id": team_id, "available_vum_num": 6000},
    )
    monkeypatch.setattr(
        load_test.runnergo_test_objects,
        "consume_team_vum",
        lambda token, team_id, source_id, vum_num: {
            "team_id": team_id,
            "source_id": source_id,
            "consumed_vum_num": vum_num,
            "available_vum_num": 1000,
            "already_consumed": False,
        },
    )

    quota = load_test._team_vum_quota("token", "team-1")
    consumed = load_test._consume_team_vum("token", "team-1", "run-1", 5000)

    assert quota["available_vum_num"] == 6000
    assert quota["enforced"] is True
    assert consumed["consumed_vum_num"] == 5000
    assert consumed["available_vum_num"] == 1000
    assert consumed["enforced"] is True


def test_distributed_protocol_mode_accepts_5000_vus_when_agents_have_capacity(monkeypatch):
    agents = [
        {"name": f"agent-{index}", "capacity_vus": 500, "available": True}
        for index in range(10)
    ]
    monkeypatch.setattr(load_test, "_load_agent_inventory", lambda **_kwargs: agents)

    config = _normalize_config(
        LoadRunIn(
            team_id="team-1",
            project_id="default",
            case_file="api.yaml",
            mode="protocol",
            execution_mode="auto",
            virtual_users=5000,
            duration_seconds=60,
        ),
        {"recommended_mode": "protocol", "has_browser": False},
        {"envs": {}, "base_url": ""},
        {"steps": [{"action": "api"}]},
    )

    assert config["execution_mode"] == "distributed"
    assert config["virtual_users"] == 5000
    assert config["distributed_capacity_vus"] == 5000


def test_distributed_mode_rejects_real_browser_flows():
    with pytest.raises(ValueError, match="真实页面"):
        _normalize_config(
            LoadRunIn(
                team_id="team-1",
                project_id="default",
                case_file="ui.yaml",
                mode="ui",
                execution_mode="distributed",
                virtual_users=10,
            ),
            {"recommended_mode": "ui", "has_browser": True},
            {"envs": {}, "base_url": ""},
            {"steps": [{"action": "goto"}]},
        )


def test_prepare_distributed_run_reserves_agents_and_builds_contiguous_shards(monkeypatch):
    agents = [
        {"name": "agent-a", "capacity_vus": 300, "available": True},
        {"name": "agent-b", "capacity_vus": 500, "available": True},
    ]
    reserved = []
    monkeypatch.setattr(load_test, "_load_agent_inventory", lambda **_kwargs: agents)
    monkeypatch.setattr(
        load_test.redis_queue,
        "reserve_load_agent",
        lambda name, run_id, ttl: reserved.append((name, run_id, ttl)) or True,
    )
    monkeypatch.setattr(load_test.redis_queue, "release_load_agent", lambda *_args: True)
    config = {
        "virtual_users": 600,
        "requested_agent_count": 0,
        "total_duration_seconds": 60,
    }

    _prepare_distributed_run(config, "run-1")

    assert config["agent_count"] == 2
    assert config["distributed_capacity_vus"] == 800
    assert config["distributed_agents"] == [
        {"name": "agent-a", "max_vus": 300, "assigned_vus": 300, "vu_start": 1, "vu_end": 300},
        {"name": "agent-b", "max_vus": 500, "assigned_vus": 300, "vu_start": 301, "vu_end": 600},
    ]
    assert [item[0] for item in reserved] == ["agent-a", "agent-b"]


def test_allocate_vu_shards_respects_uneven_agent_capacity():
    shards = _allocate_vu_shards(100, [
        {"name": "small", "capacity_vus": 1},
        {"name": "large", "capacity_vus": 100},
    ])

    assert [item["assigned_vus"] for item in shards] == [1, 99]
    assert shards[-1]["vu_end"] == 100


def test_merge_distributed_samples_updates_controller_metrics():
    state = LoadRunState(
        run_id="run-distributed",
        project_id="default",
        case_file="api.yaml",
        scenario_name="api",
        mode="protocol",
        flow={"steps": []},
        config={
            "virtual_users": 1000,
            "ramp_up_seconds": 0,
            "duration_seconds": 60,
            "ramp_down_seconds": 0,
            "sla": {},
        },
    )
    state.agent_ids = ["agent-a", "agent-b"]
    state.agent_active_vus = {"agent-a": 0, "agent-b": 0}
    state.started_clock = 100.0
    state.finished_clock = 101.0

    _merge_distributed_event(state, {
        "kind": "samples",
        "agent_id": "agent-a",
        "samples": [{
            "agent_id": "agent-a",
            "active_vus": 500,
            "vu_id": 1,
            "iteration": 1,
            "duration_ms": 120,
            "success": True,
            "elapsed_seconds": 0.5,
            "step_results": [],
        }],
    })
    _merge_distributed_event(state, {
        "kind": "shard_finished",
        "agent_id": "agent-a",
        "status": "completed",
        "completed": 1,
    })

    assert state.completed == 1
    assert state.peak_active_vus == 500
    assert state.agent_active_vus["agent-a"] == 0
    assert state.finished_agents == {"agent-a"}


def test_materialize_test_objects_does_not_mutate_source_flow():
    source = {
        "steps": [{
            "action": "api",
            "test_object_ref": {
                "team_id": "team-1",
                "target_id": "api-1",
                "target_type": "api",
                "name": "登录接口",
            },
            "headers": {"X-Case": "override"},
        }],
    }
    original = copy.deepcopy(source)
    bundle = {
        "team-1:api-1": {
            "target_type": "api",
            "step": {
                "action": "api",
                "method": "POST",
                "url": "https://example.com/login",
                "headers": {"Authorization": "secret", "X-Base": "1"},
            },
        },
    }

    runtime = _materialize_test_objects(source, bundle)

    assert source == original
    assert runtime["steps"][0]["method"] == "POST"
    assert runtime["steps"][0]["headers"] == {
        "Authorization": "secret",
        "X-Base": "1",
        "X-Case": "override",
    }
    assert "test_object_ref" not in runtime["steps"][0]


def test_flow_for_iteration_applies_runtime_data_to_copy_only():
    source = {
        "flow_version": 2,
        "base_url": "https://recorded.example.com",
        "variables": {"tenant": "default"},
        "steps": [{"action": "api", "url": "/health"}],
    }
    state = LoadRunState(
        run_id="run-1",
        project_id="default",
        case_file="health.yaml",
        scenario_name="health",
        mode="protocol",
        flow=source,
        config={
            "virtual_users": 2,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "runtime_variables": {"tenant": "runtime"},
            "data_rows": [{"account": "a"}, {"account": "b"}],
            "base_url": "https://runtime.example.com",
            "sla": {},
        },
    )

    runtime = _flow_for_iteration(state, vu_id=2, iteration=3)

    assert source["variables"] == {"tenant": "default"}
    assert "capture_screenshot" not in source["steps"][0]
    assert runtime["variables"]["tenant"] == "runtime"
    assert runtime["variables"]["account"] == "b"
    assert runtime["variables"]["vu_id"] == 2
    assert runtime["variables"]["iteration"] == 3
    assert runtime["_runtime_base_url"] == "https://runtime.example.com"
    assert runtime["steps"][0]["capture_screenshot"] is False


def _data_mode_state(mode: str) -> LoadRunState:
    return LoadRunState(
        run_id="run-mode",
        project_id="default",
        case_file="health.yaml",
        scenario_name="health",
        mode="protocol",
        flow={"steps": [{"action": "api", "url": "/health"}]},
        config={
            "virtual_users": 3,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "data_rows": [{"account": "a"}, {"account": "b"}, {"account": "c"}],
            "data_assignment_mode": mode,
            "sla": {},
        },
    )


def test_data_assignment_unique_fixes_row_per_vu_across_iterations():
    state = _data_mode_state("unique")
    accounts = [
        _flow_for_iteration(state, vu_id=2, iteration=it)["variables"]["account"]
        for it in range(1, 5)
    ]
    assert accounts == ["b", "b", "b", "b"]


def test_data_assignment_sequential_rotates_row_per_iteration():
    state = _data_mode_state("sequential")
    accounts = [
        _flow_for_iteration(state, vu_id=1, iteration=it)["variables"]["account"]
        for it in range(1, 5)
    ]
    assert accounts == ["a", "b", "c", "a"]


def test_data_assignment_random_returns_valid_row():
    state = _data_mode_state("random")
    for _ in range(20):
        account = _flow_for_iteration(state, vu_id=1, iteration=1)["variables"]["account"]
        assert account in {"a", "b", "c"}


def test_flow_for_iteration_dispatches_weighted_business_scenarios():
    state = LoadRunState(
        run_id="run-weighted",
        project_id="default",
        case_file="login.yaml",
        scenario_name="业务链路多场景（2 个）",
        mode="ui",
        flow={"steps": [{"action": "goto", "url": "/default"}]},
        flows={
            "login.yaml": {"name": "登录", "steps": [{"action": "goto", "url": "/login"}]},
            "checkout.yaml": {"name": "下单", "steps": [{"action": "goto", "url": "/checkout"}]},
        },
        config={
            "virtual_users": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "runtime_variables": {},
            "data_rows": [],
            "base_url": "https://runtime.example.com",
            "scenarios": [
                {"filename": "login.yaml", "name": "登录", "weight": 2},
                {"filename": "checkout.yaml", "name": "下单", "weight": 1},
            ],
        },
    )

    selected = [
        _flow_for_iteration(state, vu_id=vu_id, iteration=1)["_load_scenario_file"]
        for vu_id in range(1, 7)
    ]

    assert selected == [
        "login.yaml", "login.yaml", "checkout.yaml",
        "login.yaml", "login.yaml", "checkout.yaml",
    ]
    assert state.flows["login.yaml"]["steps"][0]["url"] == "/login"
    assert _flow_for_iteration(state, vu_id=1, iteration=1)["steps"][0]["disable_auto_page_skip"] is True


def test_full_chain_defaults_to_main_transaction_and_skipped_required_step_fails():
    flow = {
        "steps": [
            {"id": "open", "name": "打开", "action": "goto"},
            {"id": "submit", "name": "提交", "action": "click"},
            {"id": "optional", "name": "可选提示", "action": "click", "optional": True},
        ]
    }
    definitions = _normalize_transaction_defs(flow)
    assert definitions == [{
        "name": "主业务事务",
        "start_step": "",
        "end_step": "",
        "steps": ["open", "submit"],
        "auto_generated": True,
    }]
    state = LoadRunState(
        run_id="run-chain",
        project_id="default",
        case_file="chain.yaml",
        scenario_name="chain",
        mode="ui",
        flow=flow,
        config={
            "virtual_users": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "transaction_map": load_test._transaction_map(flow),
            "sla": {},
        },
    )
    state.started_clock = 100.0
    state.finished_clock = 102.0
    state.status = "completed"
    state.record_iteration(
        vu_id=1,
        iteration=1,
        duration_ms=200,
        success=True,
        step_results=[
            {"index": 1, "id": "open", "name": "打开", "action": "goto", "status": "passed", "duration": 0.1},
            {"index": 2, "id": "submit", "name": "提交", "action": "click", "status": "skipped", "duration": 0},
            {"index": 3, "id": "optional", "name": "可选提示", "action": "click", "status": "skipped", "duration": 0},
        ],
    )

    summary = state.summary()
    assert summary["passed"] == 0
    assert summary["failed"] == 1
    assert summary["error_types"] == {"BusinessChainIncomplete": 1}
    assert summary["chain_integrity"]["status"] == "INCOMPLETE"
    assert summary["chain_integrity"]["success_rate"] == 0
    assert summary["chain_integrity"]["skipped_required_steps"] == 1
    assert summary["business_transactions"][0]["failed"] == 1
    assert summary["business_transactions"][0]["pass_rate"] == 0


def test_optional_skipped_step_does_not_fail_full_chain():
    flow = {
        "steps": [
            {"id": "required", "name": "必需", "action": "goto"},
            {"id": "optional", "name": "可选", "action": "click", "optional": True},
        ]
    }
    state = LoadRunState(
        run_id="run-optional",
        project_id="default",
        case_file="optional.yaml",
        scenario_name="optional",
        mode="ui",
        flow=flow,
        config={
            "virtual_users": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "transaction_map": load_test._transaction_map(flow),
            "sla": {},
        },
    )
    state.started_clock = 100.0
    state.finished_clock = 102.0
    state.record_iteration(
        vu_id=1,
        iteration=1,
        duration_ms=100,
        success=True,
        step_results=[
            {"index": 1, "id": "required", "name": "必需", "action": "goto", "status": "passed", "duration": 0.1},
            {"index": 2, "id": "optional", "name": "可选", "action": "click", "status": "skipped", "duration": 0},
        ],
    )
    summary = state.summary()
    assert summary["passed"] == 1
    assert summary["failed"] == 0
    assert summary["chain_integrity"]["status"] == "COMPLETE"
    assert summary["business_transactions"][0]["passed"] == 1


def test_enrich_stored_report_reclassifies_skipped_chain_and_duration():
    summary = _enrich_stored_summary(
        {
            "elapsed_seconds": 89.18,
            "completed": 31,
            "passed": 31,
            "failed": 0,
            "error_rate": 0,
            "avg_ms": 12000,
            "p95_ms": 13000,
            "p99_ms": 14000,
            "tps": 0.35,
            "transactions": [
                {"count": 31, "passed": 31, "failed": 0, "skipped": 0},
                {"count": 31, "passed": 0, "failed": 0, "skipped": 31},
            ],
            "scenarios": [{"count": 31, "passed": 31, "failed": 0}],
        },
        {"ramp_up_seconds": 10, "duration_seconds": 60, "ramp_down_seconds": 10},
    )
    assert summary["passed"] == 0
    assert summary["failed"] == 31
    assert summary["error_rate"] == 100
    assert summary["chain_integrity"]["status"] == "INCOMPLETE"
    assert summary["business_transactions"][0]["failed"] == 31
    assert summary["planned_duration_seconds"] == 80
    assert summary["drain_duration_seconds"] == 9.18


def test_summary_exposes_percentiles_transactions_errors_and_sla():
    state = LoadRunState(
        run_id="run-2",
        project_id="default",
        case_file="mixed.yaml",
        scenario_name="mixed",
        mode="mixed",
        flow={"steps": []},
        config={
            "virtual_users": 2,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "sla": {"max_error_rate": 40, "max_p95_ms": 500},
        },
    )
    state.started_clock = 100.0
    state.finished_clock = 102.0
    state.status = "completed"
    state.record_iteration(
        vu_id=1,
        iteration=1,
        duration_ms=100,
        success=True,
        step_results=[{
            "index": 1,
            "id": "login",
            "name": "登录接口",
            "action": "api",
            "status": "passed",
            "duration": 0.08,
            "diagnostics": {"status": 200},
        }],
    )
    state.record_iteration(
        vu_id=2,
        iteration=1,
        duration_ms=300,
        success=False,
        error="assert failed",
        error_type="AssertionError",
        step_results=[{
            "index": 1,
            "id": "login",
            "name": "登录接口",
            "action": "api",
            "status": "failed",
            "duration": 0.2,
            "diagnostics": {"status": 500},
        }],
    )

    summary = state.summary()

    assert _percentile([100, 300], 0.5) == 200
    assert summary["completed"] == 2
    assert summary["error_rate"] == 50
    assert summary["p95_ms"] == 290
    assert summary["protocol_rps"] == 1
    assert summary["peak_tps"] == 2
    assert "metric_semantics" in summary
    assert summary["data_quality"]["status"] == "正常"
    assert summary["transactions"][0]["count"] == 2
    assert summary["transactions"][0]["status_codes"] == {"200": 1, "500": 1}
    assert summary["error_types"] == {"AssertionError": 1}
    assert summary["sla"]["status"] == "FAIL"


def test_normalize_request_url_collapses_ids_and_drops_query():
    from backend.load_test import _normalize_request_url

    # numeric / hex / UUID path segments collapse to {id}; query is dropped
    assert _normalize_request_url("http://h:9527/api/apply/123?x=1", "GET") == "GET http://h:9527/api/apply/{id}"
    assert _normalize_request_url("http://h:9527/api/apply/456?ref=abc", "GET") == "GET http://h:9527/api/apply/{id}"
    assert _normalize_request_url("https://h/static/app.js") == "https://h/static/app.js"
    assert _normalize_request_url("http://H:80/home?channelId=JDYFWH") == "http://h/home"
    assert _normalize_request_url("not a url", "GET") == ""
    assert _normalize_request_url("", "") == ""


def test_http_waterfall_exposes_per_url_page_component_breakdown():
    from backend.load_test import _normalize_request_url

    state = LoadRunState(
        run_id="run-page",
        project_id="default",
        case_file="ui.yaml",
        scenario_name="ui",
        mode="ui",
        flow={"steps": []},
        config={"virtual_users": 1, "duration_seconds": 1},
    )
    state.started_clock = 100.0
    state.finished_clock = 101.0
    state.status = "completed"
    # Two iterations hitting the same logical pages with different business IDs
    # and one static asset, plus a query-string page that must collapse.
    timings = [
        {"url": "http://172.16.0.88:9527/home?channelId=JDYFWH", "method": "GET",
         "resource_type": "document", "total_ms": 100, "ttfb_ms": 40, "dns_ms": 1,
         "connect_ms": 2, "tls_ms": 0, "receive_ms": 59},
        {"url": "http://172.16.0.88:9527/api/apply/123?token=abc", "method": "POST",
         "resource_type": "xhr", "total_ms": 200, "ttfb_ms": 150, "dns_ms": 1,
         "connect_ms": 2, "tls_ms": 0, "receive_ms": 49},
        {"url": "http://172.16.0.88:9527/api/apply/456", "method": "POST",
         "resource_type": "xhr", "total_ms": 300, "ttfb_ms": 250, "dns_ms": 1,
         "connect_ms": 2, "tls_ms": 0, "receive_ms": 49},
        {"url": "http://172.16.0.88:9527/static/app.js", "method": "GET",
         "resource_type": "script", "total_ms": 50, "ttfb_ms": 10, "dns_ms": 1,
         "connect_ms": 2, "tls_ms": 0, "receive_ms": 39},
    ]
    state.record_iteration(
        vu_id=1, iteration=1, duration_ms=650, success=True,
        step_results=[{
            "index": 1, "id": "goto-home", "name": "首页", "action": "goto",
            "status": "passed", "duration": 0.65,
            "diagnostics": {"status": 200, "url": "http://172.16.0.88:9527/home",
                            "http_timings": timings},
        }],
    )

    summary = state.summary()
    http = summary["http_waterfall"]

    # distinct logical URLs (home, api/apply/{id}, static/app.js) — not origins
    assert http["distinct_url_count"] == 3
    assert http["total_requests"] == 4
    by_url = {c["url"]: c for c in http["page_components"]}
    apply_key = "POST http://172.16.0.88:9527/api/apply/{id}"
    assert apply_key in by_url
    apply_comp = by_url[apply_key]
    assert apply_comp["count"] == 2  # both IDs collapsed
    assert apply_comp["total"]["p50"] == 200
    assert apply_comp["total"]["p95"] == 300
    assert apply_comp["method"] == "POST"
    assert apply_comp["resource_type"] == "xhr"
    # the home page kept its path, query dropped
    assert "GET http://172.16.0.88:9527/home" in by_url
    # normalized key is stable via the helper too
    assert _normalize_request_url("http://172.16.0.88:9527/api/apply/999", "POST") == apply_key
    # metric_semantics documents the new field
    assert "page_components" in summary["metric_semantics"]


def test_resource_monitoring_is_aggregated_and_exposed_in_summary():
    state = LoadRunState(
        run_id="run-monitor",
        project_id="default",
        case_file="api.yaml",
        scenario_name="api",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 1, "ramp_up_seconds": 0, "duration_seconds": 1, "ramp_down_seconds": 0, "sla": {}},
    )
    state.started_clock = 100.0
    state.finished_clock = 101.0
    state.record_resource_sample({"elapsed_seconds": 0.5, "cpu_percent": 20, "memory_percent": 40, "availability": {"cpu": True, "memory": True, "network": False, "jvm": False, "database": False}})
    state.record_resource_sample({"elapsed_seconds": 1.0, "cpu_percent": 60, "memory_percent": 50, "availability": {"cpu": True, "memory": True, "network": False, "jvm": False, "database": False}})

    summary = state.summary()

    assert summary["resource_monitoring"]["sample_count"] == 2
    assert summary["resource_monitoring"]["metrics"]["cpu_percent"] == {"avg": 40.0, "peak": 60.0}
    assert summary["resource_monitoring"]["availability"]["cpu"] is True


def test_distributed_monitor_event_is_merged():
    state = LoadRunState(
        run_id="run-monitor-distributed", project_id="default", case_file="api.yaml", scenario_name="api",
        mode="protocol", flow={"steps": []}, config={"virtual_users": 2, "duration_seconds": 1, "sla": {}},
    )
    _merge_distributed_event(state, {
        "kind": "monitor", "agent_id": "agent-a",
        "sample": {"elapsed_seconds": 1, "cpu_percent": 33, "availability": {"cpu": True}},
    })
    assert state.summary()["resource_monitoring"]["samples"][0]["agent_id"] == "agent-a"


def test_export_run_supports_csv_and_markdown(monkeypatch):
    state = LoadRunState(
        run_id="run-export", project_id="default", case_file="api.yaml", scenario_name="api",
        mode="protocol", flow={"steps": []}, config={"virtual_users": 1, "duration_seconds": 1, "sla": {}},
    )
    state.started_clock = 100.0
    state.finished_clock = 101.0
    state.status = "completed"
    state.record_iteration(vu_id=1, iteration=1, duration_ms=12, success=True, step_results=[])
    monkeypatch.setattr(load_test, "_RUNS", {"run-export": state})

    csv_response = export_run("run-export", "csv")
    markdown_response = export_run("run-export", "markdown")

    assert csv_response.media_type.startswith("text/csv")
    assert "summary" in csv_response.body.decode("utf-8")
    assert markdown_response.media_type.startswith("text/markdown")
    assert "LoadRunner" in markdown_response.body.decode("utf-8")


def test_export_run_rejects_unknown_format(monkeypatch):
    monkeypatch.setattr(load_test, "_RUNS", {})
    monkeypatch.setattr(load_test.db, "execute", lambda *_args, fetch=False, **_kwargs: ([{"id": "run-x", "config_json": "{}", "summary_json": "{}", "project_id": "p", "case_file": "x.yaml", "scenario_name": "x", "mode": "protocol", "status": "completed", "error_message": "", "created_at": "", "started_at": "", "finished_at": ""}] if fetch else []))
    with pytest.raises(HTTPException) as exc_info:
        export_run("run-x", "pdf")
    assert exc_info.value.status_code == 400


def test_summary_exposes_loadrunner_style_throughput_windows():
    state = LoadRunState(
        run_id="run-throughput",
        project_id="default",
        case_file="api.yaml",
        scenario_name="api",
        mode="protocol",
        flow={"steps": []},
        config={
            "virtual_users": 2,
            "ramp_up_seconds": 1,
            "duration_seconds": 3,
            "ramp_down_seconds": 1,
            "sla": {"min_tps": 1},
        },
    )
    state.started_clock = 100.0
    state.finished_clock = 105.0
    state.status = "completed"
    for index, elapsed in enumerate([0.2, 1.2, 1.4, 2.2, 4.4], start=1):
        state.record_iteration(
            vu_id=1,
            iteration=index,
            duration_ms=100,
            success=True,
            elapsed_seconds=elapsed,
            step_results=[],
        )

    summary = state.summary()

    assert summary["tps"] == 1
    assert summary["peak_tps"] == 2
    assert summary["stable_tps"] == 1
    assert summary["steady_window_seconds"] == 3
    assert summary["steady_completed"] == 3
    assert summary["data_quality"]["timeline_completed"] == summary["completed"]
    assert summary["sla"]["status"] == "PASS"


def test_yaml_load_profile_produces_business_transaction_metrics():
    flow = {
        "steps": [
            {"id": "open", "name": "打开首页", "action": "goto"},
            {"id": "login", "name": "登录接口", "action": "api"},
            {"id": "assert", "name": "登录断言", "action": "assert_text"},
        ],
        "load_profile": {
            "transactions": [
                {"name": "登录事务", "start_step": "login", "end_step": "assert"}
            ],
            "pacing_seconds": 2,
            "rendezvous_enabled": True,
        },
    }
    config = _normalize_config(
        type("Body", (), {
            "team_id": "team-1",
            "mode": "auto",
            "virtual_users": 2,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "ramp_down_seconds": 0,
            "think_time_ms": 0,
            "pacing_seconds": 0,
            "pacing_random_pct": 0,
            "rendezvous_enabled": False,
            "rendezvous_timeout_seconds": 30,
            "iterations_per_user": 0,
            "headless": True,
            "env": "test",
            "base_url": "",
            "runtime_variables": {},
            "data_rows": [],
            "sla": {},
        })(),
        {"recommended_mode": "mixed", "has_browser": True},
        {"envs": {}, "base_url": "https://example.test"},
        flow,
    )
    state = LoadRunState(
        run_id="run-txn",
        project_id="default",
        case_file="login.yaml",
        scenario_name="login",
        mode="mixed",
        flow=flow,
        config=config,
    )
    state.started_clock = 100.0
    state.finished_clock = 102.0
    state.status = "completed"
    state.record_iteration(
        vu_id=1,
        iteration=1,
        duration_ms=250,
        success=True,
        step_results=[
            {"index": 2, "id": "login", "name": "登录接口", "action": "api", "status": "passed", "duration": 0.1},
            {"index": 3, "id": "assert", "name": "登录断言", "action": "assert_text", "status": "passed", "duration": 0.05},
        ],
    )

    summary = state.summary()

    assert config["pacing_seconds"] == 2
    assert config["rendezvous_enabled"] is True
    assert summary["business_transactions"][0]["name"] == "登录事务"
    assert summary["business_transactions"][0]["count"] == 1
    assert summary["business_transactions"][0]["p95_ms"] == 150


def test_api_step_uses_virtual_user_http_session(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/health"
        text = '{"ok":true}'

        def json(self):
            return {"ok": True}

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    context = RuntimeDataContext()
    context.http_session = Session()

    result = execute_api_step({
        "action": "api",
        "method": "GET",
        "url": "https://example.test/health",
        "assert": {"status": 200},
    }, context)

    assert result["status"] == 200
    assert calls[0][0:2] == ("GET", "https://example.test/health")


def test_web_flow_runner_exposes_step_metrics_without_task_store(monkeypatch):
    class Response:
        status_code = 204
        headers = {}
        url = "https://example.test/health"
        text = ""

        def json(self):
            raise ValueError("no json")

    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)
    runner = WebFlowRunner(None, {
        "flow_version": 2,
        "steps": [{
            "id": "health",
            "name": "健康检查",
            "action": "api",
            "method": "GET",
            "url": "https://example.test/health",
            "assert": {"status": 204},
        }],
    })
    runner.runtime_context.http_session = type(
        "Session", (), {"request": lambda *_args, **_kwargs: Response()}
    )()

    runner.run()

    assert runner.step_results[0]["id"] == "health"
    assert runner.step_results[0]["status"] == "passed"
    assert runner.step_results[0]["duration"] >= 0
    assert runner.step_results[0]["diagnostics"]["status"] == 204


def test_public_run_redacts_secrets_and_compact_history_omits_data_rows():
    state = LoadRunState(
        run_id="run-secret",
        project_id="default",
        case_file="secret.yaml",
        scenario_name="secret",
        mode="protocol",
        flow={"steps": []},
        config={
            "mode": "protocol",
            "virtual_users": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 1,
            "ramp_down_seconds": 0,
            "think_time_ms": 0,
            "iterations_per_user": 1,
            "headless": True,
            "env": "test",
            "base_url": "https://example.test",
            "connection_reuse": True,
            "runtime_variables": {"token": "top-secret", "tenant": "demo"},
            "data_rows": [{"password": "hidden", "account": "u1"}],
            "sla": {},
        },
    )

    detail = state.public()
    history = state.public(compact=True)

    assert detail["config"]["runtime_variables"]["token"] == "***"
    assert detail["config"]["data_rows"][0]["password"] == "***"
    assert "runtime_variables" not in history["config"]
    assert "data_rows" not in history["config"]
    assert history["config"]["runtime_variable_count"] == 2
    assert history["config"]["data_row_count"] == 1


def test_enrich_stored_summary_backfills_loadrunner_metrics_for_old_reports():
    config = {
        "virtual_users": 2,
        "ramp_up_seconds": 1,
        "duration_seconds": 3,
        "ramp_down_seconds": 1,
    }
    summary = {
        "elapsed_seconds": 5.0,
        "completed": 5,
        "failed": 0,
        "tps": 1.0,
        "timeline": [
            {"second": 0, "completed": 1, "failed": 0},
            {"second": 1, "completed": 2, "failed": 0},
            {"second": 2, "completed": 1, "failed": 0},
            {"second": 3, "completed": 1, "failed": 0},
        ],
    }

    enriched = _enrich_stored_summary(summary, config)

    assert enriched["peak_tps"] == 2
    assert enriched["stable_tps"] == 1.33
    assert enriched["steady_window_seconds"] == 3
    assert enriched["steady_completed"] == 4
    assert enriched["data_quality"]["status"] == "正常"
    assert enriched["data_quality"]["timeline_completed"] == 5
    assert "peak_tps" in enriched["metric_semantics"]
    assert enriched["tps"] == 1.0


def test_load_run_in_accepts_goal_ip_spoofing_and_breakpoints():
    body = LoadRunIn(
        team_id="t1",
        project_id="p1",
        case_file="case.yaml",
        virtual_users=5,
        duration_seconds=10,
        goal={"target_tps": 100, "target_p95_ms": 500, "max_vus": 20},
        ip_spoofing={"proxies": ["http://proxy1:8080", "http://proxy2:8080"]},
        breakpoints=["step-3"],
    )
    assert body.goal["target_tps"] == 100
    assert body.ip_spoofing["proxies"] == ["http://proxy1:8080", "http://proxy2:8080"]
    assert body.breakpoints == ["step-3"]


def test_normalize_config_includes_advanced_fields(monkeypatch):
    monkeypatch.setattr(load_test, "_fetch_data_asset_rows", lambda *a, **kw: [])
    monkeypatch.setattr(load_test, "_fallback_data_rows", lambda n: [])
    monkeypatch.setattr(load_test, "validate_outbound_url", lambda url: url)
    monkeypatch.setattr(load_test, "_normalize_monitoring_config", lambda m: {})
    body = LoadRunIn(
        team_id="t1",
        project_id="p1",
        case_file="case.yaml",
        virtual_users=3,
        duration_seconds=10,
        goal={"target_tps": 50, "target_p95_ms": 300, "max_vus": 10},
        ip_spoofing={"proxies": ["http://proxy:8080"]},
        breakpoints=["*"],
    )
    flow = {"steps": [{"action": "api", "request": {"method": "GET", "url": "http://x"}}]}
    config = _normalize_config(body, flow, profile={})
    assert config["goal"]["target_tps"] == 50
    assert config["ip_spoofing"]["proxies"] == ["http://proxy:8080"]
    assert config["breakpoints"] == ["*"]


def test_vu_override_takes_precedence_over_ramp_schedule():
    state = LoadRunState(
        run_id="r1",
        project_id="p1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 10, "ramp_up_seconds": 10, "duration_seconds": 20, "ramp_down_seconds": 5},
    )
    state.started_clock = 1.0
    assert state.target_vus() == 1
    state.vu_override = 7
    assert state.target_vus() == 7
    state.vu_override = 0
    assert state.target_vus() == 0


def test_adjust_goal_vus_increases_when_tps_below_target():
    state = LoadRunState(
        run_id="r1",
        project_id="p1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 10, "duration_seconds": 60},
    )
    state.started_clock = 1.0
    state.completed = 10
    state.iteration_durations = [100, 100, 100]
    goal = {"target_tps": 100, "target_p95_ms": 0, "max_vus": 20}
    load_test._adjust_goal_vus(state, goal)
    assert state.vu_override is not None
    assert state.vu_override > 10


def test_adjust_goal_vus_decreases_when_p95_exceeds_target():
    state = LoadRunState(
        run_id="r1",
        project_id="p1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 10, "duration_seconds": 60},
    )
    state.started_clock = 1.0
    state.completed = 100
    state.iteration_durations = [2000] * 100
    goal = {"target_tps": 0, "target_p95_ms": 500, "max_vus": 20}
    load_test._adjust_goal_vus(state, goal)
    assert state.vu_override is not None
    assert state.vu_override < 10


def test_adjust_goal_vus_noop_without_targets():
    state = LoadRunState(
        run_id="r1",
        project_id="p1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 5, "duration_seconds": 60},
    )
    state.started_clock = 1.0
    load_test._adjust_goal_vus(state, {})
    assert state.vu_override is None


def test_debug_pause_and_resume_events():
    state = LoadRunState(
        run_id="r1",
        project_id="p1",
        case_file="case.yaml",
        scenario_name="case",
        mode="protocol",
        flow={"steps": []},
        config={"virtual_users": 1, "duration_seconds": 10},
    )
    assert not state.debug_pause_event.is_set()
    state.debug_pause_event.set()
    assert state.debug_pause_event.is_set()
    assert not state.debug_step_event.is_set()
    state.debug_step_event.set()
    assert state.debug_step_event.is_set()
    state.debug_pause_event.clear()
    state.debug_step_event.clear()
    assert not state.debug_pause_event.is_set()
    assert not state.debug_step_event.is_set()


def test_web_flow_runner_has_debug_attributes():
    runner = WebFlowRunner(None, {"steps": []})
    assert runner.debug_pause_event is None
    assert runner.debug_step_event is None


def test_distributed_mode_falls_back_for_browser_scenarios(monkeypatch):
    monkeypatch.setattr(load_test, "_fetch_data_asset_rows", lambda *a, **kw: [])
    monkeypatch.setattr(load_test, "_fallback_data_rows", lambda n: [])
    monkeypatch.setattr(load_test, "validate_outbound_url", lambda url: url)
    monkeypatch.setattr(load_test, "_normalize_monitoring_config", lambda m: {})
    body = LoadRunIn(
        team_id="t1",
        project_id="p1",
        case_file="case.yaml",
        virtual_users=5,
        duration_seconds=10,
        execution_mode="distributed",
    )
    flow = {"steps": [{"action": "click", "selector": "#btn"}]}
    config = _normalize_config(body, flow, profile={})
    assert config["execution_mode"] == "local"
    assert "浏览器" in config["distributed_fallback_reason"]


def test_multi_protocol_detection_in_config(monkeypatch):
    monkeypatch.setattr(load_test, "_fetch_data_asset_rows", lambda *a, **kw: [])
    monkeypatch.setattr(load_test, "_fallback_data_rows", lambda n: [])
    monkeypatch.setattr(load_test, "validate_outbound_url", lambda url: url)
    monkeypatch.setattr(load_test, "_normalize_monitoring_config", lambda m: {})
    body = LoadRunIn(
        team_id="t1",
        project_id="p1",
        case_file="case.yaml",
        virtual_users=3,
        duration_seconds=10,
        protocols={"http": True, "websocket": True, "database": True},
    )
    flow = {"steps": [
        {"action": "api", "request": {"method": "GET", "url": "http://x"}},
        {"action": "websocket", "url": "ws://x", "message": "hi"},
        {"action": "db", "query": "SELECT 1"},
    ]}
    config = _normalize_config(body, flow, profile={})
    assert config["multi_protocol_detected"] is True
    assert config["protocols"]["websocket"] is True
    assert config["protocols"]["database"] is True
