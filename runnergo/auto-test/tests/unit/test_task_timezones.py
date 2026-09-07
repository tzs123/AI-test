from backend import app, feishu
from backend.time_utils import localize_task_timestamps, localize_utc_timestamp


def test_utc_task_time_is_displayed_in_asia_shanghai():
    assert localize_utc_timestamp("2026-07-17 17:12:52") == "2026-07-18 01:12:52"

    task = localize_task_timestamps({
        "started_at": "2026-07-17 17:12:52",
        "finished_at": "2026-07-17 17:12:57",
    })
    assert task["started_at"] == "2026-07-18 01:12:52"
    assert task["finished_at"] == "2026-07-18 01:12:57"
    assert localize_task_timestamps(task) == task


def test_task_api_normalization_localizes_existing_utc_rows():
    task = app._normalize_task_report({
        "id": "timezone-test",
        "started_at": "2026-07-17 17:12:52",
        "finished_at": "2026-07-17 17:12:57",
        "report_url": "/report/tasks/timezone-test/report/",
    })

    assert task["started_at"] == "2026-07-18 01:12:52"
    assert task["finished_at"] == "2026-07-18 01:12:57"
    assert task["time_zone"] == "Asia/Shanghai"


def test_feishu_card_localizes_raw_utc_task_time(monkeypatch):
    sent = {}

    class Response:
        text = '{"ok":true}'
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(_url, json=None, timeout=None, headers=None, allow_redirects=None):
        sent["body"] = json
        sent["allow_redirects"] = allow_redirects
        return Response()

    monkeypatch.setattr(feishu, "_enabled_bots", lambda: [{
        "type": "feishu",
        "name": "global-quality-bot",
        "webhook_url": "https://example.test/webhook",
        "enabled": True,
    }])
    monkeypatch.setattr(feishu.requests, "post", fake_post)

    result = feishu.send_card({
        "id": "timezone-test",
        "project_id": "project-test",
        "module": "ui",
        "status": "success",
        "passed": 1,
        "failed": 0,
        "total": 1,
        "started_at": "2026-07-17 17:12:52",
        "finished_at": "2026-07-17 17:12:57",
    })

    assert result == {"ok": True}
    assert sent["allow_redirects"] is False
    time_element = sent["body"]["card"]["elements"][4]
    assert "2026-07-18 01:12:52" in time_element["text"]["content"]
    assert "2026-07-18 01:12:57" in time_element["text"]["content"]
