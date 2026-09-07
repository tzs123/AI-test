"""AI Test Agent orchestration for the Web UI automation platform.

The agent deliberately reuses the platform's existing, deterministic assets:
recorded Web flows, semantic locator healing, distributed execution, Allure
evidence, failure attribution and the defect library.  The language model is a
planner/analyser, not an unbounded browser operator.  When the model is not
configured or returns invalid output, a deterministic planner keeps the
workflow usable.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import requests
import yaml

from backend import analysis, db, executor
from backend.cases import service as case_service
from backend.projects import service as project_service


TERMINAL_STATUSES = {"completed", "failed", "stopped"}
EXECUTION_TERMINAL_STATUSES = {"success", "failed", "stopped"}
JSON_FIELDS = {"plan", "context", "result_summary", "failure_analysis", "defect_draft"}
FLOW_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
RUNTIME_GENERATED_VARIABLES = {"random_phone_cn", "random_apply_id"}
HEURISTIC_CASE_MIN_SCORE = 10
HEURISTIC_CASE_TOP_RATIO = 0.55
MODEL_CASE_MIN_SCORE = 8
MODEL_CASE_TOP_RATIO = 0.55
SENSITIVE_ACTION_PATTERN = re.compile(
    r"支付|购买|下单|提交订单|转账|退款|删除|注销|发布|发送|确认提交|pay|purchase|checkout|transfer|refund|delete|publish",
    re.IGNORECASE,
)
GLOBAL_GENERATED_FIELD_KINDS = {
    "phone",
    "verification_code",
    "password",
    "username",
    "email",
    "search",
}

_active_tasks: set[str] = set()
_active_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _serialize_task(row: Any) -> dict:
    item = db.to_dict(row) if row else {}
    for field in JSON_FIELDS:
        item[field] = _json_load(item.get(field), {})
    item["plan"] = _clean_generated_flow_plan(item.get("plan") or {})
    item["auto_execute"] = bool(item.get("auto_execute"))
    return item


def get_agent_task(task_id: str) -> Optional[dict]:
    rows = db.execute("SELECT * FROM ai_agent_tasks WHERE id=?", (task_id,), fetch=True)
    return _serialize_task(rows[0]) if rows else None


def list_agent_tasks(project_id: str = "", limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    if project_id:
        rows = db.execute(
            "SELECT * FROM ai_agent_tasks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
            fetch=True,
        )
    else:
        rows = db.execute(
            "SELECT * FROM ai_agent_tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
    return [_serialize_task(row) for row in rows]


def list_agent_events(task_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM ai_agent_events WHERE agent_task_id=? ORDER BY id",
        (task_id,),
        fetch=True,
    )
    events = []
    for row in rows:
        item = db.to_dict(row)
        item["payload"] = _json_load(item.get("payload"), {})
        events.append(item)
    return events


def _add_event(
    task_id: str,
    phase: str,
    message: str,
    *,
    level: str = "info",
    payload: Optional[dict] = None,
) -> None:
    db.execute(
        "INSERT INTO ai_agent_events(agent_task_id,phase,level,message,payload,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, phase, level, message, _json_dump(payload or {}), _now()),
    )


def _update_task(task_id: str, **changes: Any) -> None:
    if not changes:
        return
    allowed = {
        "status", "progress", "plan", "context", "execution_task_id",
        "result_summary", "failure_analysis", "defect_draft", "error_message",
        "finished_at", "base_url", "env", "auto_execute",
    }
    updates = {key: value for key, value in changes.items() if key in allowed}
    if not updates:
        return
    for field in JSON_FIELDS:
        if field in updates:
            updates[field] = _json_dump(updates[field])
    if "auto_execute" in updates:
        updates["auto_execute"] = int(bool(updates["auto_execute"]))
    updates["updated_at"] = _now()
    columns = ", ".join(f"{key}=?" for key in updates)
    db.execute(
        f"UPDATE ai_agent_tasks SET {columns} WHERE id=?",
        tuple(updates.values()) + (task_id,),
    )


def create_agent_task(
    *,
    project_id: str,
    goal: str,
    env: str = "test",
    base_url: str = "",
    auto_execute: bool = True,
    case_files: Optional[list[str]] = None,
    created_by: str = "",
    start: bool = True,
) -> dict:
    project = project_service.get_project(project_id)
    if not project:
        raise ValueError("项目不存在")
    goal = str(goal or "").strip()
    if len(goal) < 4:
        raise ValueError("测试目标至少需要 4 个字符")
    task_id = uuid.uuid4().hex[:12]
    now = _now()
    context = {"requested_case_files": case_files or []}
    db.execute(
        "INSERT INTO ai_agent_tasks("
        "id,project_id,goal,target_type,env,base_url,status,progress,auto_execute,"
        "plan,context,result_summary,failure_analysis,defect_draft,error_message,"
        "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, project_id, goal, "web", env or "test", base_url or "",
            "pending", 0, int(bool(auto_execute)), "{}", _json_dump(context),
            "{}", "{}", "{}", "", created_by or "", now, now,
        ),
    )
    _add_event(task_id, "created", "已接收测试目标，等待 Agent 规划")
    if start:
        start_agent_task(task_id)
    return get_agent_task(task_id)


def _model_config() -> Optional[dict]:
    model = os.environ.get("AI_AGENT_MODEL", "").strip()
    if not model:
        return None
    return {
        "model": model,
        "base_url": os.environ.get("AI_AGENT_BASE_URL", "http://ollama:11434/v1").rstrip("/"),
        "api_key": os.environ.get("AI_AGENT_API_KEY", "ollama"),
        "timeout": float(os.environ.get("AI_AGENT_TIMEOUT", "45")),
        "max_tokens": int(os.environ.get("AI_AGENT_MAX_TOKENS", "256")),
    }


def _call_model(system_prompt: str, user_prompt: str, max_tokens: Optional[int] = None) -> str:
    config = _model_config()
    if not config:
        raise RuntimeError("未配置 AI_AGENT_MODEL")
    base_url = config["base_url"]
    if base_url.endswith("/chat/completions"):
        url = base_url
    elif base_url.endswith("/v1"):
        url = f"{base_url}/chat/completions"
    else:
        url = f"{base_url}/v1/chat/completions"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": config["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": min(config["max_tokens"], int(max_tokens or config["max_tokens"])),
            "response_format": {"type": "json_object"},
            "stream": False,
        },
        timeout=config["timeout"],
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"])


def _extract_json_object(content: str) -> dict:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _auto_excluded_case_files(project_id: str) -> set[str]:
    if project_id != "default":
        return set()
    excluded = set()
    for target in getattr(executor, "EXCLUDED_DEFAULT_TARGETS", set()):
        filename = os.path.basename(str(target).replace("\\", "/"))
        stem, _ext = os.path.splitext(filename)
        if stem:
            excluded.add(f"{stem}.yaml")
            excluded.add(f"{stem}.yml")
    return excluded


def _case_asset_text(case: dict) -> str:
    tags = case.get("tags") or []
    if isinstance(tags, (list, tuple, set)):
        tag_text = " ".join(str(tag or "") for tag in tags)
    else:
        tag_text = str(tags or "")
    fields = [
        case.get("tc_id"),
        case.get("scenario"),
        case.get("name"),
        case.get("module"),
        tag_text,
        case.get("kb_summary"),
        case.get("kb_expected"),
        case.get("known_bug"),
    ]
    return " ".join(str(value or "") for value in fields if value)


def _collect_case_assets(project_id: str) -> list[dict]:
    assets = []
    excluded = _auto_excluded_case_files(project_id)
    for item in case_service.list_cases(project_id, "ui")[:100]:
        filename = item["name"]
        try:
            raw = case_service.get_case(project_id, "ui", filename)
            payload = yaml.safe_load(raw) or {}
        except Exception:
            raw = ""
            payload = {}
        content_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""
        if isinstance(payload, dict):
            steps = payload.get("steps") or []
            step_names = [
                str(step.get("name") or step.get("action") or "")
                for step in steps[:30]
                if isinstance(step, dict)
            ]
            assets.append({
                "filename": filename,
                "name": str(payload.get("name") or filename),
                "description": str(payload.get("description") or ""),
                "base_url": str(payload.get("base_url") or ""),
                "flow_version": payload.get("flow_version"),
                "step_count": len(steps),
                "step_names": step_names,
                "mtime": item.get("mtime"),
                "content_sha256": content_sha256,
                "auto_excluded": filename in excluded,
            })
        elif isinstance(payload, list):
            snippets = [_case_asset_text(case) for case in payload[:20] if isinstance(case, dict)]
            snippets = [snippet for snippet in snippets if snippet]
            scenarios = [
                str(case.get("scenario") or case.get("name") or "")
                for case in payload[:20]
                if isinstance(case, dict)
            ]
            assets.append({
                "filename": filename,
                "name": filename,
                "description": "；".join(snippets[:5] or filter(None, scenarios[:5])),
                "base_url": "",
                "flow_version": 1,
                "step_count": len(payload),
                "step_names": snippets or scenarios,
                "mtime": item.get("mtime"),
                "content_sha256": content_sha256,
                "auto_excluded": filename in excluded,
            })
    return assets


def _attach_asset_snapshot(plan: dict, assets: list[dict]) -> dict:
    """把执行所依据的最新文件版本写入计划，便于审计和防止旧快照混淆。"""
    result = dict(plan)
    selected = set(result.get("selected_case_files") or [])
    result["assets_refreshed_at"] = _now()
    result["asset_snapshot"] = [
        {
            "filename": asset.get("filename"),
            "mtime": asset.get("mtime"),
            "content_sha256": asset.get("content_sha256") or "",
            "step_count": asset.get("step_count", 0),
        }
        for asset in assets
        if asset.get("filename") in selected
    ]
    return result


def _collect_web_elements(project_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM web_elements WHERE project_id=? ORDER BY usage_count DESC, updated_at DESC",
        (project_id,),
        fetch=True,
    ) or []
    result = []
    for row in db.to_dicts(rows):
        row["locators"] = _json_load(row.get("locators"), [])
        row["fingerprint"] = _json_load(row.get("fingerprint"), {})
        row["element_context"] = _json_load(row.get("element_context"), {})
        result.append(row)
    return result


def _element_search_text(element: dict) -> str:
    fingerprint = element.get("fingerprint") or {}
    context = element.get("element_context") or {}
    attrs = fingerprint.get("attrs") or {}
    return " ".join(str(value or "") for value in (
        element.get("name"), fingerprint.get("accessible_name"), fingerprint.get("text"),
        context.get("label"), context.get("text"), context.get("accessible_name"),
        attrs.get("placeholder"), attrs.get("name"), attrs.get("id"),
        fingerprint.get("control_type"),
    )).lower()


def _page_key_for_element(element: dict) -> str:
    page_url = str(element.get("page_url") or "")
    parsed = urlsplit(page_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return page_url


def _normalize_generated_element_label(element: dict) -> str:
    fingerprint = element.get("fingerprint") or {}
    context = element.get("element_context") or {}
    attrs = fingerprint.get("attrs") or {}
    values = (
        context.get("accessible_name"),
        context.get("label"),
        context.get("text"),
        fingerprint.get("accessible_name"),
        fingerprint.get("text"),
        attrs.get("placeholder"),
        element.get("name"),
        attrs.get("name"),
        attrs.get("testid"),
        attrs.get("id"),
    )
    for value in values:
        normalized = re.sub(r"\s+", "", str(value or "").strip()).lower()
        normalized = re.sub(r"_[0-9a-f]{4,12}$", "", normalized, flags=re.IGNORECASE)
        if normalized:
            return normalized
    return str(element.get("id") or "")


def _generated_element_kind(element: dict) -> str:
    text = _element_search_text(element)
    if re.search(r"验证码|captcha|verification.?code|sms.?code", text, re.IGNORECASE):
        return "verification_code"
    if re.search(r"手机号|手机|mobile|phone|tel", text, re.IGNORECASE):
        return "phone"
    if re.search(r"密码|password|passwd", text, re.IGNORECASE):
        return "password"
    if re.search(r"用户名|账号|账户|username|account|login", text, re.IGNORECASE):
        return "username"
    if re.search(r"身份证|id.?card|identity", text, re.IGNORECASE):
        return "id_card"
    if re.search(r"邮箱|email", text, re.IGNORECASE):
        return "email"
    if re.search(r"搜索|关键词|search|keyword", text, re.IGNORECASE):
        return "search"
    return ""


def _compact_generated_label(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip()).lower()
    text = re.sub(r"_[0-9a-f]{4,12}$", "", text, flags=re.IGNORECASE)
    for token in (
        "输入", "填写", "录入", "点击", "勾选", "选择", "校验", "验证",
        "按钮", "输入框", "文本框", "字段", "控件", "选项", "复选框",
        "input", "button", "field", "checkbox", "select",
    ):
        text = text.replace(token, "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _generated_semantic_label(element: dict) -> str:
    kind = _generated_element_kind(element)
    action = _generated_action_for_element(element)
    label = _normalize_generated_element_label(element)
    search_text = _element_search_text(element)
    if (
        action == "check"
        and re.search(r"协议|隐私政策|授权书|服务许可", f"{label} {search_text}", re.IGNORECASE)
        and re.search(r"同意|勾选|已阅读|签署|agree|agreement|privacy", f"{label} {search_text}", re.IGNORECASE)
    ):
        return "agreement_acceptance"
    if not kind:
        return _compact_generated_label(label)

    compact = _compact_generated_label(label)
    cleanup_tokens = {
        "phone": ("手机号", "手机号码", "手机", "mobile", "phone", "tel"),
        "verification_code": ("验证码", "校验码", "短信码", "动态码", "码", "captcha", "verificationcode", "smscode", "code"),
        "password": ("密码", "password", "passwd", "pwd"),
        "username": ("用户名", "账号", "账户", "username", "account", "login"),
        "id_card": ("身份证", "身份证号", "idcard", "identity"),
        "email": ("邮箱", "email", "mail"),
        "search": ("搜索", "关键词", "search", "keyword"),
    }.get(kind, ())
    qualifier = compact
    for token in cleanup_tokens:
        qualifier = qualifier.replace(token, "")
    qualifier = re.sub(r"(van|field|input|ipt|txt|form|cell|item|value|name|no|num|number|id)+", "", qualifier)
    if re.fullmatch(r"\d*", qualifier or ""):
        qualifier = ""
    return f"{kind}:{qualifier or '_'}"


def _generated_element_dedupe_key(element: dict) -> tuple[str, ...]:
    fingerprint = element.get("fingerprint") or {}
    context = element.get("element_context") or {}
    action = _generated_action_for_element(element)
    kind = _generated_element_kind(element)
    semantic_label = _generated_semantic_label(element)
    if semantic_label == "agreement_acceptance":
        return ("global", action, semantic_label)
    if kind in GLOBAL_GENERATED_FIELD_KINDS:
        qualifier = semantic_label.split(":", 1)[1] if ":" in semantic_label else ""
        if qualifier in {"", "_"}:
            return ("global", action, f"{kind}:_")
    return (
        _page_key_for_element(element),
        "|".join(str(item or "") for item in context.get("frame_path") or []),
        action,
        kind,
        semantic_label,
    )


def _dedupe_generated_candidates(scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    result = []
    seen = set()
    for score, element in scored:
        key = _generated_element_dedupe_key(element)
        if key in seen:
            continue
        seen.add(key)
        result.append((score, element))
    return result


def _dedupe_generated_elements(elements: list[dict]) -> tuple[list[dict], int]:
    result = []
    seen = set()
    removed = 0
    for element in elements:
        key = _generated_element_dedupe_key(element)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        result.append(element)
    return result, removed


def _generated_flow_step_dedupe_key(step: dict) -> Optional[tuple[str, ...]]:
    action = str(step.get("action") or "")
    if action not in {"fill", "select", "check", "click"}:
        return None
    element = step.get("element") if isinstance(step.get("element"), dict) else {}
    if element:
        return _generated_element_dedupe_key(element)
    label = _compact_generated_label(step.get("name") or "")
    if (
        action == "check"
        and re.search(r"协议|隐私政策|授权书|服务许可", label, re.IGNORECASE)
        and re.search(r"同意|勾选|已阅读|签署|agree|agreement|privacy", label, re.IGNORECASE)
    ):
        label = "agreement_acceptance"
    if label in {"agreement_acceptance"}:
        return ("global", action, label)
    return ("legacy", action, label)


def _dedupe_generated_flow_steps(steps: list[dict]) -> tuple[list[dict], int]:
    result = []
    seen = set()
    removed = 0
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        key = _generated_flow_step_dedupe_key(step)
        if key and key in seen:
            removed += 1
            continue
        if key:
            seen.add(key)
        result.append(step)
    return result, removed


def _normalize_generated_step_value(step: dict, index: int) -> None:
    if step.get("action") != "fill" or not isinstance(step.get("element"), dict):
        return
    expected = _generated_value_for_element(step["element"], index)
    if expected.startswith("${") and step.get("value") != expected:
        step["value"] = expected


def _clean_generated_flow_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        return plan
    generated = plan.get("generated_flow")
    if not isinstance(generated, dict) or int(generated.get("flow_version") or 0) != 2:
        return plan
    cleaned_plan = dict(plan)
    cleaned_flow = dict(generated)
    steps, duplicate_count = _dedupe_generated_flow_steps(cleaned_flow.get("steps") or [])
    for index, step in enumerate(steps, 1):
        _normalize_generated_step_value(step, index)
    required_variables = list(dict.fromkeys(FLOW_VARIABLE_PATTERN.findall(_json_dump(steps))))
    metadata = dict(cleaned_flow.get("metadata") or {})
    metadata["required_variables"] = required_variables
    if duplicate_count:
        metadata["deduplicated_steps"] = duplicate_count
    cleaned_flow["steps"] = steps
    cleaned_flow["metadata"] = metadata
    cleaned_plan["generated_flow"] = cleaned_flow
    cleaned_plan["required_variables"] = required_variables
    cleaned_plan["missing_variables"] = [
        variable for variable in cleaned_plan.get("missing_variables", [])
        if variable in required_variables
    ]
    return cleaned_plan


def _generated_action_for_element(element: dict) -> str:
    fingerprint = element.get("fingerprint") or {}
    attrs = fingerprint.get("attrs") or {}
    control_type = str(fingerprint.get("control_type") or "").lower()
    tag = str(element.get("tag_name") or fingerprint.get("tag") or "").lower()
    role = str(element.get("element_role") or fingerprint.get("role") or "").lower()
    input_type = str(attrs.get("type") or (element.get("element_context") or {}).get("input_type") or "").lower()
    if control_type == "checkbox" or role == "checkbox" or input_type == "checkbox":
        return "check"
    if tag == "select" or role == "combobox":
        return "select"
    if tag in {"input", "textarea"} or role == "textbox":
        return "fill"
    return "click"


def _is_generated_click_candidate(element: dict) -> bool:
    fingerprint = element.get("fingerprint") or {}
    attrs = fingerprint.get("attrs") or {}
    tag = str(element.get("tag_name") or fingerprint.get("tag") or "").lower()
    role = str(element.get("element_role") or fingerprint.get("role") or "").lower()
    control_type = str(fingerprint.get("control_type") or "").lower()
    input_type = str(attrs.get("type") or "").lower()
    if role in {"button", "link", "menuitem", "tab", "option", "radio", "switch"}:
        return True
    if tag in {"button", "a", "summary"}:
        return True
    if input_type in {"button", "submit", "reset", "image"}:
        return True
    if control_type in {"plate_input", "virtual_keyboard_key"}:
        return True
    return False


def _generated_value_for_element(element: dict, index: int) -> str:
    text = _element_search_text(element)
    attrs = (element.get("fingerprint") or {}).get("attrs") or {}
    input_type = str(attrs.get("type") or "").lower()
    if input_type == "password":
        return "${PASSWORD}"
    if re.search(r"验证码|captcha|verification.?code|sms.?code", text, re.IGNORECASE):
        return "${VERIFICATION_CODE}"
    if re.search(r"密码|password|passwd", text, re.IGNORECASE):
        return "${PASSWORD}"
    if re.search(r"手机号|手机|mobile|phone|tel", text, re.IGNORECASE):
        return "13800138000"
    if re.search(r"用户名|账号|账户|username|account|login", text, re.IGNORECASE):
        return "${USERNAME}"
    if re.search(r"身份证|id.?card|identity", text, re.IGNORECASE):
        return "${ID_CARD}"
    if input_type == "email" or re.search(r"邮箱|email", text, re.IGNORECASE):
        return "test@example.com"
    if re.search(r"搜索|关键词|search|keyword", text, re.IGNORECASE):
        return "测试商品"
    return f"${{TEST_VALUE_{index}}}"


def _element_snapshot(element: dict) -> dict:
    fingerprint = element.get("fingerprint") or {}
    context = element.get("element_context") or {}
    attrs = fingerprint.get("attrs") or {}
    return {
        "id": element.get("id"),
        "name": element.get("name"),
        "page_url": element.get("page_url") or "",
        "tag": element.get("tag_name") or fingerprint.get("tag") or "",
        "role": element.get("element_role") or fingerprint.get("role") or "",
        "text": context.get("text") or fingerprint.get("text") or "",
        "label": context.get("label") or "",
        "accessible_name": context.get("accessible_name") or fingerprint.get("accessible_name") or "",
        "input_type": context.get("input_type") or attrs.get("type") or "",
        "frame_path": context.get("frame_path") or [],
        "shadow_hosts": context.get("shadow_hosts") or fingerprint.get("shadow_hosts") or [],
        "locators": element.get("locators") or [],
        "fingerprint": fingerprint,
        "fallback_position": context.get("fallback_position") or fingerprint.get("normalized_position") or {},
    }


def _generated_flow_filename(goal: str) -> str:
    english = re.sub(r"[^A-Za-z0-9_]+", "_", goal).strip("_").lower()[:48]
    stem = english or f"ai_agent_{int(time.time())}"
    if not stem.startswith("test_"):
        stem = f"test_{stem}"
    return f"{stem}.yaml"


def generate_flow_draft(goal: str, project_id: str, base_url: str = "") -> Optional[dict]:
    """Generate a review-only Web Flow from previously captured element assets."""
    elements = _collect_web_elements(project_id)
    if not elements:
        return None
    requested = urlsplit(base_url) if base_url else None
    requested_page = f"{requested.scheme}://{requested.netloc}{requested.path}" if requested and requested.scheme and requested.netloc else ""
    requested_host = requested.netloc if requested else ""
    if requested_page:
        matching_page = [item for item in elements if _page_key_for_element(item) == requested_page]
        if matching_page:
            elements = matching_page
    elif requested_host:
        matching_host = [item for item in elements if urlsplit(item.get("page_url") or "").netloc == requested_host]
        if matching_host:
            elements = matching_host

    goal_terms = _goal_terms(goal)
    plate_goal = bool(re.search(r"车牌|plate", goal, re.IGNORECASE))
    scored = []
    for element in elements:
        text = _element_search_text(element)
        control_type = str((element.get("fingerprint") or {}).get("control_type") or "").lower()
        if control_type in {"virtual_keyboard_key", "plate_input"} and not plate_goal:
            continue
        if _generated_action_for_element(element) == "click" and not _is_generated_click_candidate(element):
            continue
        term_score = sum(min(16, len(term) * 3) for term in goal_terms if term in text)
        role_bonus = 5 if _generated_action_for_element(element) in {"fill", "select", "check"} else 0
        usage_bonus = min(5, int(element.get("usage_count") or 0))
        scored.append((term_score + role_bonus + usage_bonus, element))
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            item[0],
            int(item[1].get("usage_count") or 0),
            str(item[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    scored = _dedupe_generated_candidates(scored)

    # 多页草稿：按页面首次出现时间排序，覆盖主流程涉及的前几个页面，
    # 避免只取单页 Top 元素导致草稿只有两三步。
    page_scores: dict[str, float] = defaultdict(float)
    page_first_seen: dict[str, str] = {}
    for score, element in scored:
        page_key = _page_key_for_element(element)
        page_scores[page_key] += score
        seen = str(element.get("updated_at") or "")
        if not page_first_seen.get(page_key) or seen < page_first_seen[page_key]:
            page_first_seen[page_key] = seen
    ordered_pages = [
        page for page in sorted(page_first_seen, key=page_first_seen.get)
        if page_scores.get(page, 0) > 0
    ][:3]

    candidates: list[dict] = []
    for page in ordered_pages:
        page_scored = [
            (score, element) for score, element in scored
            if _page_key_for_element(element) == page
        ][:14]
        has_semantic_match = any(score >= 10 for score, _ in page_scored)
        page_candidates = [
            element for score, element in page_scored
            if score >= 6 or (has_semantic_match and _generated_action_for_element(element) in {"fill", "select", "check"})
        ]
        if not page_candidates:
            page_candidates = [element for _, element in page_scored[:6]]
        candidates.extend(page_candidates)
    candidates = candidates[:18]

    ordered: list[dict] = []
    for page in ordered_pages:
        page_elements = [item for item in candidates if _page_key_for_element(item) == page]
        page_elements.sort(
            key=lambda item: (
                {"fill": 0, "select": 1, "check": 2, "click": 3}[_generated_action_for_element(item)],
                -int(item.get("usage_count") or 0),
            ),
        )
        ordered.extend(page_elements)
    generator_source = "rules"
    compact = [{
        "i": index,
        "n": str(item.get("name") or "")[:50],
        "a": _generated_action_for_element(item)[0],
    } for index, item in enumerate(ordered)]
    if compact:
        try:
            proposed = _extract_json_object(_call_model(
                "根据测试目标选择并排序元素。只输出最短 JSON：{\"s\":[元素index]}，不要解释。",
                _json_dump({"g": goal, "e": compact}),
                max_tokens=24,
            ))
        except Exception:
            proposed = {}
        selected_indexes = []
        for raw in proposed.get("s") or []:
            try:
                index = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(ordered) and index not in selected_indexes:
                selected_indexes.append(index)
        if selected_indexes:
            ordered = [ordered[index] for index in selected_indexes]
            generator_source = "ai"
    ordered, duplicate_count = _dedupe_generated_elements(ordered)
    if not ordered:
        return None

    start_url = base_url or str((ordered[0] if ordered else candidates[0]).get("page_url") or "")
    if not start_url.startswith(("http://", "https://")):
        return None
    steps = [{
        "id": f"step_{uuid.uuid4().hex[:12]}",
        "action": "goto",
        "name": f"打开 {start_url}",
        "url": start_url,
        "timeout": 30000,
    }]
    sensitive_actions = []
    required_variables = []
    current_page = _page_key_for_element(ordered[0]) if ordered else ""
    for index, element in enumerate(ordered, 1):
        action = _generated_action_for_element(element)
        page = _page_key_for_element(element)
        if page and page != current_page:
            steps.append({
                "id": f"step_{uuid.uuid4().hex[:12]}",
                "action": "goto",
                "name": f"打开 {str(element.get('page_url') or page)[:120]}",
                "url": element.get("page_url") or page,
                "timeout": 30000,
            })
            current_page = page
        snapshot = _element_snapshot(element)
        label = snapshot.get("accessible_name") or snapshot.get("name") or f"元素 {index}"
        prefix = {"fill": "输入", "select": "选择", "check": "勾选", "click": "点击"}[action]
        step = {
            "id": f"step_{uuid.uuid4().hex[:12]}",
            "action": action,
            "name": f"{prefix} {str(label)[:60]}",
            "element": snapshot,
            "timeout": 10000,
        }
        if action == "fill":
            step["value"] = _generated_value_for_element(element, index)
        elif action == "select":
            step["option_value"] = f"${{OPTION_VALUE_{index}}}"
        elif action == "click":
            step["wait_after"] = 0.2
        if action == "click" and SENSITIVE_ACTION_PATTERN.search(_element_search_text(element)):
            sensitive_actions.append(step["name"])
        for variable in FLOW_VARIABLE_PATTERN.findall(_json_dump(step)):
            if variable not in required_variables:
                required_variables.append(variable)
        steps.append(step)

    confidence = 0.8 if generator_source == "ai" else 0.65
    confidence -= min(0.2, len(required_variables) * 0.03)
    confidence -= 0.2 if sensitive_actions else 0
    confidence = round(max(0.2, min(0.95, confidence)), 2)
    review_reasons = ["AI 生成的新流程必须人工确认后才能执行"]
    if required_variables:
        review_reasons.append(f"需要配置变量：{', '.join(required_variables)}")
    if sensitive_actions:
        review_reasons.append(f"包含敏感操作：{'、'.join(sensitive_actions)}")
    if duplicate_count:
        review_reasons.append(f"已自动合并 {duplicate_count} 个语义重复步骤")
    flow = {
        "flow_version": 2,
        "name": f"AI Agent - {goal[:60]}",
        "description": f"由 AI Test Agent 根据已采集元素资产生成。置信度 {confidence}",
        "base_url": start_url,
        "browser": {"engine": "chromium", "viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"},
        "timeout": 10000,
        "steps": steps,
        "metadata": {
            "source": "ai_agent",
            "generator_source": generator_source,
            "generation_confidence": confidence,
            "required_variables": required_variables,
            "sensitive_actions": sensitive_actions,
            "review_required": True,
            "created_at": _now(),
        },
    }
    return {
        "flow": flow,
        "filename": _generated_flow_filename(goal),
        "confidence": confidence,
        "required_variables": required_variables,
        "sensitive_actions": sensitive_actions,
        "review_reasons": review_reasons,
        "generator_source": generator_source,
    }


def _goal_terms(goal: str) -> list[str]:
    normalized = re.sub(r"[\s，。！？、；：,.!?;:]+", "", goal.lower())
    for word in ("请帮我", "帮我", "麻烦", "测试一下", "测试", "验证", "流程", "功能", "页面"):
        normalized = normalized.replace(word, "")
    terms = re.findall(r"[a-z0-9_]{2,}", normalized)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    if chinese:
        terms.append(chinese)
        for size in (4, 3, 2):
            terms.extend(chinese[index:index + size] for index in range(max(0, len(chinese) - size + 1)))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))


def _asset_goal_score(goal: str, asset: dict) -> float:
    terms = _goal_terms(goal)
    if not terms:
        return 0
    normalized_goal = re.sub(r"\s+", "", goal.lower())
    haystack = " ".join([
        asset.get("filename", ""), asset.get("name", ""),
        asset.get("description", ""), *asset.get("step_names", []),
    ]).lower()
    compact = re.sub(r"\s+", "", haystack)
    term_score = sum(min(12, len(term) * 2) for term in terms if term in compact)
    similarity = SequenceMatcher(None, normalized_goal, compact[:500]).ratio() * 35
    return term_score + similarity


def _rank_case_assets(
    goal: str,
    assets: list[dict],
    *,
    include_auto_excluded: bool = False,
) -> list[tuple[float, str]]:
    ranked = []
    for asset in assets:
        if asset.get("auto_excluded") and not include_auto_excluded:
            continue
        score = _asset_goal_score(goal, asset)
        if score >= HEURISTIC_CASE_MIN_SCORE:
            ranked.append((score, asset["filename"]))
    ranked.sort(reverse=True)
    if not ranked:
        return []
    top_score = ranked[0][0]
    cutoff = max(HEURISTIC_CASE_MIN_SCORE, top_score * HEURISTIC_CASE_TOP_RATIO)
    return [(score, filename) for score, filename in ranked if score >= cutoff]


def _heuristic_case_selection(goal: str, assets: list[dict]) -> list[str]:
    ranked = _rank_case_assets(goal, assets)
    if ranked:
        return [filename for _, filename in ranked[:5]]
    available = [asset for asset in assets if not asset.get("auto_excluded")]
    if len(available) == 1:
        return [available[0]["filename"]]
    return []


def _filter_model_selected_files(goal: str, values: list[str], assets: list[dict]) -> list[str]:
    if not values:
        return []
    asset_map = {asset["filename"]: asset for asset in assets}
    scores = {
        asset["filename"]: _asset_goal_score(goal, asset)
        for asset in assets
        if not asset.get("auto_excluded")
    }
    top_score = max(scores.values(), default=0)
    cutoff = max(MODEL_CASE_MIN_SCORE, top_score * MODEL_CASE_TOP_RATIO)
    if top_score < MODEL_CASE_MIN_SCORE:
        return []
    selected = []
    for filename in values:
        asset = asset_map.get(filename)
        if not asset or asset.get("auto_excluded"):
            continue
        if scores.get(filename, 0) >= cutoff and filename not in selected:
            selected.append(filename)
    return selected


def _normalize_selected_files(values: Iterable[Any], assets: list[dict]) -> list[str]:
    valid = {asset["filename"]: asset["filename"] for asset in assets}
    valid.update({os.path.splitext(name)[0]: name for name in list(valid)})
    result = []
    for value in values or []:
        raw = os.path.basename(str(value or "").strip())
        selected = valid.get(raw) or valid.get(os.path.splitext(raw)[0])
        if selected and selected not in result:
            result.append(selected)
    return result


def _fallback_plan(goal: str, assets: list[dict], selected: list[str]) -> dict:
    asset_map = {asset["filename"]: asset for asset in assets}
    scenarios = []
    for index, filename in enumerate(selected, 1):
        asset = asset_map[filename]
        scenarios.append({
            "id": f"SC-{index:03d}",
            "name": asset.get("name") or filename,
            "objective": asset.get("description") or f"执行现有自动化资产 {filename}",
            "priority": "P0" if index == 1 else "P1",
            "case_file": filename,
            "executable": True,
        })
    if not scenarios:
        scenarios = [
            {"id": "SC-001", "name": "主业务链路", "objective": goal, "priority": "P0", "executable": False},
            {"id": "SC-002", "name": "异常与边界", "objective": "验证必填、边界和错误恢复", "priority": "P1", "executable": False},
            {"id": "SC-003", "name": "兼容与稳定性", "objective": "验证不同环境下的可重复执行", "priority": "P1", "executable": False},
        ]
    return {
        "title": goal[:80],
        "summary": f"围绕“{goal}”规划 Web UI 自动化测试",
        "risk_level": "medium" if selected else "high",
        "risks": [
            "关键业务流程中断",
            "页面结构变化导致元素定位失败",
            "测试数据或环境状态影响重复执行",
        ],
        "scenarios": scenarios,
        "selected_case_files": selected,
        "test_data": ["优先复用用例内已参数化数据", "敏感数据不得写入 Agent 日志"],
        "execution_strategy": "按优先级执行已匹配资产；定位失败时启用语义自愈和歧义保护",
        "coverage_gaps": [] if selected else ["没有找到与目标匹配的可执行 UI 用例，需要先录制或编排场景"],
        "planner_source": "rules",
    }


def build_test_plan(
    goal: str,
    assets: list[dict],
    requested_case_files: Optional[list[str]] = None,
) -> dict:
    requested = _normalize_selected_files(requested_case_files or [], assets)
    if not requested and goal:
        # 需求文本里显式点名的用例文件（如 test_xxx.yaml）直接匹配，避免
        # _goal_terms 去掉扩展名后启发式匹配失效，落到迷你 Flow 草稿。
        explicit = re.findall(
            r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]+\.ya?ml",
            goal,
            flags=re.IGNORECASE,
        )
        requested = _normalize_selected_files(explicit, assets)
    heuristic = requested or _heuristic_case_selection(goal, assets)
    fallback = _fallback_plan(goal, assets, heuristic)
    if not assets:
        return _attach_asset_snapshot(fallback, assets)

    preferred_files = set(heuristic)
    model_assets = [asset for asset in assets if asset["filename"] in preferred_files]
    model_assets.extend(
        asset for asset in assets
        if asset["filename"] not in preferred_files and not asset.get("auto_excluded")
    )
    compact_assets = [{
        "index": index,
        "filename": asset["filename"],
        "name": asset.get("name", ""),
        "description": asset.get("description", "")[:80],
    } for index, asset in enumerate(model_assets[:20])]
    system_prompt = (
        "你是测试任务路由器。资产有数字 index。只输出最短 JSON："
        "{\"i\":[匹配资产index],\"r\":\"L或M或H\"}。不要解释，不要输出文件名。"
    )
    user_prompt = _json_dump({
        "test_goal": goal,
        "requested_case_files": requested,
        "existing_assets": compact_assets,
    })
    try:
        proposed = _extract_json_object(_call_model(system_prompt, user_prompt, max_tokens=32))
    except Exception:
        return _attach_asset_snapshot(fallback, assets)
    if not proposed:
        return _attach_asset_snapshot(fallback, assets)

    indexed_selected = []
    for value in proposed.get("i") or []:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(compact_assets):
            indexed_selected.append(compact_assets[index]["filename"])
    selected = requested
    if not selected:
        raw_selected = _normalize_selected_files(
            indexed_selected or proposed.get("selected_case_files") or [],
            assets,
        )
        selected = _filter_model_selected_files(goal, raw_selected, assets)
    if not selected:
        selected = heuristic
    plan = dict(fallback)
    risk_value = str(proposed.get("r") or proposed.get("risk_level") or "").strip().lower()
    risk_map = {"l": "low", "m": "medium", "h": "high", "low": "low", "medium": "medium", "high": "high"}
    if selected and risk_value in risk_map:
        plan["risk_level"] = risk_map[risk_value]
    elif not selected:
        plan["risk_level"] = "high"
    plan["selected_case_files"] = selected
    plan["planner_source"] = "ai"
    if not selected and not plan.get("coverage_gaps"):
        plan["coverage_gaps"] = ["AI 未找到可安全执行的现有自动化资产"]
    return _attach_asset_snapshot(plan, assets)


def build_failure_report(task_id: str, result: dict, defects: list[dict]) -> tuple[dict, dict, dict]:
    summary = result.get("summary") or {}
    failures = result.get("failures") or []
    failure_analysis = {
        "executive_summary": (
            f"执行 {summary.get('total', 0)} 个检查，"
            f"通过 {summary.get('passed', 0)}，失败 {summary.get('failed', 0)}。"
        ),
        "attribution_summary": result.get("attribution_summary") or {},
        "top_suggestion": result.get("top_suggestion") or "",
        "failures": failures,
        "release_risk": "high" if failures else "low",
        "analyser_source": "rules",
    }
    if failures:
        prompt = {
            "execution_summary": summary,
            "failures": failures[:10],
            "existing_attribution": result.get("attribution_summary") or {},
        }
        try:
            refined = _extract_json_object(_call_model(
                "你是测试失败根因分析专家。基于证据输出严格 JSON，字段为 executive_summary、"
                "root_causes、recommendations、release_risk。不得把猜测写成事实。",
                _json_dump(prompt),
                max_tokens=192,
            ))
        except Exception:
            refined = {}
        if refined:
            failure_analysis.update(refined)
            failure_analysis["analyser_source"] = "ai"

    defect_items = []
    for index, failure in enumerate(failures, 1):
        attribution = failure.get("attribution") or {}
        linked = next((item for item in defects if item.get("tc_id") and item.get("tc_id") == failure.get("tc_id")), None)
        defect_items.append({
            "title": f"[UI自动化] {failure.get('scenario') or failure.get('name') or f'失败场景 {index}'}",
            "severity": "高" if attribution.get("category") == "被测系统缺陷" else "中",
            "steps": ["执行 Agent 选择的自动化用例", "等待失败步骤出现", "查看截图、日志和 Allure 报告"],
            "actual": failure.get("error_message") or "自动化检查失败",
            "expected": "业务流程和断言全部通过",
            "category": attribution.get("category") or "未知",
            "suggestion": attribution.get("suggestion") or "",
            "linked_defect_id": (linked or {}).get("id", ""),
        })
    defect_draft = {
        "task_id": task_id,
        "count": len(defect_items),
        "items": defect_items,
        "linked_defect_ids": [item.get("id") for item in defects if item.get("id")],
    }
    result_summary = {
        "test_status": "failed" if failures else "passed",
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "skipped": summary.get("skipped", 0),
        "report_url": result.get("allure_report_url") or "",
        "defect_count": len(defects),
    }
    return result_summary, failure_analysis, defect_draft


def _missing_flow_variables(project_id: str, filenames: list[str]) -> list[str]:
    required = []
    for filename in filenames:
        try:
            content = case_service.get_case(project_id, "ui", filename)
        except (FileNotFoundError, ValueError):
            continue
        for variable in FLOW_VARIABLE_PATTERN.findall(content):
            if variable not in required:
                required.append(variable)
    return [
        name for name in required
        if name not in RUNTIME_GENERATED_VARIABLES and os.getenv(name) in (None, "")
    ]


def save_generated_flow(task_id: str) -> dict:
    task = get_agent_task(task_id)
    if not task:
        raise ValueError("Agent 任务不存在")
    if task["status"] not in {"review_required", "ready"}:
        raise ValueError(f"当前状态 {task['status']} 不能保存生成流程")
    plan = dict(task.get("plan") or {})
    generated = plan.get("generated_flow") or {}
    if not isinstance(generated, dict) or int(generated.get("flow_version") or 0) != 2:
        raise ValueError("Agent 任务中没有可保存的生成流程")
    filename = os.path.basename(str(plan.get("generated_flow_filename") or _generated_flow_filename(task["goal"])))
    stem, extension = os.path.splitext(filename)
    extension = extension or ".yaml"
    candidate = f"{stem}{extension}"
    suffix = 2
    while True:
        try:
            case_service.get_case(task["project_id"], "ui", candidate)
        except FileNotFoundError:
            break
        candidate = f"{stem}_{suffix}{extension}"
        suffix += 1
    metadata = dict(generated.get("metadata") or {})
    metadata.update({
        "source": "ai_agent",
        "agent_task_id": task_id,
        "reviewed_at": _now(),
        "review_required": False,
    })
    generated = dict(generated)
    steps, duplicate_count = _dedupe_generated_flow_steps(generated.get("steps") or [])
    for index, step in enumerate(steps, 1):
        _normalize_generated_step_value(step, index)
    metadata["required_variables"] = list(dict.fromkeys(FLOW_VARIABLE_PATTERN.findall(_json_dump(steps))))
    if duplicate_count:
        metadata["deduplicated_steps"] = duplicate_count
    generated["steps"] = steps
    generated["metadata"] = metadata
    content = yaml.safe_dump(generated, allow_unicode=True, sort_keys=False, width=120)
    case_service.save_case(task["project_id"], "ui", candidate, content)
    plan["generated_flow"] = generated
    plan["generated_flow_filename"] = candidate
    plan["generated_flow_saved"] = True
    plan["selected_case_files"] = [candidate]
    plan["missing_variables"] = _missing_flow_variables(task["project_id"], [candidate])
    plan["coverage_gaps"] = []
    _update_task(task_id, status="ready", progress=45, plan=plan)
    _add_event(
        task_id,
        "generation",
        f"AI Flow 草稿已保存为 {candidate}",
        payload={"filename": candidate, "missing_variables": plan["missing_variables"]},
    )
    return get_agent_task(task_id)


def _analyse_execution(agent_task_id: str, execution_task_id: str) -> None:
    current = get_agent_task(agent_task_id)
    if not current or current["status"] == "stopped":
        return
    _update_task(agent_task_id, status="analyzing", progress=85)
    _add_event(agent_task_id, "analysis", "正在汇总 Allure 证据、失败归因和缺陷记录")
    result = analysis.analyze_task(execution_task_id)
    if result.get("error"):
        raise RuntimeError(result["error"])
    defect_rows = db.execute(
        "SELECT * FROM defects WHERE task_id=? ORDER BY created_at",
        (execution_task_id,),
        fetch=True,
    )
    defects = db.to_dicts(defect_rows)
    result_summary, failure_analysis, defect_draft = build_failure_report(
        execution_task_id, result, defects,
    )
    _update_task(
        agent_task_id,
        status="completed",
        progress=100,
        result_summary=result_summary,
        failure_analysis=failure_analysis,
        defect_draft=defect_draft,
        error_message="",
        finished_at=_now(),
    )
    conclusion = "测试通过" if result_summary["test_status"] == "passed" else "发现失败并已生成缺陷证据"
    _add_event(
        agent_task_id,
        "completed",
        f"Agent 任务完成：{conclusion}",
        payload=result_summary,
    )


def _wait_for_execution(agent_task_id: str, execution_task_id: str) -> None:
    timeout = int(os.environ.get("AI_AGENT_EXECUTION_TIMEOUT", "86400"))
    deadline = time.time() + max(60, timeout)
    last_status = ""
    while time.time() < deadline:
        agent_task = get_agent_task(agent_task_id)
        if not agent_task or agent_task["status"] == "stopped":
            try:
                executor.stop_task(execution_task_id)
            except Exception:
                pass
            return
        rows = db.execute("SELECT * FROM tasks WHERE id=?", (execution_task_id,), fetch=True)
        if not rows:
            raise RuntimeError("底层执行任务不存在")
        execution = db.to_dict(rows[0])
        status = execution.get("status") or "pending"
        if status != last_status:
            _add_event(
                agent_task_id,
                "execution",
                f"底层执行状态变更为 {status}",
                payload={"execution_task_id": execution_task_id, "status": status},
            )
            last_status = status
        if status in EXECUTION_TERMINAL_STATUSES:
            if status == "stopped":
                _update_task(agent_task_id, status="stopped", progress=100, finished_at=_now())
                _add_event(agent_task_id, "stopped", "底层测试执行已停止", level="warning")
                return
            _analyse_execution(agent_task_id, execution_task_id)
            return
        time.sleep(1)
    raise TimeoutError("等待底层测试执行完成超时")


def _execute_plan(task: dict) -> None:
    plan = task.get("plan") or {}
    selected = plan.get("selected_case_files") or []
    if not selected:
        _update_task(task["id"], status="ready", progress=40)
        _add_event(
            task["id"],
            "planning",
            "规划已完成，但没有匹配到可安全执行的自动化资产",
            level="warning",
            payload={"coverage_gaps": plan.get("coverage_gaps") or []},
        )
        return
    missing_variables = _missing_flow_variables(task["project_id"], selected)
    if missing_variables:
        plan = dict(plan)
        plan["missing_variables"] = missing_variables
        _update_task(task["id"], status="review_required", progress=45, plan=plan)
        _add_event(
            task["id"],
            "safety",
            f"执行已拦截：需要先配置变量 {', '.join(missing_variables)} 或在场景编排中填写测试数据",
            level="warning",
            payload={"missing_variables": missing_variables},
        )
        return
    _update_task(task["id"], status="executing", progress=50)
    _add_event(
        task["id"],
        "execution",
        f"开始执行 {len(selected)} 个 UI 自动化资产",
        payload={"case_files": selected},
    )
    execution_task_id = executor.create_task(
        task["project_id"],
        "ui",
        selected,
        triggered_by=f"ai-agent:{task['id']}",
        env=task.get("env") or "test",
        base_url=task.get("base_url") or "",
    )
    _update_task(task["id"], execution_task_id=execution_task_id, progress=55)
    executor.dispatch(execution_task_id)
    _wait_for_execution(task["id"], execution_task_id)


def _refresh_plan_for_execution(task: dict) -> dict:
    """执行前基于磁盘上的最新 YAML 重新规划，不复用任务里的旧资产快照。"""
    assets = _collect_case_assets(task["project_id"])
    context = task.get("context") or {}
    previous_plan = task.get("plan") or {}
    requested = context.get("requested_case_files") or []
    # 人工评审后保存的生成 Flow 属于明确选择，刷新时继续锁定该文件；
    # 普通 Agent 计划则按最新资产重新匹配目标，允许新增/删除用例生效。
    if previous_plan.get("generated_flow_saved"):
        requested = previous_plan.get("selected_case_files") or requested
    refreshed_plan = build_test_plan(task["goal"], assets, requested)
    if previous_plan.get("generated_flow_saved"):
        for key in (
            "generated_flow_saved",
            "generated_flow_filename",
            "generated_flow",
            "generation_confidence",
            "generator_source",
            "required_variables",
            "sensitive_actions",
            "review_reasons",
        ):
            if key in previous_plan:
                refreshed_plan[key] = previous_plan[key]
    _update_task(task["id"], plan=refreshed_plan, error_message="")
    _add_event(
        task["id"],
        "planning",
        f"执行前已刷新最新 UI 用例，共选择 {len(refreshed_plan.get('selected_case_files') or [])} 个资产",
        level="success",
        payload={
            "selected_case_files": refreshed_plan.get("selected_case_files") or [],
            "asset_snapshot": refreshed_plan.get("asset_snapshot") or [],
            "fresh_content": True,
        },
    )
    return get_agent_task(task["id"])


def _run_agent_task(task_id: str, force_execute: bool = False, replan: bool = False) -> None:
    try:
        task = get_agent_task(task_id)
        if not task or task["status"] == "stopped":
            return
        plan = task.get("plan") or {}
        if replan or not plan:
            _update_task(task_id, status="planning", progress=10, error_message="", finished_at=None)
            _add_event(task_id, "planning", "正在理解测试目标并分析现有测试资产")
            assets = _collect_case_assets(task["project_id"])
            context = task.get("context") or {}
            plan = build_test_plan(task["goal"], assets, context.get("requested_case_files") or [])
            generated = None
            if not plan.get("selected_case_files"):
                generated = generate_flow_draft(
                    task["goal"],
                    task["project_id"],
                    task.get("base_url") or "",
                )
                if generated:
                    plan["generated_flow"] = generated["flow"]
                    plan["generated_flow_filename"] = generated["filename"]
                    plan["generation_confidence"] = generated["confidence"]
                    plan["generator_source"] = generated["generator_source"]
                    plan["required_variables"] = generated["required_variables"]
                    plan["sensitive_actions"] = generated["sensitive_actions"]
                    plan["review_reasons"] = generated["review_reasons"]
                    plan["coverage_gaps"] = []
            next_status = "review_required" if generated else "ready"
            _update_task(task_id, status=next_status, progress=40, plan=plan)
            _add_event(
                task_id,
                "planning",
                f"测试计划已生成，匹配 {len(plan.get('selected_case_files') or [])} 个可执行资产",
                payload={
                    "planner_source": plan.get("planner_source"),
                    "risk_level": plan.get("risk_level"),
                    "selected_case_files": plan.get("selected_case_files") or [],
                },
            )
            if generated:
                _add_event(
                    task_id,
                    "generation",
                    f"未找到现成用例，已根据 {len(generated['flow'].get('steps') or []) - 1} 个元素动作生成 Flow 草稿",
                    level="warning",
                    payload={
                        "confidence": generated["confidence"],
                        "generator_source": generated["generator_source"],
                        "review_reasons": generated["review_reasons"],
                    },
                )
            task = get_agent_task(task_id)
        if task.get("status") != "review_required" and (force_execute or task.get("auto_execute")):
            if force_execute:
                task = _refresh_plan_for_execution(task)
            _execute_plan(task)
    except Exception as exc:
        _update_task(
            task_id,
            status="failed",
            progress=100,
            error_message=str(exc)[:2000],
            finished_at=_now(),
        )
        _add_event(task_id, "failed", f"Agent 任务失败：{exc}", level="error")
    finally:
        with _active_lock:
            _active_tasks.discard(task_id)


def start_agent_task(task_id: str, *, force_execute: bool = False, replan: bool = False) -> bool:
    with _active_lock:
        if task_id in _active_tasks:
            return False
        _active_tasks.add(task_id)
    thread = threading.Thread(
        target=_run_agent_task,
        args=(task_id, force_execute, replan),
        daemon=True,
        name=f"ai-test-agent-{task_id}",
    )
    thread.start()
    return True


def execute_agent_task(task_id: str) -> dict:
    task = get_agent_task(task_id)
    if not task:
        raise ValueError("Agent 任务不存在")
    if task["status"] not in {"ready", "failed", "review_required"}:
        raise ValueError(f"当前状态 {task['status']} 不允许执行")
    plan = dict(task.get("plan") or {})
    if plan.get("generated_flow") and not plan.get("generated_flow_saved"):
        raise ValueError("请先确认并保存 AI 生成的 Flow 草稿")
    selected = plan.get("selected_case_files") or []
    if plan and not selected:
        raise ValueError("当前计划没有可执行资产，请先录制或编排场景")
    if selected:
        missing_variables = _missing_flow_variables(task["project_id"], selected)
        if missing_variables:
            plan["missing_variables"] = missing_variables
            _update_task(task_id, status="review_required", progress=max(45, int(task.get("progress") or 0)), plan=plan)
            raise ValueError(
                f"仍缺少变量 {', '.join(missing_variables)}，请先在场景编排中填写测试数据或配置环境变量"
            )
        plan.pop("missing_variables", None)
    if task["status"] == "review_required":
        _update_task(task_id, status="ready", progress=max(45, int(task.get("progress") or 0)), plan=plan)
        _add_event(task_id, "approval", "人工确认已完成，继续执行测试计划")
        task = get_agent_task(task_id)
    elif selected:
        _update_task(task_id, plan=plan)
        task = get_agent_task(task_id)
    if task["status"] == "failed" and not task.get("plan"):
        start_agent_task(task_id, force_execute=True, replan=True)
    else:
        start_agent_task(task_id, force_execute=True)
    return get_agent_task(task_id)


def retry_agent_task(task_id: str) -> dict:
    task = get_agent_task(task_id)
    if not task:
        raise ValueError("Agent 任务不存在")
    if task["status"] not in TERMINAL_STATUSES | {"ready", "review_required"}:
        raise ValueError("任务正在运行，不能重试")
    _update_task(
        task_id,
        status="pending",
        progress=0,
        plan={},
        execution_task_id="",
        result_summary={},
        failure_analysis={},
        defect_draft={},
        error_message="",
        finished_at=None,
    )
    _add_event(task_id, "retry", "已重新提交 Agent 任务")
    start_agent_task(task_id, replan=True)
    return get_agent_task(task_id)


def stop_agent_task(task_id: str) -> dict:
    task = get_agent_task(task_id)
    if not task:
        raise ValueError("Agent 任务不存在")
    if task["status"] in TERMINAL_STATUSES:
        return task
    _update_task(task_id, status="stopped", progress=100, finished_at=_now())
    if task.get("execution_task_id"):
        try:
            executor.stop_task(task["execution_task_id"])
        except Exception:
            pass
    _add_event(task_id, "stopped", "用户停止了 Agent 任务", level="warning")
    return get_agent_task(task_id)


def delete_agent_task(task_id: str) -> None:
    task = get_agent_task(task_id)
    if not task:
        raise ValueError("Agent 任务不存在")
    if task["status"] not in TERMINAL_STATUSES | {"ready", "review_required"}:
        raise ValueError("运行中的 Agent 任务不能删除")
    db.execute("DELETE FROM ai_agent_events WHERE agent_task_id=?", (task_id,))
    db.execute("DELETE FROM ai_agent_tasks WHERE id=?", (task_id,))


def recover_incomplete_tasks() -> None:
    """Resume durable Agent tasks after the API process restarts."""
    rows = db.execute(
        "SELECT id,status,execution_task_id FROM ai_agent_tasks "
        "WHERE status IN ('pending','planning','executing','analyzing') ORDER BY created_at",
        fetch=True,
    ) or []
    for row in rows:
        item = db.to_dict(row)
        if item.get("status") in {"executing", "analyzing"} and item.get("execution_task_id"):
            task_id = item["id"]
            with _active_lock:
                if task_id in _active_tasks:
                    continue
                _active_tasks.add(task_id)

            def _resume(agent_task_id=task_id, execution_task_id=item["execution_task_id"]):
                try:
                    _wait_for_execution(agent_task_id, execution_task_id)
                except Exception as exc:
                    _update_task(
                        agent_task_id,
                        status="failed",
                        progress=100,
                        error_message=str(exc)[:2000],
                        finished_at=_now(),
                    )
                    _add_event(agent_task_id, "failed", f"恢复 Agent 任务失败：{exc}", level="error")
                finally:
                    with _active_lock:
                        _active_tasks.discard(agent_task_id)

            threading.Thread(target=_resume, daemon=True, name=f"ai-test-agent-resume-{task_id}").start()
        else:
            _update_task(item["id"], status="pending")
            start_agent_task(item["id"], replan=True)
