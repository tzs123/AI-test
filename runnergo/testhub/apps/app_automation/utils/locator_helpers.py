# -*- coding: utf-8 -*-
"""APP 自动化稳健定位的纯函数工具。

该模块不依赖 Django、Airtest 或 OpenCV，既可以被截图/录制接口使用，也可以
在执行器中使用，并且便于针对坐标换算和 UI 树评分做单元测试。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


BOUNDS_PATTERN = re.compile(
    r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
)

DEFAULT_LOCATOR_STRATEGY_ORDER = (
    "css",
    "resource_id",
    "accessibility",
    "text",
    "xpath",
    "webview_xpath",
    "ocr",
    "image",
    "position",
)

LOCATOR_STABILITY_SCORES = {
    "resource_id": 95,
    "accessibility": 90,
    "css": 88,
    "text": 70,
    "ocr": 62,
    "image": 55,
    "webview_xpath": 45,
    "xpath": 40,
    "position": 35,
}

LOCATOR_STRATEGY_ALIASES = {
    "id": "resource_id",
    "appium_id": "resource_id",
    "resource-id": "resource_id",
    "accessibility_id": "accessibility",
    "content_desc": "accessibility",
    "content-desc": "accessibility",
    "coordinate": "position",
    "coordinates": "position",
    "pos": "position",
    "region": "position",
    "css_selector": "css",
    "webview_css": "css",
    "dom_xpath": "webview_xpath",
    "webview_xpath": "webview_xpath",
}


def _strategy_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return LOCATOR_STRATEGY_ALIASES.get(normalized, normalized)


def locator_strategy_value(config: Dict[str, Any], strategy_type: str) -> Any:
    """从统一配置和旧 fingerprint 中解析某一种策略的定位值。"""
    fingerprint = config.get("fingerprint") or {}
    strategy_type = _strategy_type(strategy_type)
    if strategy_type == "css":
        return config.get("webview_css") or config.get("css_selector") or ""
    if strategy_type == "webview_xpath":
        return config.get("webview_xpath") or ""
    if strategy_type == "resource_id":
        return config.get("resource_id") or fingerprint.get("resource_id") or ""
    if strategy_type == "accessibility":
        return (
            config.get("accessibility_id")
            or config.get("content_desc")
            or fingerprint.get("content_desc")
            or ""
        )
    if strategy_type == "text":
        return config.get("text") or fingerprint.get("text") or ""
    if strategy_type == "xpath":
        return config.get("xpath") or ""
    if strategy_type == "ocr":
        return config.get("ocr_text") or config.get("text") or fingerprint.get("text") or ""
    if strategy_type == "image":
        return config.get("image_path") or ""
    if strategy_type == "position":
        return (
            config.get("fallback_position")
            or config.get("normalized_position")
            or config.get("normalized_region")
            or config.get("bounds")
            or config.get("template_bounds")
            or ({"x": config.get("x"), "y": config.get("y")} if "x" in config and "y" in config else {})
        )
    return ""


def _with_recommended_locator(strategies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates = [
        (
            int(item.get("stability_score") or LOCATOR_STABILITY_SCORES.get(item.get("type"), 0)),
            -index,
            item.get("type"),
        )
        for index, item in enumerate(strategies)
        if item.get("enabled", True) is not False and item.get("value") not in (None, "", {})
    ]
    recommended_type = max(candidates)[2] if candidates else ""
    for item in strategies:
        item["recommended"] = bool(recommended_type and item.get("type") == recommended_type)
    return strategies


def normalize_locator_strategies(
    config: Optional[Dict[str, Any]],
    element_type: str = "",
) -> List[Dict[str, Any]]:
    """标准化定位策略，并为没有新配置的存量元素生成兼容降级链。"""
    config = dict(config or {})
    raw_strategies = config.get("locator_strategies")
    normalized: List[Dict[str, Any]] = []
    seen = set()

    if isinstance(raw_strategies, list):
        for index, raw in enumerate(raw_strategies):
            item = {"type": raw} if isinstance(raw, str) else dict(raw or {})
            strategy_type = _strategy_type(item.get("type"))
            if strategy_type not in DEFAULT_LOCATOR_STRATEGY_ORDER or strategy_type in seen:
                continue
            seen.add(strategy_type)
            item["type"] = strategy_type
            item["enabled"] = item.get("enabled", True) is not False
            item["priority"] = index + 1
            if item.get("value") in (None, "", {}):
                item["value"] = locator_strategy_value(config, strategy_type)
            item["stability_score"] = int(
                item.get("stability_score") or LOCATOR_STABILITY_SCORES.get(strategy_type, 0)
            )
            normalized.append(item)

    if normalized:
        return _with_recommended_locator(normalized)

    # 存量元素没有 locator_strategies 时，按语义 → OCR → 图片 → 坐标自动升级。
    for strategy_type in DEFAULT_LOCATOR_STRATEGY_ORDER:
        value = locator_strategy_value(config, strategy_type)
        if value in (None, "", {}):
            continue
        # XPath 是可选高级定位方式，不从旧指纹自动推导。
        if strategy_type == "xpath" and not config.get("xpath"):
            continue
        seen.add(strategy_type)
        normalized.append({
            "type": strategy_type,
            "enabled": True,
            "priority": len(normalized) + 1,
            "value": value,
            "stability_score": LOCATOR_STABILITY_SCORES.get(strategy_type, 0),
        })

    # 极老数据仍保证其原生类型有一个可执行候选。
    native_strategy = {
        "appium": "text",
        "ocr": "ocr",
        "image": "image",
        "pos": "position",
        "region": "position",
    }.get(str(element_type or "").lower())
    if native_strategy and native_strategy not in seen:
        value = locator_strategy_value(config, native_strategy)
        if value not in (None, "", {}):
            normalized.append({
                "type": native_strategy,
                "enabled": True,
                "priority": len(normalized) + 1,
                "value": value,
                "stability_score": LOCATOR_STABILITY_SCORES.get(native_strategy, 0),
            })
    return _with_recommended_locator(normalized)


def xpath_to_fingerprint(xpath: Any) -> Dict[str, Any]:
    """把常见 Appium XPath 属性表达式转换为可评分的语义指纹。"""
    xpath = str(xpath or "").strip()
    if not xpath:
        return {}
    fingerprint: Dict[str, Any] = {}
    attribute_map = {
        "resource-id": "resource_id",
        "resource_id": "resource_id",
        "content-desc": "content_desc",
        "content_desc": "content_desc",
        "label": "content_desc",
        "name": "resource_id",
        "text": "text",
        "value": "text",
        "type": "class_name",
        "class": "class_name",
    }
    for attribute, _quote, value in re.findall(r"@([\w:-]+)\s*=\s*(['\"])(.*?)\2", xpath):
        target_key = attribute_map.get(attribute)
        if target_key and value:
            fingerprint[target_key] = value
    tag_match = re.search(r"//([\w.:_-]+)", xpath)
    if tag_match and tag_match.group(1) != "*":
        fingerprint.setdefault("class_name", tag_match.group(1))
    return fingerprint


def strategy_fingerprint(
    config: Dict[str, Any],
    strategy_type: str,
    value: Any = None,
) -> Dict[str, Any]:
    """为单一 Appium 策略构造严格身份匹配、保留位置消歧信息的指纹。"""
    strategy_type = _strategy_type(strategy_type)
    if strategy_type == "xpath":
        xpath_fingerprint = xpath_to_fingerprint(value or locator_strategy_value(config, strategy_type))
        if not xpath_fingerprint:
            return {}
        base = dict(config.get("fingerprint") or {})
        for key in ("resource_id", "content_desc", "text", "class_name"):
            if key in xpath_fingerprint:
                base[key] = xpath_fingerprint[key]
        return base

    identity_key = {
        "resource_id": "resource_id",
        "accessibility": "content_desc",
        "text": "text",
    }.get(strategy_type)
    if not identity_key:
        return {}
    identity_value = value or locator_strategy_value(config, strategy_type)
    if identity_value in (None, ""):
        return {}
    fingerprint = dict(config.get("fingerprint") or {})
    for key in ("resource_id", "content_desc", "text"):
        fingerprint.pop(key, None)
    fingerprint[identity_key] = identity_value
    for key in ("class_name", "package", "clickable", "enabled", "normalized_position", "parent"):
        if key not in fingerprint and config.get(key) not in (None, "", {}):
            fingerprint[key] = config.get(key)
    fingerprint["version"] = fingerprint.get("version") or 1
    return fingerprint


def clamp_ratio(value: Any) -> Optional[float]:
    """把值转换为 0~1 的比例；非法值返回 None。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def parse_resolution(value: Any) -> Tuple[int, int]:
    """兼容 dict、list/tuple、"1080x1920" 三种分辨率格式。"""
    width = height = 0
    if isinstance(value, dict):
        width = value.get("width") or value.get("w") or 0
        height = value.get("height") or value.get("h") or 0
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        width, height = value[0], value[1]
    elif isinstance(value, str):
        match = re.search(r"(\d+)\s*[xX,*]\s*(\d+)", value)
        if match:
            width, height = match.group(1), match.group(2)
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return 0, 0
    return (width, height) if width > 0 and height > 0 else (0, 0)


