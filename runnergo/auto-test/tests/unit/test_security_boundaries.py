import os
import json

import pytest
from fastapi import HTTPException

from backend import app, log_store, settings, url_security
from backend.cases import service as case_service
from backend.projects import service as project_service
from backend.safe_paths import ensure_allowed_path, safe_child_path


def test_safe_child_path_rejects_traversal_and_absolute_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError):
        safe_child_path(str(root), "../secret.yaml")
    with pytest.raises(ValueError):
        safe_child_path(str(root), "/etc/passwd")


def test_log_task_id_cannot_escape_logs_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "IMPORTS_DIR", str(tmp_path / "imports"))
    with pytest.raises(ValueError):
        log_store.read("../../etc/passwd")


def test_safe_child_path_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "linked")

    with pytest.raises(ValueError, match="符号链接|超出"):
        safe_child_path(str(root), "linked/secret.yaml")


def test_case_filename_cannot_escape_project_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        case_service.project_service,
        "get_project",
        lambda _project_id: {"id": "default", "case_dir": "cases"},
    )

    with pytest.raises(ValueError):
        case_service.save_case("default", "ui", "../escape.yaml", "name: bad")


def test_project_paths_are_confined_to_dedicated_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))

    assert project_service._normalize_case_dir("cases/demo", "demo") == "cases/demo"
    with pytest.raises(ValueError):
        project_service._normalize_case_dir("../../etc", "demo")


def test_allowed_path_must_be_under_allowlisted_root(tmp_path):
    allowed = tmp_path / "imports"
    allowed.mkdir()
    assert ensure_allowed_path(str(allowed / "file.txt"), [str(allowed)]).endswith("file.txt")
    with pytest.raises(ValueError):
        ensure_allowed_path("/etc/passwd", [str(allowed)])


def test_python_editing_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PYTHON_EDIT", False)
    with pytest.raises(HTTPException) as exc_info:
        app.pages_save("default", "evil.py", app.PageIn(content="raise SystemExit"))
    assert exc_info.value.status_code == 403


def test_ssrf_blocks_private_and_metadata_addresses(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(settings, "ALLOWED_URL_CIDRS", [])

    for url in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://198.18.1.181/",
    ):
        with pytest.raises(ValueError, match="受保护"):
            url_security.validate_outbound_url(url)


def test_ssrf_allows_only_explicit_private_cidr(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(settings, "ALLOWED_URL_CIDRS", ["172.16.0.88/32"])

    assert url_security.validate_outbound_url("http://172.16.0.88:9527/")
    with pytest.raises(ValueError):
        url_security.validate_outbound_url("http://172.16.0.89:9527/")


def test_reserved_example_test_host_does_not_require_dns(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(settings, "ALLOWED_URL_CIDRS", [])

    assert url_security.validate_outbound_url("https://example.test/replay") == (
        "https://example.test/replay"
    )


def test_project_environment_base_url_preserves_entered_path_and_query():
    project = project_service._normalize_project(
        {
            "id": "default",
            "name": "演示项目",
            "description": "",
            "base_url": "",
            "case_dir": "cases",
            "envs": json.dumps(
                {
                    "test": " http://172.16.0.88:9527/home?channelId=JDYFWH&productId=JDYPRD01 ",
                    "staging": "https://staging.example.com/app/#/login",
                },
                ensure_ascii=False,
            ),
        }
    )

    assert project["envs"]["test"] == "http://172.16.0.88:9527/home?channelId=JDYFWH&productId=JDYPRD01"
    assert project["envs"]["staging"] == "https://staging.example.com/app/#/login"
    assert project["base_url"] == project["envs"]["test"]


def test_project_update_allows_metadata_when_existing_private_url_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(settings, "ALLOWED_URL_CIDRS", [])

    existing_project = {
        "id": "default",
        "name": "国信小米",
        "description": "内置 UI 自动化用例",
        "base_url": "http://172.16.0.88:9527",
        "case_dir": "cases",
        "envs": {"test": "http://172.16.0.88:9527"},
    }
    updated_fields = {}

    monkeypatch.setattr(app.project_service, "get_project", lambda _pid: existing_project)

    def fake_update_project(project_id, **fields):
        updated_fields.update(fields)
        return {**existing_project, **fields, "id": project_id}

    monkeypatch.setattr(app.project_service, "update_project", fake_update_project)

    result = app.projects_update(
        "default",
        app.ProjectIn(
            name="国信小米",
            description="更新后的项目描述",
            base_url="http://172.16.0.88:9527",
            case_dir="cases",
            envs={"test": "http://172.16.0.88:9527"},
        ),
    )

    assert result["description"] == "更新后的项目描述"
    assert updated_fields["description"] == "更新后的项目描述"
    assert "defect_xlsx_path" not in updated_fields


def test_project_update_still_blocks_changed_private_url(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(settings, "ALLOWED_URL_CIDRS", [])
    monkeypatch.setattr(
        app.project_service,
        "get_project",
        lambda _pid: {
            "id": "default",
            "base_url": "http://172.16.0.88:9527",
            "envs": {"test": "http://172.16.0.88:9527"},
        },
    )
    with pytest.raises(HTTPException) as exc_info:
        app.projects_update(
            "default",
            app.ProjectIn(
                name="国信小米",
                base_url="http://172.16.0.89:9527",
                case_dir="cases",
                envs={"test": "http://172.16.0.88:9527"},
            ),
        )

    assert exc_info.value.status_code == 400
    assert "受保护" in exc_info.value.detail
