"""YAML 用例语义执行器：让浏览器视觉 Agent 按录制用例独立跑完整流程。

与平台执行器不同，本执行器不依赖固定 XPath：
定位顺序 = role/name -> label -> placeholder -> id -> css（语义优先）。
支持：goto / click / double_click / fill / popup_select_text / upload /
车牌虚拟键盘按键 / scroll / assert_error。
"""
from __future__ import annotations

import os
import random
import re
import string
import time
from typing import Any, List, Optional, Tuple, Dict

import yaml

from backend import settings
from backend.browser_agent.browser_controller import BrowserController


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_case(case_file: str) -> dict:
    """加载用例 YAML（支持绝对路径与 cases 目录相对路径）。"""
    case_file = str(case_file or "").strip()
    if not case_file:
        raise ValueError("需要 case_file 用例文件")
    candidates = []
    if os.path.isabs(case_file):
        candidates.append(case_file)
    else:
        for root in (os.path.join(settings.ROOT, "cases", "ui"),
                     os.path.join(settings.ROOT, "cases"),
                     "/app/cases/ui", "/app/cases"):
            candidates.append(os.path.join(root, case_file))
        if not case_file.endswith(".yaml") and not case_file.endswith(".yml"):
            candidates.append(os.path.join(os.path.dirname(candidates[0]), case_file + ".yaml"))
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    raise ValueError(f"用例文件不存在: {case_file}（查找过 {candidates}）")


def _random_phone() -> str:
    """生成新的合法手机号，避免复用已有申请记录导致页面流不一致。"""
    return "1" + random.choice("35789") + "".join(random.choice(string.digits) for _ in range(9))


def _resolve_runtime_values(case: dict) -> dict:
    """把录制用例中硬编码的手机号/验证码替换为本次运行值。

    优先使用环境变量 BROWSER_AGENT_PHONE / BROWSER_AGENT_CODE，
    默认生成全新随机手机号（验证码默认 123456）。
    """
    phone = os.environ.get("BROWSER_AGENT_PHONE") or _random_phone()
    code = os.environ.get("BROWSER_AGENT_CODE") or "123456"
    for step in case.get("steps") or []:
        if step.get("action") not in {"fill", "input"}:
            continue
        element = step.get("element") or {}
        label = str(element.get("label") or "")
        name = str(element.get("accessible_name") or element.get("name") or "")
        if label == "手机" or name == "手机":
            step["value"] = phone
        elif label == "验证码" or name == "验证码":
            step["value"] = code
    return case


_UPLOAD_INDEX = [0]


def _upload_files_for(step: dict) -> Tuple[List[str], Dict[str, Any]]:
    """上传文件：步骤自带 files 优先，否则轮询环境变量 BROWSER_AGENT_UPLOAD_FILES。

    返回 (resolved_paths, diagnostics)：执行容器感知的路径解析后返回实际可访问的
    文件路径列表，并附带每个原始路径的命中情况，便于上报错误提示。
    """
    # 复用 web_flow_runner 中与 docker-compose 挂载同步的共享目录常量与解析器
    try:
        from core.web_flow_runner import _resolve_upload_file_paths
    except Exception:  # pragma: no cover - 退化到简单逻辑
        _resolve_upload_file_paths = None

    raw_files = [str(f) for f in (step.get("files") or step.get("value") or []) if str(f).strip()]
    if not raw_files:
        # value 字段可能是逗号分隔的单字符串（老 YAML）
        v = str(step.get("value") or "").strip()
        if v and "," in v:
            raw_files = [p.strip() for p in v.split(",") if p.strip()]
    if not raw_files:
        env_str = os.environ.get("BROWSER_AGENT_UPLOAD_FILES", "").strip()
        if env_str:
            env_files = [f.strip() for f in env_str.split(",") if f.strip()]
            if env_files:
                index = _UPLOAD_INDEX[0] % len(env_files)
                _UPLOAD_INDEX[0] += 1
                raw_files = [env_files[index]]

    if _resolve_upload_file_paths is not None:
        return _resolve_upload_file_paths(raw_files)

    # 降级：简单存在性校验（本地调试用）
    diagnostics = {"originals": [], "container_dirs": [], "missing": [], "resolved_count": 0, "missing_count": 0}
    resolved = []
    for idx, p in enumerate(raw_files):
        info = {"index": idx, "original": p, "resolved": "", "mapped": False}
        if os.path.isfile(p):
            resolved.append(p)
            info["resolved"] = p
        else:
            diagnostics["missing"].append({"original": p, "basename": os.path.basename(p),
                                             "hint": f"文件不存在，请放入 /app/uploads 下"})
        diagnostics["originals"].append(info)
    diagnostics["resolved_count"] = len(resolved)
    diagnostics["missing_count"] = len(diagnostics["missing"])
    return resolved, diagnostics


