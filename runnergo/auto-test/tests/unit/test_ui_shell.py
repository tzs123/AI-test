from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "index.html"
STYLESHEET = ROOT / "templates" / "assets" / "auto-test.css"


def test_ui_shell_uses_local_compiled_stylesheet():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'href="assets/auto-test.css' in html
    assert "cdn.tailwindcss.com" not in html
    assert STYLESHEET.is_file()


def test_local_stylesheet_contains_page_shell_utilities():
    css = STYLESHEET.read_text(encoding="utf-8")

    assert ".flex{" in css
    assert ".h-screen{" in css
    assert ".overflow-hidden" in css
    assert ".p-6{" in css


def test_load_test_page_exposes_distributed_controls_and_floating_guide():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="loadTestExecution"' in html
    assert 'id="loadTestAgentCount"' in html
    assert 'id="loadTestGuidePanel"' in html
    assert "execution_mode: $('#loadTestExecution')" in html
    assert "千级并发配置方案" in html
    assert "panel.classList.toggle('viewport-clipped', clipped)" in html
    assert "toggle.style.right" in html


def test_load_test_page_exposes_one_click_quick_configuration():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="loadTestQuickMode"' in html
    assert 'id="loadTestQuickLevel"' in html
    assert 'id="loadTestQuickApiUrl"' in html
    assert 'id="loadTestQuickPageUrl"' in html
    assert "function applyLoadTestQuickConfig" in html
    assert "function confirmLoadTestQuickStart" in html
    assert "1 → 20 → 100 → 500 → 1000 → 5000 VU" in html
    assert "validateLoadTestQuickRun(caseFile,runtimeVariables)" in html
    assert "快速配置已发生变化" in html


def test_load_test_report_uses_unambiguous_counts_and_integrity_labels():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "SLA 通过率" in html
    assert "总执行 / 成功 / 失败" in html
    assert "数据完整性：" in html
    assert "不代表请求目标正确、SLA 通过或系统性能健康" in html
    assert "请求目标不一致" in html
    assert "本次结果不能作为被测系统容量结论" in html
    assert "有效报告整体 TPS" in html
    assert "target_integrity?.status !== 'MISMATCH'" in html


def test_self_hosted_load_test_ui_does_not_block_on_vum_balance():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "本地自托管模式不限额" in html
    assert "_loadTestCapabilities.vum?.enforced !== false" in html
    assert "if(enforced && planned>available)" in html
