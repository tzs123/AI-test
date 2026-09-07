from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_loadrunner_manual_is_not_exposed_in_the_ui_shell_navigation():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'data-page="loadrunner"' not in html
    assert "<span>LoadRunner 手册</span>" not in html
    assert "loadrunner:'loadrunner-manual'" in html
    assert "function loadrunner()" in html
    assert "本手册不执行、不分发、不集成破解程序" in html


def test_loadrunner_manual_route_is_allowed_by_backend():
    source = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")

    assert '"loadrunner-manual"' in source