def parse_bounds(value: Any) -> Optional[Tuple[int, int, int, int]]:
    """解析 Android bounds、iOS x/y/width/height 或四元组区域。"""
    if isinstance(value, str):
        match = BOUNDS_PATTERN.search(value)
        if match:
            return tuple(int(item) for item in match.groups())
        parts = [part.strip() for part in value.split(",")]
        if len(parts) >= 4:
            try:
                return tuple(int(float(item)) for item in parts[:4])
            except (TypeError, ValueError):
                return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return tuple(int(float(item)) for item in value[:4])
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        keys = ("x1", "y1", "x2", "y2")
        if all(key in value for key in keys):
            try:
                return tuple(int(float(value[key])) for key in keys)
            except (TypeError, ValueError):
                return None
        ios_keys = ("x", "y", "width", "height")
        if all(key in value for key in ios_keys):
            try:
                x = int(float(value["x"]))
                y = int(float(value["y"]))
                width = int(float(value["width"]))
                height = int(float(value["height"]))
                return x, y, x + width, y + height
            except (TypeError, ValueError):
                return None
    return None


def normalize_point(x: Any, y: Any, resolution: Any) -> Dict[str, float]:
    """将绝对坐标转换为 0~1 比例。"""
    width, height = parse_resolution(resolution)
    if width <= 0 or height <= 0:
        return {}
    try:
        return {
            "x": round(max(0.0, min(1.0, float(x) / width)), 6),
            "y": round(max(0.0, min(1.0, float(y) / height)), 6),
        }
    except (TypeError, ValueError):
        return {}


