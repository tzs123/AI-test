import os

import pytest

from backend import settings
from backend.cases import service as case_service


def _configure_project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        case_service.project_service,
        "get_project",
        lambda _project_id: {"id": "default", "case_dir": "cases"},
    )


def test_case_can_be_renamed_to_chinese_filename(tmp_path, monkeypatch):
    _configure_project(tmp_path, monkeypatch)
    original = "flow_version: 2\nname: old\nsteps: []\n"
    updated = "flow_version: 2\nname: 中文登录用例\nsteps: []\n"
    case_service.save_case("default", "ui", "test_login.yaml", original)

    target = case_service.rename_case(
        "default", "ui", "test_login.yaml", "中文登录用例.yaml", updated
    )

    assert os.path.basename(target) == "中文登录用例.yaml"
    assert not (tmp_path / "cases" / "ui" / "test_login.yaml").exists()
    assert (tmp_path / "cases" / "ui" / "中文登录用例.yaml").read_text(
        encoding="utf-8"
    ) == updated
    assert not (tmp_path / "tests" / "ui" / "test_login.py").exists()
    generated = (tmp_path / "tests" / "ui" / "中文登录用例.py").read_text(
        encoding="utf-8"
    )
    assert "cases/ui/中文登录用例.yaml" in generated


def test_case_rename_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    _configure_project(tmp_path, monkeypatch)
    case_service.save_case("default", "ui", "old.yaml", "name: old\n")
    case_service.save_case("default", "ui", "已存在.yaml", "name: existing\n")

    with pytest.raises(FileExistsError):
        case_service.rename_case(
            "default", "ui", "old.yaml", "已存在.yaml", "name: changed\n"
        )

    assert (tmp_path / "cases" / "ui" / "old.yaml").exists()
    assert (tmp_path / "cases" / "ui" / "已存在.yaml").read_text(
        encoding="utf-8"
    ) == "name: existing\n"
