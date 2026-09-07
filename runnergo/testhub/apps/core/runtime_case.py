"""Execution-time input overrides shared by automation execution flows.

The source case is always deep-copied. Overrides and dynamic expressions are
applied only to the RuntimeCase instance created for one execution.
"""
from __future__ import annotations

import copy
import random
import re
import string
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


INPUT_ACTIONS = frozenset({
    "input",
    "fill",
    "type",
    "send_keys",
    "input_text",
    "smart_input",
    "input_web_text",
    "self_heal_input",
})

VALUE_KEYS = ("value", "text", "keys", "input")
CHILD_METADATA_KEYS = frozenset({
    "element",
    "target",
    "fingerprint",
    "locators",
    "locator_strategies",
    "fallback_position",
    "source_position",
    "target_position",
    "target_element",
    "config",
    "runtime_assertions",
    "metadata",
})

_EXPRESSION_PATTERN = re.compile(
    r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)(?:\(([^{}]*)\))?\s*\}"
)


def _path_text(path: Sequence[Any]) -> str:
    return ".".join(str(part) for part in path)


def _parse_length(args_text: Optional[str], default: int = 8) -> int:
    if not args_text:
        return default
    first = str(args_text).split(",", 1)[0].strip().strip("'\"")
    try:
        return max(1, min(256, int(first)))
    except (TypeError, ValueError):
        return default


def resolve_dynamic_value(value: Any, context: Any = None) -> Any:
    """Resolve supported runtime expressions recursively."""
    if context is not None and hasattr(context, 'resolve'):
        return context.resolve(value)
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1).lower()
            args_text = match.group(2)
            if name == "timestamp":
                return str(int(time.time() * 1000))
            if name == "uuid":
                return str(uuid.uuid4())
            if name in {"random_phone", "random_phone_cn"}:
                return "1" + random.choice("3456789") + "".join(
                    random.choice(string.digits) for _ in range(9)
                )
            if name == "random_string":
                length = _parse_length(args_text)
                alphabet = string.ascii_letters + string.digits
                return "".join(random.choice(alphabet) for _ in range(length))
            return match.group(0)

        return _EXPRESSION_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [resolve_dynamic_value(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_dynamic_value(item) for key, item in value.items()}
    return value


def _element_label(step: Dict[str, Any]) -> str:
    for candidate in (step.get("element"), step.get("target")):
        if isinstance(candidate, dict):
            for key in ("name", "label", "accessible_name", "text", "id"):
                value = candidate.get(key)
                if value not in (None, ""):
                    return str(value)
        elif candidate not in (None, ""):
            return str(candidate)

    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    for source in (step, config):
        for key in ("element_name", "element_id"):
            value = source.get(key)
            if value not in (None, ""):
                return f"元素#{value}" if key == "element_id" else str(value)

    for fingerprint in (step.get("fingerprint"), config.get("fingerprint")):
        if not isinstance(fingerprint, dict):
            continue
        for key in ("resource_id", "content_desc", "text", "placeholder", "accessible_name"):
            value = fingerprint.get(key)
            if value not in (None, ""):
                return str(value)

    for source in (step, config):
        for key in ("webview_css", "webview_xpath", "webview_text", "selector"):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return "-"


def _value_location(step: Dict[str, Any], path: Tuple[Any, ...]) -> Tuple[Tuple[Any, ...], Any]:
    for key in VALUE_KEYS:
        if key in step:
            return path + (key,), step.get(key)
    config = step.get("config")
    if isinstance(config, dict):
        for key in VALUE_KEYS:
            if key in config:
                return path + ("config", key), config.get(key)
        return path + ("config", "value"), ""
    return path + ("value",), ""


@dataclass(frozen=True)
class InputField:
    step_id: str
    step_path: str
    step_name: str
    action: str
    element: str
    default_value: Any
    value_path: Tuple[Any, ...]
    sensitive: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stepId": self.step_id,
            "stepPath": self.step_path,
            "step": self.step_name,
            "action": self.action,
            "element": self.element,
            "defaultValue": self.default_value,
            "sensitive": self.sensitive,
        }


