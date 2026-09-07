import json

from backend import settings, step_store
from backend.executor import _generate_builtin_report, _prefer_step_store_stats


def test_builtin_report_renders_step_screenshots(tmp_path):
    results_dir = tmp_path / "results"
    report_dir = tmp_path / "report"
    results_dir.mkdir()
    screenshot = results_dir / "step-shot.png"
    screenshot.write_bytes(b"fake-png-for-copy-test")
    execution_log = results_dir / "task-log.txt"
    execution_log.write_text("[步骤 1/1] ✅ 输入车牌号", encoding="utf-8")
    (results_dir / "case-result.json").write_text(
        json.dumps({
            "status": "passed",
            "name": "车牌号录制流程",
            "attachments": [{
                "name": "任务执行日志",
                "source": execution_log.name,
                "type": "text/plain",
            }],
            "steps": [{
                "name": "输入车牌号",
                "status": "passed",
                "attachments": [{
                    "name": "执行成功-输入车牌号",
                    "source": screenshot.name,
                    "type": "image/png",
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert _generate_builtin_report(str(results_dir), str(report_dir)) is True

    report = (report_dir / "index.html").read_text(encoding="utf-8")
    assert "车牌号录制流程" in report
    assert "输入车牌号" in report
    assert "执行成功-输入车牌号" in report
    assert "任务执行日志" in report
    assert "[步骤 1/1] ✅ 输入车牌号" in report
    assert "attachments/step-shot.png" in report
    assert "attachments/task-log.txt" in report
    assert (report_dir / "attachments" / "step-shot.png").exists()
    assert (report_dir / "attachments" / "task-log.txt").exists()


def test_ui_task_stats_prefer_step_store_counts_over_pytest_items(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    flow = {
        "name": "页面分支流程",
        "steps": [
            {"id": "one", "action": "click"},
            {"id": "two", "action": "click"},
            {"id": "three", "action": "assert_text"},
        ],
    }
    step_store.initialize("task-stats", flow)
    step_store.update_step("task-stats", 1, status="passed")
    step_store.update_step("task-stats", 2, status="skipped")
    step_store.update_step("task-stats", 3, status="passed")
    step_store.finalize("task-stats", "success")

    assert _prefer_step_store_stats(
        "task-stats",
        {"passed": 1, "failed": 0, "skipped": 0, "total": 1},
    ) == {"passed": 2, "failed": 0, "skipped": 1, "total": 3}
    assert _prefer_step_store_stats(
        "task-stats",
        {"passed": 2, "failed": 0, "skipped": 0, "total": 2},
        enabled=False,
    ) == {"passed": 2, "failed": 0, "skipped": 0, "total": 2}


def test_builtin_report_uses_authoritative_step_stats(tmp_path):
    results_dir = tmp_path / "results"
    report_dir = tmp_path / "report"
    results_dir.mkdir()
    (results_dir / "case-result.json").write_text(
        json.dumps({"status": "passed", "name": "页面分支流程", "steps": []}),
        encoding="utf-8",
    )

    assert _generate_builtin_report(
        str(results_dir),
        str(report_dir),
        stats={"passed": 45, "failed": 0, "skipped": 3, "total": 48},
    ) is True
    report = (report_dir / "index.html").read_text(encoding="utf-8")
    assert "<b>总数</b><br>48" in report
    assert "<b>通过</b><br>45" in report
    assert "<b>跳过</b><br>3" in report