def _locators_of(element: dict) -> dict:
    """提取语义定位信息（role/name/label/placeholder/id/css）。"""
    element = element or {}
    info: dict[str, str] = {
        "role": str(element.get("role") or ""),
        "name": str(element.get("accessible_name") or element.get("name") or ""),
        "label": str(element.get("label") or ""),
        "placeholder": "",
        "element_id": "",
        "css": "",
    }
    for locator in element.get("locators") or []:
        strategy = str(locator.get("strategy") or "")
        value = str(locator.get("value") or "")
        if strategy == "placeholder" and not info["placeholder"]:
            info["placeholder"] = value
        elif strategy == "id" and not info["element_id"]:
            info["element_id"] = value
        elif strategy == "css" and not info["css"]:
            info["css"] = value
    return info


def _find_locator(controller: BrowserController, element: dict):
    """语义优先定位元素（同名元素由控制器用可见唯一 id 消歧）。"""
    info = _locators_of(element)
    return controller.semantic_locator(**info)


def _exec_goto(controller: BrowserController, step: dict, url: str = "") -> dict:
    target = url or step.get("url") or ""
    opened = controller.open(target)
    return {"status": "success", "note": f"已打开 {opened.get('title', target)}"}


def _exec_click(controller: BrowserController, step: dict, *, double: bool = False) -> dict:
    element = step.get("element") or {}
    control_type = ((element.get("fingerprint") or {}).get("control_type") or "")
    name = str(step.get("name") or "")
    if control_type == "virtual_keyboard_key":
        keyboard_info = (element.get("fingerprint") or {}).get("keyboard") or {}
        key = keyboard_info.get("key") or step.get("value") or name
        kind = str(keyboard_info.get("kind") or "")
        if kind == "confirm":
            ok = controller.confirm_plate_keyboard()
        else:
            ok = controller.click_keyboard_key(key)
        return {"status": "success" if ok else "failed", "target": f"键盘按键 {key}",
                "error": "" if ok else f"未找到键盘按键 {key}"}
    if control_type == "plate_input":
        try:
            controller.locator("#app ul li").first.click()
            controller.wait(600)
            return {"status": "success", "target": "车牌号输入框"}
        except Exception:  # noqa: BLE001
            return {"status": "failed", "target": name, "error": "车牌号输入框点击失败"}
    locator = _find_locator(controller, element)
    if locator is None:
        # 兜底：按步骤名关键词（去除“点击 ”前缀）查找按钮
        text = name.replace("点击 ", "").strip()
        if text:
            locator = controller.semantic_locator(role="button", name=text)
    if locator is None:
        return {"status": "skipped", "target": name, "note": "元素不可见，页面感知跳步"}
    ok = controller.click_locator(locator, double=double)
    if not ok:
        # 动画/弹层遮挡时等待后重试一次
        controller.wait(400)
        ok = controller.click_locator(locator, double=double)
    return {"status": "success" if ok else "failed", "target": name,
            "error": "" if ok else "点击失败（已重试）"}


def _exec_fill(controller: BrowserController, step: dict) -> dict:
    element = step.get("element") or {}
    value = step.get("value")
    name = str(step.get("name") or "")
    if value is None:
        return {"status": "skipped", "target": name, "note": "无填充值"}
    locator = _find_locator(controller, element)
    if locator is None:
        return {"status": "skipped", "target": name, "note": "输入框不可见，页面感知跳步"}
    ok = controller.fill_locator(locator, value)
    return {"status": "success" if ok else "failed", "target": name,
            "value": str(value), "error": "" if ok else "填充失败"}


def _exec_popup_select_text(controller: BrowserController, step: dict) -> dict:
    value = str(step.get("value") or "")
    name = str(step.get("name") or "")
    if not value:
        return {"status": "skipped", "target": name, "note": "无选项值"}
    controller.wait(400)  # 等待弹框打开动画
    # 多级值（浙江省/杭州市/拱墅区）：逐级点击，最后点击末级
    segments = [seg for seg in value.split("/") if seg]
    clicked = False
    for seg in segments:
        if controller.click_popup_text(seg):
            clicked = True
            controller.wait(350)
    return {"status": "success" if clicked else "failed", "target": value,
            "error": "" if clicked else f"未找到选项 {value}"}


