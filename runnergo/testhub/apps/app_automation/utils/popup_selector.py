# -*- coding: utf-8 -*-
"""Text-driven popup/picker selection shared by live capture and replay."""
from __future__ import annotations

import re
import statistics
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .locator_helpers import parse_bounds, parse_resolution


POPUP_PATH_SEPARATOR = re.compile(r"\s*(?:/|／|>|\u203a|\u2192|,|，|\n)\s*")
ADMIN_DIVISION_SUFFIXES = (
    "特别行政区",
    "自治州",
    "自治县",
    "自治区",
    "新区",
    "地区",
    "盟",
    "省",
    "市",
    "区",
    "县",
    "旗",
)


class PopupSelectionError(AssertionError):
    """Raised when a requested popup option cannot be found."""


def split_popup_selection_path(value: Any) -> List[str]:
    """Split ``province/city/district`` style input into picker columns."""
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in POPUP_PATH_SEPARATOR.split(text) if item.strip()]


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().casefold()


def _strip_admin_division_suffix(value: str) -> str:
    for suffix in ADMIN_DIVISION_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[:-len(suffix)]
    return value


def _popup_text_exact_match(expected: str, candidate: str) -> bool:
    if expected == candidate:
        return True
    return expected == _strip_admin_division_suffix(candidate)


def _popup_region_pixels(region: Any, resolution: Any) -> Tuple[int, int, int, int]:
    width, height = parse_resolution(resolution)
    if width <= 0 or height <= 0:
        return 0, 0, 0, 0

    value = region
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        if len(parts) >= 4:
            value = parts[:4]
    if isinstance(value, dict):
        value = [value.get(key) for key in ("x1", "y1", "x2", "y2")]
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        value = [0.0, 0.35, 1.0, 0.98]

    try:
        numbers = [float(item) for item in value[:4]]
    except (TypeError, ValueError):
        numbers = [0.0, 0.35, 1.0, 0.98]
    if max(abs(item) for item in numbers) <= 1.0:
        x1, y1, x2, y2 = (
            numbers[0] * width,
            numbers[1] * height,
            numbers[2] * width,
            numbers[3] * height,
        )
    else:
        x1, y1, x2, y2 = numbers
    left = max(0, min(width - 1, int(round(min(x1, x2)))))
    top = max(0, min(height - 1, int(round(min(y1, y2)))))
    right = max(left + 1, min(width, int(round(max(x1, x2)))))
    bottom = max(top + 1, min(height, int(round(max(y1, y2)))))
    return left, top, right, bottom


def find_popup_text_node(
    nodes: Sequence[Dict[str, Any]],
    text: Any,
    resolution: Any,
    *,
    column_index: int = 0,
    column_count: int = 1,
    popup_region: Any = None,
    match_mode: str = "exact",
) -> Optional[Dict[str, Any]]:
    """Return the best visible picker node for one input path segment."""
    expected = _normalize_text(text)
    if not expected:
        return None
    left, top, right, bottom = _popup_region_pixels(popup_region, resolution)
    if right <= left or bottom <= top:
        return None
    count = max(1, int(column_count or 1))
    index = max(0, min(count - 1, int(column_index or 0)))
    expected_x = left + (right - left) * (index + 0.5) / count
    column_width = max(1.0, (right - left) / count)
    column_left = left + column_width * index
    column_right = column_left + column_width
    normalized_mode = str(match_mode or "exact").strip().lower()
    ranked = []

    for node in nodes or []:
        if node.get("visible", True) is False or node.get("enabled", True) is False:
            continue
        bounds = parse_bounds(node.get("bounds"))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        if not (left <= center_x <= right and top <= center_y <= bottom):
            continue
        if count > 1 and not (column_left <= center_x <= column_right):
            continue
        values = []
        for key in ("text", "content_desc", "value", "placeholder", "resource_id"):
            normalized = _normalize_text(node.get(key))
            if normalized and normalized not in values:
                values.append(normalized)
        exact = any(_popup_text_exact_match(expected, value) for value in values)
        contains = any(expected in value or value in expected for value in values)
        if not exact and not (normalized_mode == "contains" and contains):
            continue
        column_distance = abs(center_x - expected_x) / column_width
        area = max(1, x2 - x1) * max(1, y2 - y1)
        ranked.append((
            0 if exact else 1,
            round(column_distance, 4),
            area,
            -int(node.get("depth") or 0),
            node,
        ))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[:4])
    return ranked[0][4]


