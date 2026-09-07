"""元素定位器：把视觉解析结果转换为语义定位动作。

不依赖固定 XPath，统一使用 BrowserController 的
placeholder / label / role / 文本 语义定位能力。
"""
from __future__ import annotations

from typing import Any, Optional


def locate_and_act(
    controller: Any,
    element: dict,
    *,
    value: str = "",
    index: int = 0,
) -> dict:
    """根据元素描述执行填充或点击，返回执行结果。"""
    role = str(element.get("role") or "")
    name = str(element.get("name") or "")
    kind = str(element.get("kind") or "")
    if role in {"textbox", "searchbox"} or kind in {"username", "password", "text"}:
        filled = controller.fill_by(
            placeholder=name if name else "",
            label=name if name else "",
            role="textbox",
            value=value,
            index=index,
        )
        if filled:
            return {"action": "fill", "target": name or "(输入框)", "value": value, "status": "success"}
        return {"action": "fill", "target": name or "(输入框)", "value": value, "status": "failed", "error": "未定位到输入框"}
    clicked = controller.click_by(text=name, role=role if role in {"button", "link"} else "button", index=index)
    if clicked:
        return {"action": "click", "target": name or "(按钮)", "status": "success"}
    return {"action": "click", "target": name or "(按钮)", "status": "failed", "error": "未定位到可点击元素"}
