from backend import executor, feishu


def test_enabled_bots_come_from_runnergo_third_party_integrations(monkeypatch):
    captured = {}

    class Response:
        text = '{"bots":[]}'

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "bots": [{
                    "type": "feishu",
                    "name": "global-quality-bot",
                    "webhook_url": "https://example.test/feishu",
                    "enabled": True,
                }],
            }

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        captured.update({
            "url": url,
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
        })
        return Response()

    monkeypatch.setattr(feishu.requests, "get", fake_get)

    bots = feishu._enabled_bots()

    assert bots[0]["name"] == "global-quality-bot"
    assert captured["url"].endswith("/notice/internal/enabled_webhook_bots")
    assert captured["headers"]["X-Agent-Token"] == feishu.settings.TEST_DATA_CENTER_AGENT_TOKEN
    assert captured["timeout"] == 5
    assert captured["allow_redirects"] is False


def test_enabled_bots_fall_back_to_mysql_when_management_bridge_is_unavailable(monkeypatch):
    fallback = [{
        "id": "notice-db",
        "type": "feishu",
        "name": "数据库通知",
        "webhook_url": "https://example.test/db",
    }]

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("management bridge unavailable")

    monkeypatch.setattr(feishu.requests, "get", unavailable)
    monkeypatch.setattr(feishu, "_enabled_bots_from_mysql", lambda: fallback)

    assert feishu._enabled_bots() == fallback


def test_notification_targets_are_safe_and_selected_by_id():
    bots = [
        {
            "id": "notice-a",
            "type": "feishu",
            "name": "质量群",
            "webhook_url": "https://example.test/secret-webhook-a",
            "secret": "secret-a",
            "enabled": True,
            "created_at": "2026-08-13 12:00:00",
        },
        {
            "id": "notice-b",
            "type": "wechat",
            "name": "研发群",
            "webhook_url": "https://example.test/secret-webhook-b",
            "enabled": True,
        },
    ]

    targets = feishu.notification_targets(bots)
    selected = feishu.select_bots(["notice-b"], bots)

    assert targets == [{
        "id": "notice-a",
        "type": "feishu",
        "name": "质量群",
        "created_at": "2026-08-13 12:00:00",
    }, {
        "id": "notice-b",
        "type": "wechat",
        "name": "研发群",
        "created_at": "",
    }]
    assert "webhook_url" not in targets[0]
    assert "secret" not in targets[0]
    assert [item["id"] for item in selected] == ["notice-b"]


def test_send_card_to_bots_only_sends_selected_bot(monkeypatch):
    posted_urls = []

    class Response:
        status_code = 200
        text = '{"ok":true}'

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(url, **_kwargs):
        posted_urls.append(url)
        return Response()

    monkeypatch.setattr(feishu.requests, "post", fake_post)
    monkeypatch.setattr(feishu, "_record_notification", lambda *_args, **_kwargs: None)

    result = feishu.send_card_to_bots(
        {
            "id": "report-1",
            "project_id": "project-1",
            "module": "ui",
            "status": "success",
            "passed": 1,
            "failed": 0,
            "total": 1,
        },
        [{
            "id": "notice-b",
            "type": "wechat",
            "name": "研发群",
            "webhook_url": "https://example.test/selected",
        }],
    )

    assert posted_urls == ["https://example.test/selected"]
    assert result["results"][0]["id"] == "notice-b"
    assert result["results"][0]["success"] is True


def test_finished_manual_and_scheduled_reports_use_unified_integrations(monkeypatch):
    sent = []
    monkeypatch.setattr(feishu, "send_card", lambda task: sent.append(task) or {"ok": True})

    manual = {"id": "manual-report", "status": "success", "triggered_by": "manual"}
    scheduled = {
        "id": "scheduled-report",
        "status": "failed",
        "triggered_by": "cron:job-1",
        "scheduled_job_id": "job-1",
        # 旧定时任务字段即使关闭，也不再阻止统一第三方集成通知。
        "notify_on_success": 0,
        "notify_on_failure": 0,
        "notification_type": "",
    }

    executor._send_automatic_report_notification(manual, "success", "/report/manual")
    executor._send_automatic_report_notification(scheduled, "failed", "/report/scheduled")
    executor._send_automatic_report_notification(scheduled, "stopped", "/report/stopped")

    assert [item["id"] for item in sent] == ["manual-report", "scheduled-report"]
    assert sent[0]["report_url"] == "/report/manual"
    assert sent[1]["report_url"] == "/report/scheduled"
