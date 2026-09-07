from backend import db, settings
from backend.web_recorder import (
    _delete_recording_draft,
    _get_recording_draft,
    _latest_recording_draft,
    _public_recording_draft,
    _update_recording_draft_steps,
    _upsert_recording_draft,
    mark_stale_recording_drafts_interrupted,
)


def _init_temp_db(tmp_path, monkeypatch):
    root = tmp_path / "auto-test"
    runtime = root / "runtime"
    monkeypatch.setattr(settings, "ROOT", str(root))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(settings, "DB_PATH", str(runtime / "platform.db"))
    monkeypatch.setattr(settings, "RESULTS_DIR", str(root / "results"))
    monkeypatch.setattr(settings, "REPORT_DIR", str(root / "reports"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(root / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(root / "logs"))
    db.init_db()


def test_recording_draft_round_trip_and_recovery(tmp_path, monkeypatch):
    _init_temp_db(tmp_path, monkeypatch)
    first_steps = [{"id": "step_1", "action": "goto", "url": "https://example.com"}]

    _upsert_recording_draft(
        "wrec_test",
        "default",
        "https://example.com",
        {"width": 1280, "height": 720},
        first_steps,
        {
            "cookies": [{"name": "sid", "value": "secret", "domain": "example.com", "path": "/"}],
            "origins": [{"origin": "https://example.com", "localStorage": [{"name": "token", "value": "abc"}]}],
        },
        "recording",
        "https://example.com/form",
    )

    draft = _get_recording_draft("wrec_test")
    assert draft["viewport"] == {"width": 1280, "height": 720}
    assert draft["steps"] == first_steps
    assert draft["storage_state"]["cookies"][0]["name"] == "sid"
    public = _public_recording_draft(draft)
    assert "storage_state" not in public
    assert public["auth_state_saved"] is True
    assert draft["step_count"] == 1
    assert _latest_recording_draft("default")["id"] == "wrec_test"

    mark_stale_recording_drafts_interrupted()
    assert _get_recording_draft("wrec_test")["status"] == "interrupted"

    updated_steps = [*first_steps, {"id": "step_2", "action": "click"}]
    updated = _update_recording_draft_steps("wrec_test", updated_steps)
    assert updated["steps"] == updated_steps
    assert updated["step_count"] == 2

    assert _delete_recording_draft("wrec_test") is True
    assert _get_recording_draft("wrec_test") is None
    assert _delete_recording_draft("wrec_test") is False
