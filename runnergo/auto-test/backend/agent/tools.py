"""AI Agent 工具注册表。

已注册工具：
    generate_data  测试数据生成（写入 test_assets）
    create_case    AI 生成测试用例
    run_api_test   执行 API 测试（OpenAI 兼容安全校验）
    run_ui_test    执行 UI 自动化（复用现有 executor）
    run_app_test   执行 APP 自动化（testhub 承载，本节点未配置时结构化跳过）
    create_report  生成测试报告（写入 runtime/agent-reports）

Agent 根据任务类型自动选择工具，调用结果统一写入 test_execution_result。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
import zlib
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
import yaml

from backend import db, settings
from backend.agent.memory import remember
from backend.llm.provider import chat_json, is_configured, ModelProviderError
from backend.url_security import validate_outbound_url


_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
_GIVEN_NAMES = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "秀英", "霞", "平", "刚", "桂英", "文", "辉", "力", "建华", "国庆", "志强", "丽丽", "小红", "小明", "雨欣", "梓涵", "浩然", "欣怡", "宇轩", "子墨", "一诺", "思远", "佳琪", "天佑", "雨桐", "晨曦"]
_EMAIL_DOMAINS = ["example.com", "test.cn", "runnergo.dev", "mail.test"]
_PROVINCES = ["北京市", "上海市", "广东省", "浙江省", "江苏省", "四川省", "湖北省", "湖南省", "山东省", "福建省"]
_CITIES = ["朝阳区", "浦东新区", "天河区", "西湖区", "鼓楼区", "武侯区", "洪山区", "岳麓区", "历下区", "思明区"]
_STREETS = ["人民路", "中山路", "建设路", "解放路", "科技路", "创新大道", "梧桐街", "云杉路", "海蓝路", "朝阳街"]
_PHONE_PREFIXES = (
    "134", "135", "136", "137", "138", "139", "147", "148", "150", "151", "152",
    "157", "158", "159", "165", "172", "178", "182", "183", "184", "187", "188",
    "195", "197", "198",
    "130", "131", "132", "145", "146", "155", "156", "166", "171", "175", "176",
    "185", "186", "196",
    "133", "149", "153", "173", "177", "180", "181", "189", "190", "191", "192",
    "193", "199",
)


_FIELD_GENERATORS = {
    "name": lambda i, r: r.choice(list(_SURNAMES)) + r.choice(_GIVEN_NAMES),
    "username": lambda i, r: f"user_{i:06d}",
    "phone": lambda i, r: f"{r.choice(_PHONE_PREFIXES)}{r.randint(10000000, 99999999)}",
    "mobile": lambda i, r: f"{r.choice(_PHONE_PREFIXES)}{r.randint(10000000, 99999999)}",
    "email": lambda i, r: f"user_{i:06d}@{r.choice(_EMAIL_DOMAINS)}",
    "password": lambda i, r: f"Passw0rd@{r.randint(1000, 9999)}",
    "id_card": lambda i, r: f"{r.randint(110000, 659000)}19{r.randint(60, 99)}{r.randint(1, 12):02d}{r.randint(1, 28):02d}{r.randint(1000, 9999)}{r.choice('0123456789X')}",
    "address": lambda i, r: f"{r.choice(_PROVINCES)}{r.choice(_CITIES)}{r.choice(_STREETS)}{r.randint(1, 300)}号",
    "company": lambda i, r: f"{r.choice(['云启', '星图', '恒信', '蓝桥', '极光', '聚点'])}{r.choice(['科技', '网络', '信息', '数据', '智能'])}有限公司",
    "search": lambda i, r: r.choice(["手机", "笔记本电脑", "蓝牙耳机", "运动鞋", "保温杯", "键盘", "显示器", "背包", "连衣裙", "扫地机器人"]),
    "amount": lambda i, r: f"{r.randint(1, 9999)}.00",
    "age": lambda i, r: str(r.randint(18, 60)),
    "gender": lambda i, r: r.choice(["男", "女"]),
    "code": lambda i, r: f"{r.randint(100000, 999999)}",
    "verification_code": lambda i, r: f"{r.randint(100000, 999999)}",
}


def _field_generator(field_name: str) -> Optional[Callable[[int, random.Random], Any]]:
    """Resolve duplicate input names such as phone_2 to their base generator."""
    generator = _FIELD_GENERATORS.get(field_name)
    if generator:
        return generator
    match = re.fullmatch(r"(.+)_\d+", field_name)
    return _FIELD_GENERATORS.get(match.group(1)) if match else None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _truncate(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _next_asset_id() -> str:
    return f"data_{uuid.uuid4().hex[:10]}"


def _publish_rows_to_testhub_api(
    *,
    asset_type: str,
    name: str,
    rows: list[dict],
    tags: list[str],
    binding: Optional[dict] = None,
    api_url: str = "",
) -> dict:
    url = (api_url or os.environ.get("TEST_DATA_CENTER_API_URL", "")).strip()
    if not url:
        return {"synced": False, "reason": "api_url_missing"}
    token = os.environ.get("TEST_DATA_CENTER_AGENT_TOKEN", "runnergo-local-agent-token")
    try:
        response = requests.post(
            url.rstrip("/") + "/api/core/test-data-assets/agent-publish/",
            json={
                "asset_type": str(asset_type or "CUSTOM").upper(),
                "name": name[:200],
                "rows": rows,
                "tags": tags,
                **(binding or {}),
            },
            headers={"X-Agent-Token": token},
            timeout=10,
        )
        if response.status_code >= 400:
            return {
                "synced": False,
                "reason": f"http_{response.status_code}",
                "response": _truncate(response.text, 500),
            }
        payload = response.json()
        return {
            "synced": True,
            "asset_id": payload.get("id"),
            "api_url": url,
            "tags": payload.get("tags") or tags,
            "requirement": payload.get("requirement"),
        }
    except requests.RequestException as exc:
        return {"synced": False, "reason": str(exc), "api_url": url}


def _sync_rows_to_test_data_center(
    *,
    asset_type: str,
    name: str,
    rows: list[dict],
    tags: Optional[list[str]] = None,
    source_asset_id: str = "",
    binding: Optional[dict] = None,
    api_url: str = "",
) -> dict:
    """Best-effort publish into the TestHub test data center (API only).

    不再跨容器直写 TestHub 的 SQLite 文件：该路径绕过 Django ORM 且与
    迁移 schema 耦合，多容器并发写存在锁冲突与数据损坏风险。API 不可用时
    返回结构化失败（synced=False + reason），由调用方决定是否终止任务。
    """
    clean_rows = [row for row in rows if isinstance(row, dict)]
    if not clean_rows:
        return {"synced": False, "reason": "empty_rows"}
    tag_values = list(dict.fromkeys([
        "ai-generated",
        "auto-test-agent",
        *(tags or []),
        *((binding or {}).get("binding_tags") or []),
        *([f"auto-test-asset-{source_asset_id}"] if source_asset_id else []),
    ]))
    result: dict = {"synced": False, "reason": "unreachable"}
    for attempt in range(2):
        result = _publish_rows_to_testhub_api(
            asset_type=asset_type,
            name=name,
            rows=clean_rows,
            tags=tag_values,
            binding=binding,
            api_url=api_url,
        )
        if result.get("synced"):
            return result
        if attempt == 0:
            time.sleep(0.5)
    return result


# ============ 工具实现 ============

def generate_test_data(
    asset_type: str = "USER",
    count: int = 100,
    fields: Optional[list[str]] = None,
    *,
    project_id: str = "",
    created_by: str = "",
    binding: Optional[dict] = None,
    api_url: str = "",
    code_length: int = 6,
) -> dict:
    """生成测试数据并写入 test_assets。"""
    count = max(1, min(int(count or 100), 10000))
    asset_type = str(asset_type or "USER").upper()
    field_names = [str(f).strip().lower() for f in (fields or []) if str(f).strip()]
    default_fields = {
        "USER": ["name", "phone", "email", "password"],
        "ORDER": ["order_no", "amount", "phone"],
        "PRODUCT": ["search", "name", "amount"],
    }
    if not field_names:
        field_names = list(default_fields.get(asset_type, default_fields["USER"]))
    supported = set(_FIELD_GENERATORS)
    unknown = [f for f in field_names if not _field_generator(f) and not re.fullmatch(r"input_\d+", f)]
    if unknown:
        raise ValueError(f"不支持的字段: {', '.join(unknown)}，可用: {', '.join(sorted(supported))}")

    rng = random.Random(f"{asset_type}:{count}:{int(time.time() * 1000)}:{os.getpid()}")
    code_len = max(1, min(12, int(code_length or 6)))
    code_min = 10 ** (code_len - 1)
    code_max = 10 ** code_len - 1
    seen_per_field: dict[str, set] = {f: set() for f in field_names}
    rows = []
    for index in range(1, count + 1):
        row = {"id": index}
        for field in field_names:
            if field in ("code", "verification_code"):
                value = None
                for _ in range(200):
                    candidate = f"{rng.randint(code_min, code_max)}"
                    if candidate not in seen_per_field[field]:
                        value = candidate
                        break
                if value is None:
                    value = f"{rng.randint(code_min, code_max)}_{index}"
                seen_per_field[field].add(value)
                row[field] = value
                continue
            generator = _field_generator(field)
            if generator:
                value = None
                for _ in range(200):
                    candidate = generator(index, rng)
                    if candidate not in seen_per_field[field]:
                        value = candidate
                        break
                if value is None:
                    value = f"{generator(index, rng)}_{index}"
                seen_per_field[field].add(value)
                row[field] = value
            else:
                row[field] = f"test_{index}_{field}"
        rows.append(row)

    asset_id = _next_asset_id()
    now = _now()
    db.execute(
        "INSERT INTO test_assets(id, asset_type, name, project_id, source, count, fields, payload, status, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            asset_id,
            asset_type,
            f"{asset_type} 测试数据 x{count}",
            project_id or "",
            "agent",
            count,
            json.dumps(field_names, ensure_ascii=False),
            json.dumps({"rows": rows, "fields": field_names}, ensure_ascii=False),
            "success",
            created_by or "",
            now,
            now,
        ),
    )
    data_center_asset = _sync_rows_to_test_data_center(
        asset_type=asset_type,
        name=f"{asset_type} 测试数据 x{count}",
        rows=rows,
        tags=[asset_type.lower(), "agent-generated-data"],
        source_asset_id=asset_id,
        binding=binding,
        api_url=api_url,
    )
    return {
        "asset_id": asset_id,
        "data_center_asset": data_center_asset,
        "count": count,
        "status": "success",
        "fields": field_names,
        "sample": rows[:3],
        "binding": binding or {},
    }


def _ui_case_path(project_id: str, case_file: str) -> str:
    from backend import executor
    return executor._case_file_path(project_id, "ui", case_file)


def _ui_target_id(project_id: str, case_file: str) -> int:
    stable_key = f"{project_id}:ui:{str(case_file).replace(chr(92), '/')}"
    return (zlib.crc32(stable_key.encode("utf-8")) & 0x7FFFFFFF) or 1


def _field_name_for_input(field: Any, index: int) -> str:
    haystack = " ".join([
        str(getattr(field, "element", "") or ""),
        str(getattr(field, "step_name", "") or ""),
        str(getattr(field, "step_id", "") or ""),
    ]).lower()
    candidates = (
        ("phone", ("手机号", "手机", "电话", "phone", "mobile", "tel")),
        ("password", ("密码", "password", "pwd")),
        ("verification_code", ("验证码", "短信码", "otp", "captcha", "code")),
        ("email", ("邮箱", "email", "mail")),
        ("username", ("账号", "用户名", "username", "account", "login")),
        ("name", ("姓名", "昵称", "name")),
        ("search", ("搜索", "关键字", "search", "keyword")),
        ("amount", ("金额", "价格", "amount", "price")),
        ("address", ("地址", "address")),
    )
    for name, keywords in candidates:
        if any(keyword in haystack for keyword in keywords):
            return name
    return f"input_{index}"


def prepare_ui_data_binding(
    *,
    project_id: str,
    case_files: list[str],
    agent_task_id: str,
    count: int = 1,
    existing_bindings: Optional[list[dict]] = None,
) -> dict:
    """Generate one bound data asset per selected YAML without changing source files."""
    from core.runtime_case import scan_input_fields

    prepared = []
    supplied_bindings = [item for item in (existing_bindings or []) if isinstance(item, dict)]
    for case_index, case_file in enumerate(case_files or [], 1):
        path = _ui_case_path(project_id, case_file)
        if not path or not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            case_data = yaml.safe_load(handle) or {}
        input_fields = scan_input_fields(case_data)
        if not input_fields:
            continue
        supplied = next((
            item for item in supplied_bindings
            if str(item.get("target_id") or item.get("target_case_name") or "") == str(case_file)
        ), None)
        if supplied is None and len(supplied_bindings) == len(case_files or []):
            supplied = supplied_bindings[case_index - 1]
        supplied_fields = [
            str(item) for item in ((supplied or {}).get("fields") or []) if str(item).strip()
        ]
        field_names = []
        overrides = []
        alias = str((supplied or {}).get("alias") or '').strip()
        if not alias:
            alias = re.sub(r"[^A-Za-z0-9_]", "_", f"agent_{agent_task_id}_{case_index}")[:100]
        for field_index, field in enumerate(input_fields, 1):
            field_name = _field_name_for_input(field, field_index)
            if supplied_fields:
                matching_field = next((
                    item for item in supplied_fields
                    if item not in field_names
                    and (
                        item.lower() == field_name.lower()
                        or re.fullmatch(rf"{re.escape(field_name)}_\d+", item, re.I)
                    )
                ), None)
                if matching_field is None:
                    # A reused asset must never replace a semantic input with an
                    # unrelated leftover field. Keep the YAML value so its own
                    # dataAssets expression can resolve at execution time.
                    continue
                field_name = matching_field
            if field_name in field_names:
                field_name = f"{field_name}_{field_index}"
            field_names.append(field_name)
            overrides.append({
                "stepId": field.step_id,
                "stepPath": field.step_path,
                "runtimeValue": f"${{dataAssets.{alias}.{field_name}}}",
            })
        target_id = _ui_target_id(project_id, case_file)
        binding_tag = f"agent-bind-ui-{agent_task_id}-{target_id}"
        if supplied:
            generated = {
                "asset_id": supplied.get("asset_id"),
                "count": count,
                "fields": supplied_fields,
                "status": "success",
                "reused": True,
                "data_center_asset": {
                    "synced": True,
                    "asset_id": supplied.get("asset_id"),
                    "requirement_id": supplied.get("requirement_id"),
                },
            }
        else:
            generated = generate_test_data(
                asset_type="CUSTOM",
                count=count,
                fields=field_names,
                project_id=project_id,
                created_by=agent_task_id,
                binding={
                    "target_type": "ui_automation",
                    "target_case_id": target_id,
                    "target_case_name": str(case_file),
                    "alias": alias,
                    "binding_tags": [binding_tag],
                },
            )
            if not generated.get("data_center_asset", {}).get("synced"):
                raise RuntimeError(f"测试数据中心绑定失败: {generated['data_center_asset'].get('reason', 'unknown')}")
        prepared.append({
            "case_file": case_file,
            "target_case_id": target_id,
            "alias": alias,
            "fields": field_names,
            "runtime_override": overrides,
            "runtime_variables": {
                f"AGENT_DATA_{index}": f"${{dataAssets.{alias}.{field_name}}}"
                for index, field_name in enumerate(field_names, 1)
            },
            "asset": generated,
        })
    return {"status": "success", "bindings": prepared, "count": len(prepared)}


def list_test_assets(project_id: str = "", limit: int = 100) -> list[dict]:
    """列出测试数据资产（不含全量 payload，避免响应过大）。"""
    limit = max(1, min(int(limit or 100), 500))
    if project_id:
        rows = db.execute(
            "SELECT id, asset_type, name, project_id, source, count, fields, status, created_by, created_at "
            "FROM test_assets WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
            fetch=True,
        )
    else:
        rows = db.execute(
            "SELECT id, asset_type, name, project_id, source, count, fields, status, created_by, created_at "
            "FROM test_assets ORDER BY created_at DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
    assets = []
    for row in rows or []:
        item = db.to_dict(row)
        item["fields"] = json.loads(item.get("fields") or "[]")
        assets.append(item)
    return assets


def get_test_asset(asset_id: str) -> Optional[dict]:
    """读取测试数据资产详情（含 sample 数据）。"""
    rows = db.execute("SELECT * FROM test_assets WHERE id=?", (asset_id,), fetch=True)
    if not rows:
        return None
    item = db.to_dict(rows[0])
    item["fields"] = json.loads(item.get("fields") or "[]")
    payload = json.loads(item.get("payload") or "{}")
    item["sample"] = (payload.get("rows") or [])[:20]
    item.pop("payload", None)
    return item


def create_case(
    requirement: str,
    dimension_plan: Optional[dict] = None,
    *,
    project_id: str = "",
) -> dict:
    """AI 生成测试用例：基于维度计划（功能/异常/边界/安全/性能）生成用例骨架。"""
    from backend.agent.planner import build_test_dimension_plan

    plan = dimension_plan or build_test_dimension_plan(requirement)
    cases = []
    seq = 0
    for dimension, items in plan.items():
        for item in items:
            seq += 1
            cases.append({
                "id": f"TC-{seq:03d}",
                "dimension": dimension,
                "title": item.get("title", ""),
                "preconditions": item.get("preconditions", ""),
                "steps": item.get("steps", []),
                "test_data": item.get("test_data", ""),
                "expected": item.get("expected", ""),
            })
    return {
        "status": "success",
        "requirement": requirement,
        "total": len(cases),
        "cases": cases,
    }


def run_api_test(
    *,
    method: str = "GET",
    url: str = "",
    base_url: str = "",
    headers: Optional[dict] = None,
    body: Any = None,
    timeout: int = 15,
) -> dict:
    """执行 API 测试（带 SSRF 安全校验）。"""
    method = str(method or "GET").upper()
    raw_url = str(url or "").strip()
    if not raw_url:
        raise ValueError("API 测试需要 url（可省略协议，使用 base_url 拼接）")
    if urlsplit(raw_url).scheme:
        target = raw_url
    else:
        target = urljoin(str(base_url or "").rstrip("/") + "/", raw_url.lstrip("/"))
    if not urlsplit(target).scheme:
        return {
            "status": "skipped",
            "message": f"未配置 base_url，无法发起真实请求（目标: {raw_url}）",
            "suggestion": "在 Agent 任务或运行环境配置 base_url / 完整 URL 后重试",
        }
    try:
        validate_outbound_url(target)
    except ValueError as exc:
        return {"status": "failed", "error": f"API 地址被安全策略拦截: {exc}"}
    started = time.time()
    try:
        response = requests.request(
            method,
            target,
            headers=headers or {},
            json=body if body not in (None, "") else None,
            timeout=timeout,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        preview = _truncate(response.text, 2000)
        result = {
            "status": "success" if response.status_code < 400 else "failed",
            "method": method,
            "url": target,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "response": preview,
        }
        try:
            result["response_json"] = response.json()
        except ValueError:
            pass
        return result
    except requests.RequestException as exc:
        return {
            "status": "failed",
            "method": method,
            "url": target,
            "error": f"请求失败: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }


def run_ui_test(
    *,
    project_id: str = "default",
    case_files: Optional[list[str]] = None,
    env: str = "test",
    base_url: str = "",
    triggered_by: str = "ai-agent",
    runtime_override: Optional[list[dict]] = None,
    runtime_variables: Optional[dict] = None,
    data_bindings: Optional[list[dict]] = None,
    test_data_count: Optional[int] = None,
) -> dict:
    """复用现有 UI 自动化执行器运行用例。"""
    from backend import executor

    if not project_id:
        raise ValueError("UI 测试需要 project_id")
    bindings = [item for item in (data_bindings or []) if isinstance(item, dict)]
    binding_by_case = {
        str(item.get("case_file") or ""): item
        for item in bindings
        if str(item.get("case_file") or "")
    }
    selected_case_files = [str(item) for item in (case_files or [])]
    execution_specs = [
        binding_by_case.get(case_file) or {
            "case_file": case_file,
            "runtime_override": runtime_override or [],
            "runtime_variables": runtime_variables or {},
        }
        for case_file in selected_case_files
    ]
    execution_specs.extend(
        binding for binding in bindings
        if str(binding.get("case_file") or "") not in set(selected_case_files)
    )
    if not execution_specs:
        execution_specs = [{
            "case_file": "",
            "runtime_override": runtime_override or [],
            "runtime_variables": runtime_variables or {},
        }]

    task_ids = []
    dispatch_modes = []
    for spec in execution_specs:
        case_file = str(spec.get("case_file") or "")
        selected_files = [case_file] if case_file else list(case_files or [])
        spec_runtime_variables = dict(spec.get("runtime_variables") or runtime_variables or {})
        if test_data_count is not None:
            spec_runtime_variables[executor.PARAMETER_ROW_LIMIT_VARIABLE] = max(
                1,
                min(int(test_data_count), executor.MAX_DATA_ASSET_PARAMETER_ROWS),
            )
        task_id = executor.create_task(
            project_id=project_id,
            module="ui",
            case_files=selected_files,
            triggered_by=triggered_by,
            env=env or "test",
            base_url=base_url or "",
            runtime_override=spec.get("runtime_override") or runtime_override or [],
            runtime_case_file=(
                os.path.relpath(_ui_case_path(project_id, case_file), settings.ROOT)
                if case_file else ""
            ),
            runtime_variables=spec_runtime_variables,
        )
        dispatch = executor.dispatch(task_id)
        task_ids.append(task_id)
        dispatch_modes.append("sync" if dispatch else "redis")
    return {
        "status": "dispatched",
        "task_id": task_ids[0],
        "task_ids": task_ids,
        "project_id": project_id,
        "queue": dispatch_modes[0] if len(set(dispatch_modes)) == 1 else "mixed",
        "data_bindings": bindings,
        "note": "任务已进入现有 UI 执行队列，可通过 /api/tasks/{task_id} 查询结果",
    }


def run_app_test(
    *,
    project_id: str = "",
    case_ids: Optional[list[str]] = None,
    device_id: str = "",
) -> dict:
    """APP 自动化：由 testhub 平台承载，当前节点未配置 APP runner 时结构化跳过。"""
    runner_url = os.environ.get("APP_AUTOMATION_API_URL", "").strip()
    if not runner_url:
        return {
            "status": "skipped",
            "message": "APP 自动化由 testhub 平台承载，本节点未配置 APP_AUTOMATION_API_URL，已跳过",
            "suggestion": "在 testhub 平台执行 APP 用例，或配置 APP_AUTOMATION_API_URL 后由 Agent 自动调用",
        }
    try:
        response = requests.post(
            f"{runner_url.rstrip('/')}/api/app-automation/agents/execute/",
            json={"project_id": project_id, "case_ids": case_ids or [], "device_id": device_id},
            timeout=30,
        )
        return {
            "status": "dispatched",
            "status_code": response.status_code,
            "response": _truncate(response.text, 2000),
        }
    except requests.RequestException as exc:
        return {"status": "failed", "error": f"APP 执行器调用失败: {exc}"}


def run_security_test(**kwargs: Any) -> dict:
    """Run the Yakit-compatible security agent from the legacy workflow registry."""
    from backend.security_agent import scan
    return scan(**kwargs)


def create_report(
    *,
    agent_task_id: str = "",
    title: str = "AI Agent 测试报告",
    results: Optional[list[dict]] = None,
) -> dict:
    """生成测试报告（Markdown + JSON），写入 runtime/agent-reports。"""
    results = results or []
    passed = sum(1 for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") in {"skipped", "pending"})
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(passed / len(results) * 100, 1) if results else 0.0,
    }
    report_dir = os.path.join(settings.RUNTIME_DIR, "agent-reports")
    os.makedirs(report_dir, exist_ok=True)
    filename = f"{agent_task_id or 'task'}-{int(time.time())}"
    md_path = os.path.join(report_dir, f"{filename}.md")
    json_path = os.path.join(report_dir, f"{filename}.json")
    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{_now()}",
        f"- 任务：{agent_task_id or '-'}",
        f"- 结果：{summary}",
        "",
        "## 执行明细",
        "",
    ]
    for index, result in enumerate(results or [], 1):
        status = result.get("status", "unknown")
        icon = {"success": "✅", "failed": "❌", "skipped": "⏭️"}.get(status, "➖")
        lines.append(f"{index}. {icon} **{result.get('tool', '')}** {result.get('target', '')}")
        if result.get("summary"):
            lines.append(f"   - {result.get('summary')}")
        if result.get("error"):
            lines.append(f"   - 错误：{result.get('error')}")
        if result.get("suggestion"):
            lines.append(f"   - 建议：{result.get('suggestion')}")
        for detail in result.get("failure_details") or []:
            lines.append(
                f"   - 失败步骤：{detail.get('case', '')} / "
                f"{detail.get('step_index', '-')}. {detail.get('step', '')}"
            )
            if detail.get("error"):
                lines.append(f"     - {detail.get('error')}")
            if detail.get("screenshot"):
                lines.append(f"     - 截图：{detail.get('screenshot')}")
        execution_steps = result.get("execution_steps") or []
        if execution_steps:
            failed_steps = [item for item in execution_steps if item.get("status") == "failed"]
            lines.append(f"   - 执行步骤数：{len(execution_steps)}")
            for detail in failed_steps[:10]:
                lines.append(
                    f"     - ❌ {detail.get('index', '-')}. {detail.get('name') or detail.get('action')}"
                )
                if detail.get("error"):
                    lines.append(f"       - {detail.get('error')}")
                if detail.get("screenshot"):
                    lines.append(f"       - 截图：{detail.get('screenshot')}")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"task_id": agent_task_id, "title": title, "summary": summary, "results": results},
                  f, ensure_ascii=False, indent=2, default=str)
    return {
        "status": "success",
        "report_file": md_path,
        "report_json": json_path,
        "summary": summary,
        "report_url": f"/api/agent/v2/reports/{os.path.basename(json_path)}",
    }


# ============ 注册表 ============

def _handler(tool_name: str, handler: Callable) -> Callable:
    def wrapped(**kwargs: Any) -> dict:
        return handler(**kwargs)
    wrapped.__name__ = f"tool_{tool_name}"
    return wrapped


TOOLS: dict[str, dict] = {
    "generate_data": {
        "name": "generate_data",
        "description": "测试数据生成：按类型与字段生成用户/订单/商品等测试数据",
        "parameters": ["asset_type", "count", "fields", "project_id"],
        "handler": _handler("generate_data", generate_test_data),
    },
    "generate_test_data": {
        "name": "generate_test_data",
        "description": "测试数据生成：按类型与字段生成测试数据并同步到测试数据中心",
        "parameters": ["asset_type", "count", "fields", "project_id"],
        "handler": _handler("generate_test_data", generate_test_data),
    },
    "create_case": {
        "name": "create_case",
        "description": "AI 生成测试用例：输入业务需求，输出功能/异常/边界/安全/性能用例",
        "parameters": ["requirement", "dimension_plan"],
        "handler": _handler("create_case", create_case),
    },
    "generate_test_case": {
        "name": "generate_test_case",
        "description": "AI 生成测试用例：输入业务需求，输出功能/异常/边界/安全/性能用例",
        "parameters": ["requirement", "dimension_plan"],
        "handler": _handler("generate_test_case", create_case),
    },
    "run_api_test": {
        "name": "run_api_test",
        "description": "执行 API 测试：method/url/base_url/headers/body",
        "parameters": ["method", "url", "base_url", "headers", "body", "timeout"],
        "handler": _handler("run_api_test", run_api_test),
    },
    "run_ui_test": {
        "name": "run_ui_test",
        "description": "执行 UI 自动化：复用现有项目用例与分布式执行器",
        "parameters": ["project_id", "case_files", "env", "base_url"],
        "handler": _handler("run_ui_test", run_ui_test),
    },
    "run_web_test": {
        "name": "run_web_test",
        "description": "执行 UI 自动化：复用现有项目用例与分布式执行器",
        "parameters": ["project_id", "case_files", "env", "base_url"],
        "handler": _handler("run_web_test", run_ui_test),
    },
    "run_app_test": {
        "name": "run_app_test",
        "description": "执行 APP 自动化：由 testhub 平台承载",
        "parameters": ["project_id", "case_ids", "device_id"],
        "handler": _handler("run_app_test", run_app_test),
    },
    "run_security_test": {
        "name": "run_security_test",
        "description": "安全测试：接口参数识别、SQL注入/XSS/越权/JWT/SSRF 扫描",
        "parameters": ["api_doc", "target", "parameters", "headers", "project_id"],
        "handler": _handler("run_security_test", run_security_test),
    },
    "create_report": {
        "name": "create_report",
        "description": "生成测试报告：汇总执行结果并输出 Markdown/JSON 报告",
        "parameters": ["agent_task_id", "title", "results"],
        "handler": _handler("create_report", create_report),
    },
}


def list_tools() -> list[dict]:
    """返回工具注册表（不含 handler，供 API/前端展示）。"""
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "parameters": list(item["parameters"]),
        }
        for item in TOOLS.values()
    ]


def _record_execution_result(
    *,
    agent_task_id: str,
    step_id: int,
    tool: str,
    target: str,
    result: dict,
    logs: str = "",
) -> None:
    status = result.get("status", "failed")
    db.execute(
        "INSERT INTO test_execution_result("
        "agent_task_id, step_id, tool, target, status, summary, logs, screenshots, api_response, failure_analysis, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            agent_task_id,
            step_id,
            tool,
            target,
            status,
            _truncate(result.get("summary") or result.get("message") or result.get("error") or result.get("status") or "", 1000),
            _truncate(logs, 4000),
            json.dumps(result.get("screenshots") or [], ensure_ascii=False),
            _truncate(result.get("response") or result.get("response_json") or "", 4000),
            json.dumps(result.get("failure_analysis") or {}, ensure_ascii=False),
            _now(),
        ),
    )


def record_tool_result(
    *,
    agent_task_id: str,
    step_id: int = 0,
    tool: str,
    target: str,
    result: dict,
    logs: str = "",
) -> None:
    """公开写入 test_execution_result（供 ReAct 核心与新增工具复用）。"""
    _record_execution_result(
        agent_task_id=agent_task_id,
        step_id=step_id,
        tool=tool,
        target=target,
        result=result,
        logs=logs,
    )


def run_tool(
    name: str,
    *,
    agent_task_id: str = "",
    step_id: int = 0,
    logs: str = "",
    **kwargs: Any,
) -> dict:
    """调用工具：参数校验 -> 执行 -> 记录 test_execution_result -> 返回结果。"""
    tool = TOOLS.get(name)
    if not tool:
        result = {"status": "failed", "error": f"未知工具: {name}，可用: {', '.join(TOOLS)}"}
    else:
        target = str(kwargs.get("target") or kwargs.get("url") or kwargs.get("requirement") or kwargs.get("task_id") or name)
        try:
            result = tool["handler"](**kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具必须结构化返回错误
            result = {"status": "failed", "error": f"{name} 执行异常: {exc}"}
    if agent_task_id:
        try:
            _record_execution_result(
                agent_task_id=agent_task_id,
                step_id=step_id,
                tool=name,
                target=str(kwargs.get("target") or kwargs.get("url") or kwargs.get("requirement") or kwargs.get("task_id") or name),
                result=result,
                logs=logs,
            )
            remember(agent_task_id, f"last_tool_result:{name}", result)
        except Exception:  # noqa: BLE001 - 记录失败不影响工具结果
            pass
    return result
