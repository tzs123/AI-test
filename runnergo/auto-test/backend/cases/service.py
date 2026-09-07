"""UI 自动化用例 CRUD：以 YAML 文件存储，按项目组织。

目录约定:
  项目 case_dir=cases            -> cases/ui/*.yaml
  项目 case_dir=cases/<pid>      -> cases/<pid>/ui/*.yaml
对应 pytest 文件:
  默认项目: tests/ui/<stem>.py
  新项目:   tests/<pid>/ui/<stem>.py（用例与测试脚本同名映射）
"""
import os
import re
import yaml
from .. import settings
from ..safe_paths import safe_child_path
from ..projects import service as project_service


def _module_dir(project_id: str, module: str) -> str:
    if module != "ui":
        raise ValueError("UI 自动化模块仅支持 ui 用例")
    proj = project_service.get_project(project_id)
    if not proj:
        raise ValueError(f"项目不存在: {project_id}")
    case_dir = proj.get("case_dir") or "cases"
    d = safe_child_path(settings.ROOT, os.path.join(case_dir, module))
    os.makedirs(d, exist_ok=True)
    return d


def _abs(project_id: str, module: str, filename: str) -> str:
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    return safe_child_path(
        _module_dir(project_id, module),
        filename,
        allowed_suffixes=(".yaml", ".yml"),
        filename_only=True,
    )


def list_cases(project_id: str, module: str = "ui") -> list:
    d = _module_dir(project_id, module)
    files = sorted(
        f for f in os.listdir(d)
        if f.endswith((".yaml", ".yml"))
        and os.path.isfile(os.path.join(d, f))
        and not os.path.islink(os.path.join(d, f))
    )
    result = []
    for fn in files:
        path = os.path.join(d, fn)
        result.append({
            "name": fn,
            "module": module,
            "project_id": project_id,
            "size": os.path.getsize(path),
            "mtime": int(os.path.getmtime(path)),
            "relative": f"{project_service.get_project(project_id).get('case_dir','cases')}/{module}/{fn}",
        })
    return result


def get_case(project_id: str, module: str, filename: str) -> str:
    with open(_abs(project_id, module, filename), "r", encoding="utf-8") as f:
        return f.read()


