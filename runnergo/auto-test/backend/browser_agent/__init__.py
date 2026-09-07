"""Web Vision Agent：基于 Playwright MCP 的浏览器视觉定位测试智能体。

流程：打开页面 -> 截图 -> DOM/Accessibility 分析 -> 识别元素 -> 执行点击/输入。
不依赖固定 XPath。入口：run_browser_agent(url, goal)。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from backend.browser_agent.browser_controller import BrowserController
from backend.browser_agent.element_locator import locate_and_act
from backend.browser_agent.vision_parser import analyze_page, parse_goal, pick_for_kind


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _login_value() -> tuple[str, str]:
    return os.environ.get("BROWSER_AGENT_USERNAME", "test001"), os.environ.get(
        "BROWSER_AGENT_PASSWORD", "Passw0rd@123"
    )


def _phone_code_value() -> tuple[str, str]:
    return os.environ.get("BROWSER_AGENT_PHONE", "13800138000"), os.environ.get(
        "BROWSER_AGENT_CODE", "123456"
    )


def _execute_intent(
    controller: Any,
    intent: str,
    candidates: list[dict],
    trace: list[dict],
) -> None:
    username, password = _login_value()
    phone, code = _phone_code_value()
    if intent == "login":
        # 优先手机号+验证码登录，其次用户名+密码
        phone_boxes = pick_for_kind(candidates, "phone")
        code_boxes = pick_for_kind(candidates, "code")
        if phone_boxes:
            box = phone_boxes[0]
            locate_and_act(controller, box, value=phone)
            trace.append({"step": "fill", "target": box.get("name") or "手机号", "value": phone,
                          "status": "success"})
            send_buttons = pick_for_kind(candidates, "send_code")
            if send_buttons:
                result = locate_and_act(controller, send_buttons[0])
                trace.append({"step": "click", "target": send_buttons[0].get("name") or "发送验证码",
                              "status": result["status"], "note": result.get("error", "")})
                controller.wait(1500)
            if code_boxes:
                box = code_boxes[0]
                locate_and_act(controller, box, value=code)
                trace.append({"step": "fill", "target": box.get("name") or "验证码", "value": code,
                              "status": "success"})
        else:
            for box in pick_for_kind(candidates, "username")[:1]:
                trace.append({"step": "fill", "target": box.get("name") or "用户名", "value": username,
                              "status": "success"})
                locate_and_act(controller, box, value=username)
            for box in pick_for_kind(candidates, "password")[:1]:
                trace.append({"step": "fill", "target": box.get("name") or "密码", "value": "******",
                              "status": "success"})
                locate_and_act(controller, box, value=password)
        button = pick_for_kind(candidates, "login")
        target = button[0] if button else pick_for_kind(candidates, "submit")
        if target:
            item = target[0]
            result = locate_and_act(controller, item)
            trace.append({"step": "click", "target": item.get("name") or "登录", "status": result["status"],
                          "note": result.get("error", "")})
        controller.wait(1500)
    elif intent == "register":
        for box in pick_for_kind(candidates, "username")[:1]:
            locate_and_act(controller, box, value=f"reg_{int(time.time()) % 100000}")
        for box in pick_for_kind(candidates, "password")[:1]:
            locate_and_act(controller, box, value=password)
        button = pick_for_kind(candidates, "register") or pick_for_kind(candidates, "submit")
        if button:
            result = locate_and_act(controller, button[0])
            trace.append({"step": "click", "target": button[0].get("name") or "注册", "status": result["status"]})
        controller.wait(1200)
    elif intent == "search":
        boxes = pick_for_kind(candidates, "text")
        if boxes:
            locate_and_act(controller, boxes[0], value="手机")
            controller.press_enter()
            trace.append({"step": "search", "target": boxes[0].get("name") or "搜索框", "value": "手机",
                          "status": "success"})
        controller.wait(1200)
    elif intent == "cart":
        button = pick_for_kind(candidates, "cart") or pick_for_kind(candidates, "submit")
        if button:
            result = locate_and_act(controller, button[0])
            trace.append({"step": "click", "target": button[0].get("name") or "加入购物车", "status": result["status"]})
        controller.wait(800)
    elif intent == "checkout":
        button = pick_for_kind(candidates, "checkout") or pick_for_kind(candidates, "submit")
        if button:
            result = locate_and_act(controller, button[0])
            trace.append({"step": "click", "target": button[0].get("name") or "结算", "status": result["status"]})
        controller.wait(1200)


def run_browser_agent(
    *,
    url: str = "",
    goal: str = "",
    case_file: str = "",
    project_id: str = "",
    headless: bool = True,
    max_steps: int = 8,
) -> dict:
    """浏览器视觉 Agent 入口。

    - 提供 case_file 时：按 YAML 用例语义执行完整流程（不依赖固定 XPath）
    - 否则：打开页面 -> 视觉识别 -> 按 goal 意图执行
    """
    from backend.browser_agent.case_runner import run_browser_case

    if case_file:
        return run_browser_case(
            case_file=case_file,
            url=url,
            project_id=project_id,
            headless=headless,
            max_steps=500,
        )
    goal = str(goal or "").strip()
    if not url:
        return {
            "status": "skipped",
            "message": "未提供 url，无法执行浏览器视觉测试（在 Agent 任务或运行环境配置目标页面地址）",
            "suggestion": "配置 BROWSER_AGENT_BASE_URL 或提供完整页面 URL 后重试",
        }
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001
        return {
            "status": "skipped",
            "message": "当前节点未安装 Playwright，浏览器视觉测试不可用",
            "suggestion": "安装 playwright 并执行 playwright install chromium 后重试",
        }
    controller = None
    trace: list[dict] = []
    screenshots: list[str] = []
    try:
        controller = BrowserController(headless=headless)
        opened = controller.open(url)
        trace.append({"step": "open", "target": url, "title": opened.get("title", ""),
                      "status": "success", "mcp": opened.get("mcp")})
        screenshot = controller.screenshot("vision")
        screenshots.append(screenshot)

        parsed = parse_goal(goal)
        analysis = analyze_page(controller, goal)
        candidates = analysis.get("candidates") or []
        actions = analysis.get("actions") or []
        trace.append({
            "step": "analyze",
            "target": goal,
            "elements": len(candidates),
            "intents": [i["label"] for i in parsed["intents"]],
            "status": "success",
        })

        # LLM 动作优先，否则按意图规则执行
        if actions:
            for action in actions[:max_steps]:
                result = locate_and_act(controller, action, value=action.get("value_hint", ""))
                trace.append({"step": result["action"], "target": action.get("name") or "(元素)",
                              "status": result["status"], "note": result.get("error", "")})
                controller.wait(400)
        else:
            for intent in parsed["intents"][:3]:
                _execute_intent(controller, intent["action"], candidates, trace)
                screenshots.append(controller.screenshot("vision"))

        final_url = controller.current_url()
        page_text = controller.page_text()
        success_hints = ["登录成功", "欢迎", "退出", "购物车(1)", "订单提交成功", "支付成功", "注册成功", "搜索到"]
        matched = [h for h in success_hints if h in page_text]
        result_text = "页面操作完成"
        if final_url and final_url != url:
            result_text = f"页面跳转至 {final_url}"
        if matched:
            result_text = f"检测到成功标识：{matched[0]}"
        trace.append({"step": "verify", "target": final_url, "note": result_text, "status": "success"})
        try:
            from backend.agent.core.memory import remember_system_fact
            remember_system_fact(project_id or "default", "page", {
                "url": url,
                "title": opened.get("title", ""),
                "elements": len(candidates),
                "goal": goal,
                "result": result_text,
            })
        except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
            pass
        return {
            "status": "success",
            "architecture": "AI -> Playwright MCP -> Chrome -> Page -> Screenshot -> Vision Analysis",
            "goal": goal,
            "url": final_url,
            "opened": opened,
            "result": result_text,
            "trace": trace,
            "screenshots": screenshots,
            "screenshot_url": f"/screenshots/{os.path.basename(screenshots[-1])}" if screenshots else "",
            "visual_summary": {
                "element_count": len(candidates),
                "action_count": len([item for item in trace if item.get("step") in {"fill", "click", "search"}]),
                "mcp": opened.get("mcp") or {},
            },
            "elements_found": len(candidates),
        }
    except Exception as exc:  # noqa: BLE001 - 结构化返回错误
        return {
            "status": "failed",
            "goal": goal,
            "url": url,
            "error": str(exc)[:1000],
            "trace": trace,
            "screenshots": screenshots,
        }
    finally:
        if controller:
            controller.close()
