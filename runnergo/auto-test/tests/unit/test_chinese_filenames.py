import yaml

from backend.web_recorder import FlowSaveIn, save_web_flow, _safe_flow_filename


def test_safe_flow_filename_preserves_chinese():
    assert _safe_flow_filename("登录场景.yaml", "登录场景") == "登录场景.yaml"


def test_web_flow_save_renames_original_to_chinese(monkeypatch):
    saved = {}
    existing = yaml.safe_dump({
        "flow_version": 2,
        "name": "登录",
        "steps": [{"action": "goto", "url": "https://example.com"}],
        "metadata": {"created_at": "2026-01-01"},
    }, allow_unicode=True)
    monkeypatch.setattr(
        "backend.web_recorder.project_service.get_project",
        lambda project_id: {"id": project_id},
    )
    monkeypatch.setattr(
        "backend.web_recorder.case_service.get_case",
        lambda project_id, module, filename: existing
        if filename == "test_login.yaml" else (_ for _ in ()).throw(FileNotFoundError()),
    )

    def capture_rename(project_id, module, filename, new_filename, content):
        saved.update(filename=filename, new_filename=new_filename, content=content)
        return f"/tmp/{new_filename}"

    monkeypatch.setattr("backend.web_recorder.case_service.rename_case", capture_rename)
    result = save_web_flow(FlowSaveIn(
        project_id="default",
        name="登录流程",
        filename="登录场景.yaml",
        original_filename="test_login.yaml",
        steps=[{"action": "goto", "url": "https://example.com"}],
    ))

    assert saved["filename"] == "test_login.yaml"
    assert saved["new_filename"] == "登录场景.yaml"
    assert result["filename"] == "登录场景.yaml"