def normalize_region(bounds: Any, resolution: Any) -> Dict[str, float]:
    """将区域转换为归一化坐标。"""
    parsed = parse_bounds(bounds)
    width, height = parse_resolution(resolution)
    if not parsed or width <= 0 or height <= 0:
        return {}
    x1, y1, x2, y2 = parsed
    return {
        "x1": round(max(0.0, min(1.0, x1 / width)), 6),
        "y1": round(max(0.0, min(1.0, y1 / height)), 6),
        "x2": round(max(0.0, min(1.0, x2 / width)), 6),
        "y2": round(max(0.0, min(1.0, y2 / height)), 6),
    }


def _ratio_from_config(config: Dict[str, Any], key: str, legacy_key: str) -> Optional[float]:
    normalized = config.get(key)
    if isinstance(normalized, dict):
        ratio = clamp_ratio(normalized.get(legacy_key))
        if ratio is not None:
            return ratio
    return clamp_ratio(config.get(f"{legacy_key}_ratio"))


def scale_point(
    config: Dict[str, Any],
    current_resolution: Any,
    x_key: str = "x",
    y_key: str = "y",
    normalized_key: str = "normalized_position",
) -> Optional[Tuple[int, int]]:
    """优先按比例、其次按源分辨率、最后按旧绝对坐标换算点击点。"""
    current_width, current_height = parse_resolution(current_resolution)
    if current_width <= 0 or current_height <= 0:
        current_width, current_height = parse_resolution(config.get("source_resolution"))

    normalized = config.get(normalized_key)
    ratio_x = ratio_y = None
    if isinstance(normalized, dict):
        ratio_x = clamp_ratio(normalized.get("x"))
        ratio_y = clamp_ratio(normalized.get("y"))
    if ratio_x is None:
        ratio_x = clamp_ratio(config.get(f"{x_key}_ratio"))
    if ratio_y is None:
        ratio_y = clamp_ratio(config.get(f"{y_key}_ratio"))
    if ratio_x is not None and ratio_y is not None and current_width > 0 and current_height > 0:
        return (
            max(0, min(current_width - 1, int(round(ratio_x * current_width)))),
            max(0, min(current_height - 1, int(round(ratio_y * current_height)))),
        )

    try:
        x, y = float(config.get(x_key)), float(config.get(y_key))
    except (TypeError, ValueError):
        return None

    source_width, source_height = parse_resolution(config.get("source_resolution"))
    if source_width > 0 and source_height > 0 and current_width > 0 and current_height > 0:
        x = x * current_width / source_width
        y = y * current_height / source_height
    return int(round(x)), int(round(y))


