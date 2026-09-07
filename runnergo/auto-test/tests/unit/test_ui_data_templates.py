import json

import pytest
from fastapi import HTTPException

from backend import app, db, settings
from backend.cases import service as case_service


def _prepare_project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "runtime" / "platform.db"))
    monkeypatch.setattr(settings, "RESULTS_DIR", str(tmp_path / "reports" / "allure-results"))
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports" / "allure-report"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "IMPORTS_DIR", str(tmp_path / "imports"))
    db.init_db()
    db.execute(
        "INSERT INTO projects(id,name,description,base_url,case_dir,pages_dir,envs,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("default", "默认项目", "", "", "cases", "pages", json.dumps({}), "2026-07-28 10:00:00"),
    )
    case_service.save_case(
        "default",
        "ui",
        "test_login.yaml",
        """
flow_version: 2
steps:
  - id: phone-step
    action: fill
    name: 输入手机号
    element:
      name: 手机号
    value: '13800000000'
""".strip(),
    )


def test_ui_data_template_can_be_saved_listed_and_deleted(tmp_path, monkeypatch):
    _prepare_project(tmp_path, monkeypatch)

    created = app.ui_data_templates_create(app.UiDataTemplateIn(
        project_id="default",
        case_file="cases/ui/test_login.yaml",
        name="登录前置数据",
        runtimeOverride=[{
            "stepId": "phone-step",
            "stepPath": "steps.0",
            "runtimeValue": "${random_phone}",
        }],
    ))

    assert created["name"] == "登录前置数据"
    assert created["runtimeOverride"][0]["runtimeValue"] == "${random_phone}"

    listed = app.ui_data_templates_list("default", "cases/ui/test_login.yaml")
    assert [item["id"] for item in listed] == [created["id"]]

    app.ui_data_templates_delete(created["id"])
    assert app.ui_data_templates_list("default", "cases/ui/test_login.yaml") == []


def test_ui_data_template_rejects_unknown_runtime_field(tmp_path, monkeypatch):
    _prepare_project(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        app.ui_data_templates_create(app.UiDataTemplateIn(
            project_id="default",
            case_file="cases/ui/test_login.yaml",
            name="无效模板",
            runtimeOverride=[{
                "stepId": "missing-step",
                "runtimeValue": "bad",
            }],
        ))

    assert exc_info.value.status_code == 400
