import os
import sys
import time
import json
import pytest
import allure
from playwright.sync_api import sync_playwright
from playwright.sync_api import Page

from backend.url_security import guard_playwright_route


def _runtime_context_rows():
    raw = os.getenv("RUNTIME_CONTEXT_ROWS_JSON", "")
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def pytest_generate_tests(metafunc):
    rows = _runtime_context_rows()
    if len(rows) > 1 and "runtime_data_row" in metafunc.fixturenames:
        metafunc.parametrize(
            "runtime_data_row",
            [
                {
                    "index": index,
                    "count": len(rows),
                    "label": f"data-{index}",
                    "context": row,
                }
                for index, row in enumerate(rows, start=1)
            ],
            indirect=True,
            ids=[f"data-{index}" for index in range(1, len(rows) + 1)],
        )


@pytest.fixture(scope="function", autouse=True)
def runtime_data_row(request):
    parameter = getattr(request, "param", None)
    row = parameter.get("context") if isinstance(parameter, dict) else None
    if not isinstance(row, dict):
        yield
        return

    environment = {
        "RUNTIME_CONTEXT_JSON": json.dumps(row, ensure_ascii=False),
        "RUNTIME_DATA_ROW_INDEX": str(parameter.get("index") or 1),
        "RUNTIME_DATA_ROW_COUNT": str(parameter.get("count") or 1),
        "RUNTIME_DATA_ROW_LABEL": str(parameter.get("label") or "data-1"),
        "RUNTIME_DATA_ROW_JSON": json.dumps(row.get("dataAssets") or row.get("data") or {}, ensure_ascii=False),
    }
    previous = {name: os.environ.get(name) for name in environment}
    os.environ.update(environment)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ===== 项目级 pages 目录支持 =====
_project_id = os.getenv("PROJECT_ID", "")
if _project_id and _project_id != "default":
    _project_pages = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", _project_id)
    if os.path.isdir(_project_pages) and _project_pages not in sys.path:
        sys.path.insert(0, _project_pages)


# ===== UI 测试 fixture =====
def _chromium_launch_options():
    """Allow CI/developer machines to opt into an installed Chrome binary."""
    executable_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    if executable_path and os.path.isfile(executable_path):
        return {"executable_path": executable_path}
    return {}


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            **_chromium_launch_options(),
        )
        yield browser
        browser.close()


@pytest.fixture(scope="function")
def page(browser):
    context = browser.new_context(
        viewport={"width": 375, "height": 812},
        locale="zh-CN",
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    )
    context.route("**/*", guard_playwright_route)
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    yield page
    context.clear_cookies()
    context.close()


def _task_screenshot_dir() -> str:
    """当前执行任务截图目录（由执行器设置 TASK_ID 环境变量）。"""
    task_id = os.getenv("TASK_ID", "")
    if not task_id:
        return ""
    d = os.path.join("screenshots", task_id)
    os.makedirs(d, exist_ok=True)
    return d


# ===== 失败自动截图（allure + 任务目录） =====
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        page = item.funcargs.get("page")
        if page:
            try:
                img = page.screenshot()
                allure.attach(
                    img, name="失败截图", attachment_type=allure.attachment_type.PNG
                )
                shot_dir = _task_screenshot_dir()
                if shot_dir:
                    fname = f"{int(time.time())}_{item.name}.png"
                    with open(os.path.join(shot_dir, fname), "wb") as f:
                        f.write(img)
            except Exception:
                pass


# ===== 跑完汇总 + 可选上报 =====
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(terminalreporter.stats.get("failed", []))
    error = len(terminalreporter.stats.get("error", []))
    total = passed + failed + error

    print(f"\n{'=' * 20} 测试汇总 {'=' * 20}")
    print(f"总计: {total} | 通过: {passed} | 失败: {failed + error}")

@pytest.fixture(scope="function", autouse=True)
def screenshot_on_failure(request):

    page = request.getfixturevalue("page") if "page" in request.fixturenames else None

    yield

    rep = getattr(request.node, "rep_call", None)

    if rep and rep.failed and page:

        img = page.screenshot()

        allure.attach(
            img,
            name="failure screenshot",
            attachment_type=allure.attachment_type.PNG
        )