def scale_region(
    config: Dict[str, Any],
    current_resolution: Any,
    normalized_key: str = "normalized_region",
) -> Optional[Tuple[int, int, int, int]]:
    """按当前屏幕分辨率换算区域。"""
    current_width, current_height = parse_resolution(current_resolution)
    normalized = config.get(normalized_key)
    if isinstance(normalized, dict) and current_width > 0 and current_height > 0:
        ratios = [clamp_ratio(normalized.get(key)) for key in ("x1", "y1", "x2", "y2")]
        if all(item is not None for item in ratios):
            return (
                int(round(ratios[0] * current_width)),
                int(round(ratios[1] * current_height)),
                int(round(ratios[2] * current_width)),
                int(round(ratios[3] * current_height)),
            )

    parsed = parse_bounds(config)
    if not parsed:
        return None
    source_width, source_height = parse_resolution(config.get("source_resolution"))
    if source_width > 0 and source_height > 0 and current_width > 0 and current_height > 0:
        x1, y1, x2, y2 = parsed
        return (
            int(round(x1 * current_width / source_width)),
            int(round(y1 * current_height / source_height)),
            int(round(x2 * current_width / source_width)),
            int(round(y2 * current_height / source_height)),
        )
    return parsed


def region_center(bounds: Any) -> Optional[Tuple[int, int]]:
    parsed = parse_bounds(bounds)
    if not parsed:
        return None
    x1, y1, x2, y2 = parsed
    return int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))