def scan_input_fields(case_data: Any) -> List[InputField]:
    fields: List[InputField] = []

    def walk(node: Any, path: Tuple[Any, ...]) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, path + (index,))
            return
        if not isinstance(node, dict):
            return

        action = str(node.get("action") or node.get("type") or "").strip().lower()
        if action in INPUT_ACTIONS:
            step_path = _path_text(path)
            raw_id = node.get("id") or node.get("stepId") or node.get("step_id")
            step_id = str(raw_id or f"runtime:{step_path}")
            value_path, default_value = _value_location(node, path)
            config = node.get("config") if isinstance(node.get("config"), dict) else {}
            input_kind = str(node.get("input_kind") or config.get("input_kind") or "").lower()
            fields.append(InputField(
                step_id=step_id,
                step_path=step_path,
                step_name=str(node.get("name") or node.get("title") or step_id),
                action=action,
                element=_element_label(node),
                default_value=default_value,
                value_path=value_path,
                sensitive=bool(node.get("sensitive") or config.get("sensitive") or input_kind == "password"),
            ))

        for key, value in node.items():
            if key in CHILD_METADATA_KEYS:
                continue
            if isinstance(value, (dict, list)):
                walk(value, path + (key,))

    walk(case_data, ())
    return fields


def public_input_fields(case_data: Any) -> List[Dict[str, Any]]:
    return [field.as_dict() for field in scan_input_fields(case_data)]


def normalize_overrides(overrides: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if overrides in (None, ""):
        return []
    if not isinstance(overrides, (list, tuple)):
        raise ValueError("runtimeOverride 必须是数组")
    if len(overrides) > 500:
        raise ValueError("runtimeOverride 最多支持 500 个字段")
    normalized = []
    for index, item in enumerate(overrides, 1):
        if not isinstance(item, dict):
            raise ValueError(f"runtimeOverride 第 {index} 项格式错误")
        step_id = str(item.get("stepId") or item.get("step_id") or "").strip()
        step_path = str(item.get("stepPath") or item.get("step_path") or "").strip()
        if not step_id and not step_path:
            raise ValueError(f"runtimeOverride 第 {index} 项缺少 stepId/stepPath")
        value_key = next(
            (key for key in ("runtimeValue", "overrideValue", "runtime_value") if key in item),
            None,
        )
        if value_key is None:
            continue
        runtime_value = item.get(value_key)
        if isinstance(runtime_value, str) and len(runtime_value) > 10000:
            raise ValueError(f"runtimeOverride 第 {index} 项执行值过长")
        normalized.append({"stepId": step_id, "stepPath": step_path, "runtimeValue": runtime_value})
    return normalized


def _set_path(root: Any, path: Sequence[Any], value: Any) -> None:
    current = root
    for part in path[:-1]:
        if isinstance(part, int):
            current = current[part]
        else:
            if part not in current or not isinstance(current[part], (dict, list)):
                current[part] = {}
            current = current[part]
    last = path[-1]
    if isinstance(last, int):
        current[last] = value
    else:
        current[last] = value


def _runtime_assertion_rules(case_data: Any) -> List[Dict[str, Any]]:
    if not isinstance(case_data, dict):
        return []
    candidates = [
        case_data.get("runtime_assertions"),
        case_data.get("runtimeAssertions"),
    ]
    metadata = case_data.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend([metadata.get("runtime_assertions"), metadata.get("runtimeAssertions")])
    for value in candidates:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _find_step_insert_location(
    node: Any,
    rule: Dict[str, Any],
    path: Tuple[Any, ...] = (),
) -> Optional[Tuple[List[Any], int]]:
    target_id = str(rule.get("afterStepId") or rule.get("after_step_id") or "").strip()
    target_path = str(rule.get("afterStepPath") or rule.get("after_step_path") or "").strip()
    target_type = str(rule.get("afterType") or rule.get("after_type") or "").strip().lower()

    if isinstance(node, dict):
        steps = node.get("steps")
        if isinstance(steps, list):
            found = _find_step_insert_location(steps, rule, path + ("steps",))
            if found:
                return found
        return None
    if not isinstance(node, list):
        return None

    fallback: Optional[Tuple[List[Any], int]] = None
    for index, item in enumerate(node):
        item_path = path + (index,)
        if isinstance(item, dict):
            step_id = str(item.get("id") or item.get("stepId") or item.get("step_id") or "").strip()
            step_type = str(item.get("type") or item.get("action") or "").strip().lower()
            path_text = _path_text(item_path)
            if (target_id and step_id == target_id) or (target_path and path_text == target_path):
                return node, index + 1
            if target_type and step_type == target_type:
                fallback = node, index + 1
            found = _find_step_insert_location(item, rule, item_path)
            if found:
                return found
    return fallback


def _normalize_assertion_type(value: Any) -> str:
    normalized = str(value or "assert_text").strip().lower()
    aliases = {
        "text": "assert_text",
        "page_text": "assert_text",
        "exists": "assert_exists",
        "visible": "assert_exists",
        "page": "assert_page",
        "activity": "assert_activity",
    }
    return aliases.get(normalized, normalized)


def _runtime_assertion_step(rule: Dict[str, Any], index: int, context: Any = None) -> Dict[str, Any]:
    step_type = _normalize_assertion_type(rule.get("type") or rule.get("action"))
    if step_type not in {"assert_text", "assert_exists", "assert_page", "assert_activity"}:
        raise ValueError(f"runtime_assertions 第 {index} 项断言类型不支持: {step_type}")

    expected = resolve_dynamic_value(
        rule.get("expected", rule.get("value", rule.get("runtimeValue", ""))),
        context=context,
    )
    config = copy.deepcopy(rule.get("config")) if isinstance(rule.get("config"), dict) else {}
    config = resolve_dynamic_value(config, context=context)
    step: Dict[str, Any] = {
        "id": str(rule.get("id") or f"runtime-assertion-{index}"),
        "type": step_type,
        "name": str(rule.get("name") or "数据驱动断言"),
        "timeout": rule.get("timeout", 5),
        "runtime_generated_assertion": True,
        "runtime_assertion_rule": copy.deepcopy(rule),
    }
    if step_type == "assert_text":
        step["expected"] = expected
        config.setdefault("expected", expected)
        config.setdefault("selector_type", "region")
        config.setdefault("selector", [0, 0, 1, 1])
    elif step_type == "assert_exists":
        step["expected_exists"] = rule.get("expected_exists", True)
    elif step_type == "assert_page":
        for key in ("page_type", "expected_page", "expected_package", "expected_activity", "match_mode"):
            if key in rule:
                step[key] = resolve_dynamic_value(rule[key], context=context)
    elif step_type == "assert_activity":
        step["expected"] = expected
        if rule.get("expected_package"):
            step["expected_package"] = resolve_dynamic_value(rule.get("expected_package"), context=context)
    if config:
        step["config"] = config
    return step


def inject_runtime_assertions(case_data: Any, context: Any = None) -> Any:
    """Insert data-driven assertion steps into an execution-only case copy."""
    rules = _runtime_assertion_rules(case_data)
    if not rules:
        return case_data
    root = copy.deepcopy(case_data)
    steps = root.get("steps") if isinstance(root, dict) else None
    if not isinstance(steps, list):
        raise ValueError("runtime_assertions 需要用例包含 steps 数组")

    def remove_generated(items: List[Any]) -> None:
        items[:] = [
            item for item in items
            if not (isinstance(item, dict) and item.get("runtime_generated_assertion"))
        ]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("steps"), list):
                remove_generated(item["steps"])

    remove_generated(steps)
    for index, rule in enumerate(rules, 1):
        if rule.get("enabled") is False:
            continue
        location = _find_step_insert_location(root, rule)
        if not location:
            label = rule.get("afterStepId") or rule.get("afterStepPath") or rule.get("afterType") or "末尾"
            raise ValueError(f"runtime_assertions 第 {index} 项找不到插入步骤: {label}")
        parent, insert_index = location
        parent.insert(insert_index, _runtime_assertion_step(rule, index, context=context))
    return root


