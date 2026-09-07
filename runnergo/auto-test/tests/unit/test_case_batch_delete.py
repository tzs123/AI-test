from backend import settings
from backend.cases import service as case_service


def test_batch_delete_removes_selected_cases_and_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        case_service.project_service,
        "get_project",
        lambda _project_id: {"id": "default", "case_dir": "cases"},
    )
    for filename in ("one.yaml", "two.yaml", "keep.yaml"):
        case_service.save_case("default", "ui", filename, "name: test\n")

    result = case_service.delete_cases("default", "ui", ["one.yaml", "two.yaml"])

    assert result == ["one.yaml", "two.yaml"]
    assert not (tmp_path / "cases" / "ui" / "one.yaml").exists()
    assert not (tmp_path / "cases" / "ui" / "two.yaml").exists()
    assert (tmp_path / "cases" / "ui" / "keep.yaml").exists()