def save_case(project_id: str, module: str, filename: str, content: str) -> str:
    """新增/更新 UI 用例 YAML，并自动创建对应 pytest 骨架。"""
    path = _abs(project_id, module, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _ensure_test_stub(project_id, module, os.path.basename(path), case_path=path)
    return path


def rename_case(
    project_id: str,
    module: str,
    filename: str,
    new_filename: str,
    content: str,
) -> str:
    """重命名 UI 用例并同步更新对应的 pytest 脚本。"""
    source_path = _abs(project_id, module, filename)
    target_path = _abs(project_id, module, new_filename)
    if source_path == target_path:
        return save_case(project_id, module, new_filename, content)
    if not os.path.exists(source_path):
        raise FileNotFoundError(source_path)
    if os.path.exists(target_path):
        raise FileExistsError(f"用例文件已存在: {os.path.basename(target_path)}")

    old_stem = os.path.basename(source_path).rsplit(".", 1)[0]
    new_stem = os.path.basename(target_path).rsplit(".", 1)[0]
    test_dir = os.path.join(settings.ROOT, "tests", module) if project_id == "default" \
        else os.path.join(settings.ROOT, "tests", project_id, module)
    old_test_path = safe_child_path(
        test_dir, f"{old_stem}.py", allowed_suffixes=(".py",), filename_only=True
    )
    new_test_path = safe_child_path(
        test_dir, f"{new_stem}.py", allowed_suffixes=(".py",), filename_only=True
    )
    if os.path.exists(new_test_path):
        raise FileExistsError(f"执行脚本已存在: {os.path.basename(new_test_path)}")

    with open(source_path, "r", encoding="utf-8") as handle:
        original_content = handle.read()
    test_renamed = False
    os.rename(source_path, target_path)
    try:
        if os.path.exists(old_test_path):
            os.rename(old_test_path, new_test_path)
            test_renamed = True
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        _ensure_test_stub(
            project_id, module, os.path.basename(target_path), case_path=target_path
        )
    except Exception:
        if test_renamed and os.path.exists(new_test_path):
            os.rename(new_test_path, old_test_path)
        if os.path.exists(target_path):
            os.rename(target_path, source_path)
            with open(source_path, "w", encoding="utf-8") as handle:
                handle.write(original_content)
        raise
    return target_path


def delete_case(project_id: str, module: str, filename: str) -> bool:
    path = _abs(project_id, module, filename)
    if os.path.exists(path):
        os.remove(path)
    # 同步删除对应的 pytest 脚本，避免执行"全部"时跑到已删除用例的旧 .py
    stem = filename.rsplit(".", 1)[0]
    if project_id == "default":
        test_dir = os.path.join(settings.ROOT, "tests", module)
    else:
        test_dir = os.path.join(settings.ROOT, "tests", project_id, module)
    test_path = safe_child_path(
        test_dir, f"{stem}.py", allowed_suffixes=(".py",), filename_only=True
    )
    if os.path.exists(test_path):
        os.remove(test_path)
    return os.path.exists(path) is False


def delete_cases(project_id: str, module: str, filenames: list[str]) -> list[str]:
    """批量删除 UI 用例，并同步删除对应 pytest 脚本。"""
    unique = list(dict.fromkeys(str(item or "").strip() for item in filenames))
    unique = [item for item in unique if item]
    # Validate every filename before mutating any file.
    paths = {item: _abs(project_id, module, item) for item in unique}
    deleted = []
    for filename, path in paths.items():
        if not os.path.exists(path):
            continue
        delete_case(project_id, module, filename)
        deleted.append(filename)
    return deleted


def _ensure_test_stub(project_id: str, module: str, filename: str, case_path: str = ""):
    """为新增 YAML 用例生成对应 pytest 文件（数据驱动），避免执行时找不到。
    默认项目：tests/ui/test_x.py
    新项目：  tests/{pid}/ui/test_x.py
    """
    stem = filename.rsplit(".", 1)[0]
    # 默认项目 -> tests/module/；新项目 -> tests/{pid}/module/
    if project_id == "default":
        test_dir = os.path.join(settings.ROOT, "tests", module)
    else:
        test_dir = os.path.join(settings.ROOT, "tests", project_id, module)
    os.makedirs(test_dir, exist_ok=True)
    test_path = safe_child_path(
        test_dir, f"{stem}.py", allowed_suffixes=(".py",), filename_only=True
    )

    # 浏览器录制用例使用通用 Flow Runner，不绑定任何具体业务 PageObject。
    try:
        source_path = case_path or _abs(project_id, module, filename)
        with open(source_path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and int(payload.get("flow_version") or 0) == 2:
        marker = "# AUTO-GENERATED WEB FLOW RUNNER"
        if os.path.exists(test_path):
            with open(test_path, "r", encoding="utf-8") as handle:
                existing = handle.read()
            if marker not in existing:
                return
        rel_flow = os.path.relpath(source_path, settings.ROOT).replace(os.sep, "/")
        function_stem = re.sub(r"[^A-Za-z0-9_]", "_", stem)
        if not function_stem or function_stem[0].isdigit():
            function_stem = f"flow_{function_stem}"
        feature_name = str(payload.get("name") or stem)
        template = f'''{marker}
import allure
import pytest

from core.web_flow_runner import load_web_flow, run_web_flow


FLOW_PATH = {rel_flow!r}


@allure.feature({feature_name!r})
@pytest.mark.ui
def test_{function_stem}(browser, runtime_data_row):
    flow = load_web_flow(FLOW_PATH)
    run_web_flow(browser, flow)
'''
        with open(test_path, "w", encoding="utf-8") as handle:
            handle.write(template)
        return

    if os.path.exists(test_path):
        return
    rel = f"cases/{module}/{filename}" if project_id == "default" \
        else f"{project_service.get_project(project_id).get('case_dir','cases')}/{module}/{filename}"
    template = f'''import pytest
import allure
from utils.yaml_loader import load_cases
from pages.jdy_submit_page import JdySubmitPage
from tests.ui.jdy_kb_flow import flow_home, flow_result, flow_fill, wait_url_contains


@allure.feature("{stem}")
@pytest.mark.ui
@pytest.mark.parametrize("case", load_cases("{rel}"))
def test_{stem}(case, page):
    """自动生成的 UI 用例默认执行真实今东车融申请链路。"""
    with allure.step(case.get("name", case.get("scenario", "{stem}"))):
        flow_home(page, case)
        flow_result(page, case)
        flow_fill(page, case)
        wait_url_contains(page, "/submit", timeout=15000)

        submit = JdySubmitPage(page)
        assert "/submit" in submit.get_current_url(), "应进入签署页"
        if case.get("expect_submit_page"):
            assert submit.has_text("申请信息"), "签署页应展示申请信息"
        if case.get("expect_borrower_name"):
            assert submit.verify_apply_info(case["expect_borrower_name"])
        if case.get("expect_steps"):
            actual = submit.get_step_status()
            for step, expected_status in case["expect_steps"].items():
                assert actual.get(step) == expected_status, \\
                    f"步骤'{{step}}'期望'{{expected_status}}'，实际'{{actual.get(step)}}'"
'''
    with open(test_path, "w", encoding="utf-8") as f:
        f.write(template)