def _exec_upload(controller: BrowserController, step: dict) -> dict:
    files, diag = _upload_files_for(step)
    if not files:
        missing = diag.get("missing") or []
        if missing:
            names = "; ".join(m["basename"] for m in missing[:3])
            note = (
                f"缺少文件: {names}。请把文件放入仓库 auto-test/uploads/ 目录"
                f"（对应容器路径 /app/uploads/），步骤中填写容器路径。"
            )
        else:
            note = "无文件路径"
        return {"status": "skipped", "target": "上传文件", "note": note, "diagnostics": diag}
    missing = diag.get("missing") or []
    index = _UPLOAD_INDEX[0]
    _UPLOAD_INDEX[0] += 1
    ok = controller.upload_files(files, index=index)
    return {
        "status": "success" if ok else "failed",
        "target": "上传文件",
        "files": files,
        "input_index": index,
        "resolved": diag.get("resolved_count", 0),
        "skipped_missing": len(missing),
        "error": "" if ok else "未找到文件输入框",
        "diagnostics": diag,
    }


def _exec_scroll(controller: BrowserController, step: dict) -> dict:
    name = str(step.get("name") or "")
    delta = 300
    if "向下滚动" in name:
        digits = "".join(ch for ch in name.split("向下滚动")[-1] if ch.isdigit())
        if digits:
            delta = int(digits)
    ok = controller.scroll_by(delta)
    return {"status": "success" if ok else "skipped", "target": f"滚动 {delta}px"}


def _exec_assert_error(controller: BrowserController, step: dict) -> dict:
    error = controller.detect_error_toast()
    if error:
        return {"status": "failed", "target": "禁止出现任意页面提示",
                "error": f"页面出现错误提示: {error}"}
    return {"status": "success", "target": "禁止出现任意页面提示", "note": "无错误提示"}


_EXECUTORS = {
    "goto": _exec_goto,
    "open": _exec_goto,
    "click": _exec_click,
    "double_click": lambda c, s, **kw: _exec_click(c, s, double=True),
    "dblclick": lambda c, s, **kw: _exec_click(c, s, double=True),
    "fill": _exec_fill,
    "input": _exec_fill,
    "popup_select_text": _exec_popup_select_text,
    "upload": _exec_upload,
    "scroll": _exec_scroll,
    "assert_error": _exec_assert_error,
}


def run_browser_case(
    *,
    case_file: str = "",
    url: str = "",
    project_id: str = "",
    headless: bool = True,
    max_steps: int = 500,
    screenshots: bool = True,
) -> dict:
    """按 YAML 用例语义执行全流程。"""
    case = load_case(case_file)
    case = _resolve_runtime_values(case)
    steps = case.get("steps") or []
    if not steps:
        return {"status": "failed", "case_file": case_file, "error": "用例没有步骤"}
    base_url = url or case.get("base_url") or ""
    if not base_url:
        return {"status": "failed", "case_file": case_file, "error": "用例缺少 base_url"}
    controller = None
    trace: list[dict] = []
    shots: list[str] = []
    passed = skipped = failed = 0
    try:
        controller = BrowserController(headless=headless)
        for index, step in enumerate(steps[:max_steps], 1):
            action = str(step.get("action") or "")
            executor = _EXECUTORS.get(action)
            name = str(step.get("name") or "")
            entry: dict[str, Any] = {
                "step_no": index, "action": action, "name": name,
                "status": "pending", "started_at": _now(),
            }
            if executor is None:
                entry.update({"status": "skipped", "note": f"不支持的动作 {action}"})
                skipped += 1
                trace.append(entry)
                continue
            try:
                result = executor(controller, step, url=base_url) if action in {"goto", "open"} else executor(controller, step)
            except Exception as exc:  # noqa: BLE001
                result = {"status": "failed", "error": str(exc)[:500]}
            entry.update(result)
            if result.get("status") == "success":
                passed += 1
            elif result.get("status") == "skipped":
                skipped += 1
            else:
                failed += 1
            trace.append(entry)
            controller.wait(300)  # 页面稳定，避免跳转/弹框动画期点击被吞
            wait_after = float(step.get("wait_after") or 0)
            if wait_after:
                controller.wait(int(wait_after * 1000))
            if screenshots and index % 4 == 0:
                shots.append(controller.screenshot(f"case-{os.path.basename(case_file)}"))
        if screenshots:
            shots.append(controller.screenshot(f"case-{os.path.basename(case_file)}"))
        status = "success" if failed == 0 else "failed"
        try:
            from backend.agent.core.memory import remember_system_fact
            remember_system_fact(project_id or "default", "page", {
                "case_file": case_file,
                "url": base_url,
                "passed": passed, "skipped": skipped, "failed": failed,
                "finished_at": _now(),
            })
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": status,
            "case_file": case_file,
            "url": base_url,
            "total": len(steps[:max_steps]),
            "passed": passed,
            "skipped": skipped,
            "failed": failed,
            "trace": trace,
            "screenshots": shots,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "case_file": case_file,
            "url": base_url,
            "error": str(exc)[:1000],
            "trace": trace,
            "screenshots": shots,
        }
    finally:
        if controller:
            controller.close()