class RuntimeCase:
    """A non-persistent case copy with execution-time input values applied."""

    def __init__(self, source_case: Any, runtime_override: Optional[Iterable[Dict[str, Any]]] = None):
        self.source_case = source_case
        self.fields = scan_input_fields(source_case)
        self.runtime_override = normalize_overrides(runtime_override)
        self.case_data: Any = None
        self.execution_data: List[Dict[str, Any]] = []

    def build(self, context: Any = None) -> "RuntimeCase":
        by_path = {item["stepPath"]: item for item in self.runtime_override if item["stepPath"]}
        by_id = {item["stepId"]: item for item in self.runtime_override if item["stepId"]}
        valid_paths = {field.step_path for field in self.fields}
        valid_ids = {field.step_id for field in self.fields}
        unknown = [
            item for item in self.runtime_override
            if item["stepPath"] not in valid_paths and item["stepId"] not in valid_ids
        ]
        if unknown:
            raise ValueError(f"runtimeOverride 包含不存在的步骤: {unknown[0].get('stepId') or unknown[0].get('stepPath')}")

        self.case_data = copy.deepcopy(self.source_case)
        self.execution_data = []
        for field in self.fields:
            override = by_path.get(field.step_path) or by_id.get(field.step_id)
            raw_value = override["runtimeValue"] if override is not None else field.default_value
            final_value = resolve_dynamic_value(raw_value, context=context)
            _set_path(self.case_data, field.value_path, final_value)
            self.execution_data.append({
                **field.as_dict(),
                "runtimeValue": override["runtimeValue"] if override is not None else None,
                "finalValue": final_value,
                "source": "runtimeOverride" if override is not None else "defaultValue",
            })
        self.case_data = inject_runtime_assertions(self.case_data, context=context)
        return self