def popup_column_swipe(
    resolution: Any,
    *,
    column_index: int,
    column_count: int,
    direction: str,
    popup_region: Any = None,
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Build a vertical swipe constrained to the requested picker column."""
    _width, height = parse_resolution(resolution)
    left, top, right, bottom = _popup_region_pixels(popup_region, resolution)
    count = max(1, int(column_count or 1))
    index = max(0, min(count - 1, int(column_index or 0)))
    x = int(round(left + (right - left) * (index + 0.5) / count))
    # iOS WebView picker wheels retain noticeable momentum even after WDA's
    # swipe call returns. A short gesture is much less likely to skip the
    # requested option than the page-scroll-sized gesture used elsewhere.
    center_y = top + (bottom - top) * 0.70
    # ``resolution`` is the Airtest screenshot size. On Retina iOS devices it
    # is commonly 3x the logical UIKit/WDA coordinate space, and Airtest scales
    # the gesture back down before sending it to WDA. A fixed 48px cap therefore
    # becomes only a 16pt drag and many WebView pickers do not move at all.
    # Scale the cap with screen height so the resulting logical gesture remains
    # roughly 24-48pt on both 1x and Retina devices.
    distance = max(
        height * 0.0275,
        min(height * 0.055, (bottom - top) * 0.07),
    )
    upper = int(round(center_y - distance / 2))
    lower = int(round(center_y + distance / 2))
    if str(direction or "up").lower() == "down":
        return (x, upper), (x, lower)
    return (x, lower), (x, upper)


def popup_node_tap_point(
    node: Dict[str, Any],
    nodes: Sequence[Dict[str, Any]],
    resolution: Any,
    *,
    column_index: int = 0,
    column_count: int = 1,
    popup_region: Any = None,
) -> Optional[Tuple[int, int]]:
    """Return the visual row center instead of an overlapping semantic center.

    Safari/WKWebView exposes picker rows as tall, overlapping accessibility
    buttons. Their top edges still advance by the real row pitch, while their
    geometric centers land one or two options below the requested label.
    """
    bounds = parse_bounds(node.get("bounds"))
    if not bounds:
        return None
    x1, y1, x2, y2 = bounds
    target_x = int(round((x1 + x2) / 2))
    target_y = int(round((y1 + y2) / 2))

    left, top, right, bottom = _popup_region_pixels(popup_region, resolution)
    count = max(1, int(column_count or 1))
    index = max(0, min(count - 1, int(column_index or 0)))
    column_width = max(1.0, (right - left) / count)
    column_left = left + column_width * index
    column_right = column_left + column_width
    row_bounds = []
    target_class = str(node.get("class_name") or "")
    for candidate in nodes or []:
        if candidate.get("visible", True) is False or candidate.get("enabled", True) is False:
            continue
        candidate_bounds = parse_bounds(candidate.get("bounds"))
        if not candidate_bounds:
            continue
        cx1, cy1, cx2, cy2 = candidate_bounds
        center_x = (cx1 + cx2) / 2
        if not (column_left <= center_x <= column_right):
            continue
        candidate_width = max(1.0, float(cx2 - cx1))
        if not (column_width * 0.7 <= candidate_width <= column_width * 1.3):
            continue
        if target_class and str(candidate.get("class_name") or "") != target_class:
            continue
        if cy2 < top or cy1 > bottom:
            continue
        if not any(
            _normalize_text(candidate.get(key))
            for key in ("text", "content_desc", "value", "resource_id")
        ):
            continue
        row_bounds.append((float(cy1), float(cy2)))

    unique_tops = sorted(set(item[0] for item in row_bounds))
    pitches = [
        current - previous
        for previous, current in zip(unique_tops, unique_tops[1:])
        if 4 <= current - previous <= 160
    ]
    if pitches:
        row_pitch = float(statistics.median(pitches))
        semantic_height = max(1.0, float(y2 - y1))
        if semantic_height > row_pitch * 1.5:
            # During a CSS picker transform Safari may anchor accessibility
            # boxes by either their top or bottom edge. Choose the orientation
            # whose derived row centers best align with the fixed selection
            # band, then apply the same orientation to the requested option.
            selection_y = top + (bottom - top) * 0.62
            top_distance = min(
                abs(candidate_top + row_pitch / 2 - selection_y)
                for candidate_top, _candidate_bottom in row_bounds
            )
            bottom_distance = min(
                abs(candidate_bottom - row_pitch / 2 - selection_y)
                for _candidate_top, candidate_bottom in row_bounds
            )
            if bottom_distance < top_distance:
                target_y = int(round(y2 - row_pitch / 2))
            else:
                target_y = int(round(y1 + row_pitch / 2))

            safe_top = max(
                top + (bottom - top) * 0.35,
                selection_y - row_pitch * 2.25,
            )
            safe_bottom = min(
                bottom - (bottom - top) * 0.10,
                selection_y + row_pitch * 2.25,
            )
            if not (safe_top <= target_y <= safe_bottom):
                return None

    target_x = max(left, min(right - 1, target_x))
    target_y = max(top, min(bottom - 1, target_y))
    return target_x, target_y


def select_popup_text_path(
    value: Any,
    *,
    resolution: Any,
    get_nodes: Callable[[], Sequence[Dict[str, Any]]],
    tap: Callable[[int, int], Any],
    swipe: Callable[[int, int, int, int, float], Any],
    pause: Callable[[float], Any],
    max_swipes: int = 8,
    interval: float = 0.25,
    duration: float = 0.35,
    popup_region: Any = None,
    match_mode: str = "exact",
) -> Dict[str, Any]:
    """Select every segment in a dependent native/WebView picker path."""
    parts = split_popup_selection_path(value)
    if not parts:
        raise ValueError("弹框选择内容不能为空")
    width, height = parse_resolution(resolution)
    if width <= 0 or height <= 0:
        raise ValueError("无法读取设备分辨率，不能执行弹框选择")

    swipe_limit = max(0, min(30, int(max_swipes or 0)))
    wait_seconds = max(0.0, min(3.0, float(interval or 0)))
    swipe_duration = max(0.1, min(2.0, float(duration or 0.35)))
    selected = []

    def locate_selectable_node(
        candidate_nodes: Sequence[Dict[str, Any]],
        expected_text: str,
        index: int,
    ) -> Optional[Dict[str, Any]]:
        candidate = find_popup_text_node(
            candidate_nodes,
            expected_text,
            (width, height),
            column_index=index,
            column_count=len(parts),
            popup_region=popup_region,
            match_mode=match_mode,
        )
        if candidate and not popup_node_tap_point(
            candidate,
            candidate_nodes,
            (width, height),
            column_index=index,
            column_count=len(parts),
            popup_region=popup_region,
        ):
            return None
        return candidate

    for column_index, part in enumerate(parts):
        current_nodes = list(get_nodes() or [])
        if not current_nodes:
            pause(wait_seconds)
            current_nodes = list(get_nodes() or [])
        if not current_nodes:
            raise PopupSelectionError(
                "当前弹框未暴露可识别文本节点，请确认弹框已打开或改用坐标/图片步骤"
            )
        node = locate_selectable_node(
            current_nodes,
            part,
            column_index,
        )
        swipe_attempts = 0
        if not node and swipe_limit:
            # Sweep forward first, then back across the starting position so either list
            # direction can be found without requiring the user to know wheel ordering.
            for direction, attempts in (("up", swipe_limit), ("down", swipe_limit * 2)):
                for _ in range(attempts):
                    start, end = popup_column_swipe(
                        (width, height),
                        column_index=column_index,
                        column_count=len(parts),
                        direction=direction,
                        popup_region=popup_region,
                    )
                    swipe(start[0], start[1], end[0], end[1], swipe_duration)
                    swipe_attempts += 1
                    # Wait for wheel momentum/accessibility bounds to settle
                    # before deciding whether the target is visible.
                    pause(max(wait_seconds, min(1.0, swipe_duration + 0.2)))
                    refreshed_nodes = list(get_nodes() or [])
                    node = locate_selectable_node(
                        refreshed_nodes,
                        part,
                        column_index,
                    )
                    if node:
                        break
                if node:
                    break
        if not node:
            raise PopupSelectionError(f"弹框内未定位到文本：{part}")

        current_nodes = list(get_nodes() or [])
        stable_node = locate_selectable_node(
            current_nodes,
            part,
            column_index,
        )
        if not stable_node:
            pause(max(wait_seconds, 0.2))
            current_nodes = list(get_nodes() or [])
            stable_node = locate_selectable_node(
                current_nodes,
                part,
                column_index,
            )
        if not stable_node:
            raise PopupSelectionError(f"弹框文本尚未稳定，请重试：{part}")
        node = stable_node
        target = popup_node_tap_point(
            node,
            current_nodes,
            (width, height),
            column_index=column_index,
            column_count=len(parts),
            popup_region=popup_region,
        )
        if not target:
            raise PopupSelectionError(f"弹框文本缺少可点击区域：{part}")
        target_x, target_y = target
        tap(target_x, target_y)
        pause(max(wait_seconds, 0.4 if column_index + 1 < len(parts) else wait_seconds))

        # Dependent wheel pickers must confirm that the requested label snapped
        # into the fixed selection band. This prevents a visible-but-clipped row
        # (often hidden behind Safari's bottom toolbar) from being reported as a
        # successful selection.
        bounds = parse_bounds(node.get("bounds"))
        region_left, region_top, region_right, region_bottom = _popup_region_pixels(
            popup_region, (width, height)
        )
        semantic_height = (bounds[3] - bounds[1]) if bounds else 0
        should_confirm = (
            len(parts) > 1
            and semantic_height > (region_bottom - region_top) * 0.12
        )
        if should_confirm:
            selection_y = region_top + (region_bottom - region_top) * 0.62
            centered_threshold = max(
                18.0,
                min(40.0, (region_bottom - region_top) * 0.07),
            )
            confirmed = False
            for _ in range(4):
                confirmation_nodes = list(get_nodes() or [])
                confirmation_node = find_popup_text_node(
                    confirmation_nodes,
                    part,
                    (width, height),
                    column_index=column_index,
                    column_count=len(parts),
                    popup_region=popup_region,
                    match_mode=match_mode,
                )
                confirmation_point = (
                    popup_node_tap_point(
                        confirmation_node,
                        confirmation_nodes,
                        (width, height),
                        column_index=column_index,
                        column_count=len(parts),
                        popup_region=popup_region,
                    )
                    if confirmation_node
                    else None
                )
                if confirmation_point and abs(confirmation_point[1] - selection_y) <= centered_threshold:
                    confirmed = True
                    break
                if not confirmation_point:
                    break
                direction = "up" if confirmation_point[1] > selection_y else "down"
                start, end = popup_column_swipe(
                    (width, height),
                    column_index=column_index,
                    column_count=len(parts),
                    direction=direction,
                    popup_region=popup_region,
                )
                swipe(start[0], start[1], end[0], end[1], swipe_duration)
                swipe_attempts += 1
                pause(max(wait_seconds, min(1.0, swipe_duration + 0.2)))
            if not confirmed:
                raise PopupSelectionError(f"弹框文本未确认选中：{part}")
        selected.append({
            "text": part,
            "column": column_index,
            "target": {"x": target_x, "y": target_y},
            "swipe_attempts": swipe_attempts,
            "class_name": node.get("class_name") or "",
        })

    return {
        "matched": True,
        "value": str(value),
        "parts": parts,
        "selected": selected,
        "popup_region": list(_popup_region_pixels(popup_region, (width, height))),
    }