def airtest_record_position(bounds: Any, resolution: Any) -> Optional[Tuple[float, float]]:
    """生成 Airtest IDE 使用的 record_pos（横纵方向都以屏幕宽度归一化）。"""
    center = region_center(bounds)
    width, height = parse_resolution(resolution)
    if not center or width <= 0 or height <= 0:
        return None
    return (
        round((center[0] - width / 2) / width, 4),
        round((center[1] - height / 2) / width, 4),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def parse_ui_hierarchy(xml_text: str, resolution: Any = None) -> List[Dict[str, Any]]:
    """将 Android uiautomator 或 iOS WDA XML 转成扁平节点列表。"""
    if not xml_text:
        return []
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    if start > 0:
        xml_text = xml_text[start:]
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    width, height = parse_resolution(resolution)
    ios_source_width = ios_source_height = 0
    for candidate in root.iter():
        attrs = candidate.attrib
        if attrs.get("type") != "XCUIElementTypeApplication":
            continue
        app_bounds = parse_bounds(attrs)
        if app_bounds:
            x1, y1, x2, y2 = app_bounds
            ios_source_width = max(0, x2 - x1)
            ios_source_height = max(0, y2 - y1)
        break
    ios_scale_x = (
        float(width) / ios_source_width
        if width > 0 and ios_source_width > 0
        else 1.0
    )
    ios_scale_y = (
        float(height) / ios_source_height
        if height > 0 and ios_source_height > 0
        else 1.0
    )
    nodes: List[Dict[str, Any]] = []

    def walk(element: ET.Element, depth: int, parent: Optional[Dict[str, Any]]) -> None:
        attrs = element.attrib
        bounds = parse_bounds(attrs.get("bounds")) or parse_bounds(attrs)
        current: Optional[Dict[str, Any]] = None
        if bounds:
            x1, y1, x2, y2 = bounds
            if ios_source_width and ios_source_height:
                # WDA XML 使用 UIKit 逻辑点，Airtest touch() 和截图使用原生像素。
                # 将语义节点统一到截图坐标系，避免 Retina 设备上按缩放倍数点偏。
                x1 = int(round(x1 * ios_scale_x))
                y1 = int(round(y1 * ios_scale_y))
                x2 = int(round(x2 * ios_scale_x))
                y2 = int(round(y2 * ios_scale_y))
                bounds = (x1, y1, x2, y2)
            class_name = attrs.get("class") or attrs.get("type") or element.tag or ""
            resource_id = attrs.get("resource-id") or attrs.get("name") or ""
            label = attrs.get("label") or attrs.get("content-desc") or ""
            value = attrs.get("text") or attrs.get("value") or ""
            placeholder = attrs.get("hint") or attrs.get("placeholderValue") or ""
            text = value or label or resource_id
            ios_interactive = any(
                token in class_name
                for token in (
                    "Button", "Cell", "Link", "TextField", "SecureTextField",
                    "Switch", "Slider", "Picker", "Icon", "TabBar",
                )
            )
            current = {
                "resource_id": resource_id,
                "text": text,
                "value": value,
                "placeholder": placeholder,
                "content_desc": label,
                "class_name": class_name,
                "traits": attrs.get("traits") or "",
                "package": attrs.get("package") or attrs.get("bundleId") or "",
                "clickable": _as_bool(attrs.get("clickable")) or ios_interactive,
                "enabled": _as_bool(attrs.get("enabled", True)),
                "checked": (
                    _as_bool(attrs.get("checked"))
                    if "checked" in attrs
                    else None
                ),
                "selected": (
                    _as_bool(attrs.get("selected"))
                    if "selected" in attrs
                    else None
                ),
                "visible": (
                    _as_bool(attrs.get("visible"))
                    if "visible" in attrs
                    else True
                ),
                "focusable": _as_bool(attrs.get("focusable")),
                "focused": _as_bool(attrs.get("focused")) or _as_bool(attrs.get("hasKeyboardFocus")),
                "scrollable": _as_bool(attrs.get("scrollable")),
                "depth": depth,
                "bounds": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "center": {"x": int(round((x1 + x2) / 2)), "y": int(round((y1 + y2) / 2))},
            }
            if width > 0 and height > 0:
                current["normalized_bounds"] = normalize_region(bounds, (width, height))
                current["normalized_position"] = normalize_point(
                    current["center"]["x"], current["center"]["y"], (width, height)
                )
            if parent:
                current["parent"] = {
                    key: parent.get(key, "")
                    for key in ("resource_id", "text", "content_desc", "class_name")
                }
            nodes.append(current)
        next_parent = current or parent
        for child in element:
            walk(child, depth + 1, next_parent)

    walk(root, 0, None)
    return nodes


def fingerprint_at_point(nodes: Sequence[Dict[str, Any]], x: Any, y: Any) -> Dict[str, Any]:
    """选择覆盖点击点的最合适节点，并生成精简元素指纹。"""
    try:
        px, py = float(x), float(y)
    except (TypeError, ValueError):
        return {}
    matches = []
    for node in nodes:
        bounds = parse_bounds(node.get("bounds"))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if x1 <= px <= x2 and y1 <= py <= y2:
            area = max(1, x2 - x1) * max(1, y2 - y1)
            semantic = bool(node.get("resource_id") or node.get("content_desc") or node.get("text"))
            matches.append((node, area, semantic))
    if not matches:
        return {}

    # 可点击的语义节点优先；否则选择包含点击点的最小语义节点/最深节点。
    matches.sort(
        key=lambda item: (
            0 if item[0].get("clickable") and item[2] else 1,
            0 if item[2] else 1,
            item[1],
            -int(item[0].get("depth") or 0),
        )
    )
    node = matches[0][0]
    fingerprint = {
        key: node.get(key)
        for key in (
            "resource_id", "text", "content_desc", "class_name", "package",
            "clickable", "enabled", "bounds", "normalized_bounds", "normalized_position", "parent",
        )
        if node.get(key) not in (None, "", {})
    }
    fingerprint["version"] = 1
    return fingerprint


def fingerprint_is_replay_safe(fingerprint: Dict[str, Any]) -> bool:
    """判断录制指纹是否足够精确，避免把整页容器中心当成点击目标。"""
    if not isinstance(fingerprint, dict):
        return False
    if not any(fingerprint.get(key) for key in ("resource_id", "content_desc", "text")):
        return False
    class_name = str(fingerprint.get("class_name") or "")
    normalized_bounds = fingerprint.get("normalized_bounds") or {}
    try:
        area_ratio = max(
            0.0,
            float(normalized_bounds["x2"]) - float(normalized_bounds["x1"]),
        ) * max(
            0.0,
            float(normalized_bounds["y2"]) - float(normalized_bounds["y1"]),
        )
    except (KeyError, TypeError, ValueError):
        area_ratio = 0.0

    if class_name.endswith("WebView"):
        return False
    if area_ratio >= 0.45:
        return False
    if fingerprint.get("resource_id") or fingerprint.get("clickable") is True:
        return True
    return True


def _string_equal(left: Any, right: Any) -> bool:
    return bool(left not in (None, "") and right not in (None, "") and str(left) == str(right))


def score_ui_node(fingerprint: Dict[str, Any], node: Dict[str, Any]) -> Tuple[float, List[str]]:
    """根据语义、层级和相对位置给 UI 节点评分。"""
    score = 0.0
    reasons: List[str] = []
    weights = {
        "resource_id": 55,
        "content_desc": 42,
        "text": 32,
        "class_name": 10,
        "package": 5,
    }
    for key, weight in weights.items():
        expected = fingerprint.get(key)
        actual = node.get(key)
        if _string_equal(expected, actual):
            score += weight
            reasons.append(f"{key}+{weight}")
        elif expected not in (None, "") and key in ("resource_id", "content_desc"):
            score -= weight * 0.35

    for bool_key, weight in (("clickable", 4), ("enabled", 2)):
        if bool_key in fingerprint and fingerprint.get(bool_key) == node.get(bool_key):
            score += weight
            reasons.append(f"{bool_key}+{weight}")

    expected_parent = fingerprint.get("parent") or {}
    actual_parent = node.get("parent") or {}
    for key, weight in (("resource_id", 14), ("content_desc", 10), ("text", 8), ("class_name", 4)):
        if _string_equal(expected_parent.get(key), actual_parent.get(key)):
            score += weight
            reasons.append(f"parent.{key}+{weight}")

    expected_pos = fingerprint.get("normalized_position") or {}
    actual_pos = node.get("normalized_position") or {}
    try:
        distance = math.hypot(
            float(expected_pos["x"]) - float(actual_pos["x"]),
            float(expected_pos["y"]) - float(actual_pos["y"]),
        )
        position_score = max(0.0, 18.0 * (1.0 - distance / 0.35))
        score += position_score
        if position_score:
            reasons.append(f"position+{position_score:.1f}")
    except (KeyError, TypeError, ValueError):
        pass
    return round(score, 2), reasons


def select_best_ui_node(
    fingerprint: Dict[str, Any],
    nodes: Iterable[Dict[str, Any]],
    minimum_score: float = 36.0,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """选择最佳 UI 节点，同时返回可落盘的评分诊断。"""
    ranked = []
    identity_keys = [
        key for key in ("resource_id", "content_desc", "text")
        if fingerprint.get(key) not in (None, "")
    ]
    for node in nodes:
        if node.get("enabled") is False:
            continue
        # WDA 会把已经滚出视口的 iOS 子节点继续保留在页面树里，并标记为
        # visible=false。此类节点的语义信息仍可能完全匹配，但坐标可能为负数；
        # 把它当作可操作目标会导致点击落在屏幕外。
        if node.get("visible") is False:
            continue
        score, reasons = score_ui_node(fingerprint, node)
        identity_matched = any(_string_equal(fingerprint.get(key), node.get(key)) for key in identity_keys)
        if identity_keys and not identity_matched:
            score -= 60
            reasons.append("identity-mismatch-60")
        ranked.append({
            "node": node,
            "score": round(score, 2),
            "reasons": reasons,
            "identity_matched": identity_matched,
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)
    preview = [
        {
            "score": item["score"],
            "reasons": item["reasons"],
            "identity_matched": item["identity_matched"],
            "resource_id": item["node"].get("resource_id", ""),
            "text": item["node"].get("text", ""),
            "content_desc": item["node"].get("content_desc", ""),
            "class_name": item["node"].get("class_name", ""),
            "bounds": item["node"].get("bounds"),
        }
        for item in ranked[:5]
    ]
    diagnostics = {"minimum_score": minimum_score, "candidates": preview}
    if not ranked or ranked[0]["score"] < minimum_score:
        diagnostics["matched"] = False
        return None, diagnostics
    diagnostics["matched"] = True
    diagnostics["score"] = ranked[0]["score"]
    diagnostics["ambiguous"] = len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < 6
    return ranked[0]["node"], diagnostics
