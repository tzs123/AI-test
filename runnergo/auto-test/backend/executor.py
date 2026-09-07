"""测试执行器：隔离结果目录、流式日志、结果解析、报告生成、飞书通知。

负载均衡下多 worker 并发执行时，每个任务使用独立 results/report 目录，互不干扰。
"""
import os
import sys
import copy
import csv
import json
import glob
import time
import uuid
import shutil
import subprocess
import importlib.util
import re
import sqlite3
import threading
from typing import Optional

import requests
import yaml
import signal as _signal

# 支持直接执行（python -m backend.executor）与包内导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db, log_store, step_store, settings, feishu
from backend.time_utils import utc_now_text
from backend.safe_paths import safe_child_path, safe_identifier
from core.runtime_case import RuntimeCase

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DATA_ASSET_EXPR_RE = re.compile(r"\$\{\s*dataAssets\.([A-Za-z_][A-Za-z0-9_]*)\.")
DATA_ASSET_FIELD_EXPR_RE = re.compile(
    r"\$\{\s*dataAssets\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\d+)*)"
)
MAX_DATA_ASSET_PARAMETER_ROWS = 100
PARAMETER_ROW_LIMIT_VARIABLE = "__RUNNERGO_PARAMETER_ROW_LIMIT"
_MISSING = object()
TEST_OBJECT_SECRET_DIR = os.path.join(settings.RUNTIME_DIR, "test-object-secrets")


DEFAULT_UI_TARGETS = [
    "tests/ui/test_jdy_kb_home.py",
    "tests/ui/test_jdy_kb_apply.py",
    "tests/ui/test_jdy_kb_fill.py",
    "tests/ui/test_jdy_kb_upload.py",
    "tests/ui/test_jdy_kb_submit.py",
    "tests/ui/test_jdy_kb_submit_return.py",
    "tests/ui/test_new.py",
]


EXCLUDED_DEFAULT_TARGETS = {
    # 旧版大集合流程已拆分到 test_jdy_kb_*，默认全量不再重复执行。
    "tests/ui/test_jdy_flow.py",
}


def _test_object_secret_path(task_id: str) -> str:
    task_key = safe_identifier(task_id, label="任务 ID")
    return os.path.join(TEST_OBJECT_SECRET_DIR, f"{task_key}.json")


def store_test_object_bundle(task_id: str, bundle: dict) -> str:
    """Store execution-only object details outside task rows and source YAML."""
    if not bundle:
        return ""
    os.makedirs(TEST_OBJECT_SECRET_DIR, mode=0o700, exist_ok=True)
    expiry = time.time() - 86400
    for filename in os.listdir(TEST_OBJECT_SECRET_DIR):
        candidate = os.path.join(TEST_OBJECT_SECRET_DIR, filename)
        try:
            if os.path.isfile(candidate) and os.path.getmtime(candidate) < expiry:
                os.remove(candidate)
        except OSError:
            pass
    path = _test_object_secret_path(task_id)
    temp_path = path + ".tmp"
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(bundle, handle, ensure_ascii=False)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return path


def _consume_test_object_bundle(task_id: str) -> dict:
    path = _test_object_secret_path(task_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def discard_test_object_bundle(task_id: str) -> None:
    try:
        os.remove(_test_object_secret_path(task_id))
    except OSError:
        pass


def _json_or(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dict_get(source: dict, key: str, default=None):
    if key in source:
        return source[key]
    lower = str(key).lower()
    for item_key, item_value in source.items():
        if str(item_key).lower() == lower:
            return item_value
    return default


def _get_path(source, path: str, default=None):
    current = source
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = _dict_get(current, part, default)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return default
        else:
            return default
        if current is default:
            return default
    return current


def _deep_merge_dict(target: dict, source: dict) -> dict:
    for key, value in source.items():
        existing_key = next((item_key for item_key in target if str(item_key).lower() == str(key).lower()), key)
        if isinstance(target.get(existing_key), dict) and isinstance(value, dict):
            _deep_merge_dict(target[existing_key], value)
        else:
            target[existing_key] = value
    return target


def _dict_has_key_case_insensitive(source: dict, key: str) -> bool:
    return any(str(item_key).lower() == str(key).lower() for item_key in source)


def _merge_direct_data_fields(target: dict, payload) -> None:
    """Expose row object fields as `${data.field}` without overwriting aliases."""
    if not isinstance(target, dict) or not isinstance(payload, dict):
        return
    for key, value in payload.items():
        if _dict_has_key_case_insensitive(target, str(key)):
            continue
        target[key] = copy.deepcopy(value)


def _case_file_path(project_id: str, module: str, case_file: str) -> str:
    value = str(case_file or "").replace("\\", "/").strip()
    if not value:
        return ""
    case_dir = _project_case_dir(project_id).replace("\\", "/").strip("/")
    if "/" not in value:
        value = f"{case_dir}/{module}/{value}"
    try:
        return safe_child_path(
            settings.ROOT,
            value,
            allowed_suffixes=(".yaml", ".yml"),
        )
    except ValueError:
        return ""


def _extract_data_asset_references(project_id: str, module: str, case_files: list, runtime_variables: dict) -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}

    def add_from_text(text: str):
        for alias, path in DATA_ASSET_FIELD_EXPR_RE.findall(text or ""):
            references.setdefault(alias, set()).add(path)
        for alias in DATA_ASSET_EXPR_RE.findall(text or ""):
            references.setdefault(alias, set())

    add_from_text(json.dumps(runtime_variables or {}, ensure_ascii=False))
    for case_file in case_files or []:
        path = _case_file_path(project_id, module, case_file)
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                add_from_text(handle.read())
        except OSError:
            continue
    return references


def _extract_data_asset_aliases(project_id: str, module: str, case_files: list, runtime_variables: dict) -> list[str]:
    return list(_extract_data_asset_references(project_id, module, case_files, runtime_variables))


def _asset_matches(payload: dict, filters: dict) -> bool:
    if not isinstance(filters, dict):
        return True
    for key, expected in filters.items():
        if str(_get_path(payload, key)) != str(expected):
            return False
    return True


def _asset_payload_rows(payload, filters: dict) -> tuple[list[dict], bool]:
    """Return matching object rows and whether the asset is parameterized."""
    if isinstance(payload, dict):
        return ([payload] if _asset_matches(payload, filters) else []), False
    if not isinstance(payload, list):
        return [], False

    rows = [
        item for item in payload
        if isinstance(item, dict) and _asset_matches(item, filters)
    ]
    return rows[:MAX_DATA_ASSET_PARAMETER_ROWS], True


def _load_bulk_asset_rows(db_path: str, payload: dict) -> list[dict]:
    """读取 Django 大批量资产的文件前若干行，供 UI/API 参数化执行使用。"""
    if not isinstance(payload, dict) or payload.get("_runnergo_source") != "bulk_test_data":
        return []
    try:
        job_id = int(payload.get("bulk_job_id"))
    except (TypeError, ValueError):
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT status, output_format, output_file FROM bulk_test_data_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except (UnboundLocalError, AttributeError):
            pass

    if not row or row[0] != "COMPLETED" or row[1] not in {"csv", "jsonl"} or not row[2]:
        return []

    media_root = os.path.realpath(os.environ.get(
        "TESTHUB_MEDIA_ROOT",
        os.path.abspath(os.path.join(settings.ROOT, os.pardir, "testhub", "media")),
    ))
    output_path = os.path.realpath(os.path.join(media_root, str(row[2])))
    if os.path.commonpath([media_root, output_path]) != media_root or not os.path.isfile(output_path):
        return []

    rows = []
    try:
        with open(output_path, "r", encoding="utf-8", newline="") as handle:
            if row[1] == "csv":
                for item in csv.DictReader(handle):
                    rows.append(dict(item))
                    if len(rows) >= MAX_DATA_ASSET_PARAMETER_ROWS:
                        break
            else:
                for line in handle:
                    if line.strip():
                        rows.append(json.loads(line))
                    if len(rows) >= MAX_DATA_ASSET_PARAMETER_ROWS:
                        break
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in rows if isinstance(item, dict)]


def _payload_has_referenced_field(payload: dict, referenced_paths: set[str]) -> bool:
    if not referenced_paths:
        return True
    return any(_get_path(payload, path, _MISSING) is not _MISSING for path in referenced_paths)


def _prune_payload_to_referenced_fields(payload: dict, referenced_paths: set[str]) -> dict:
    if not referenced_paths or not isinstance(payload, dict):
        return payload
    result = {}
    for path in referenced_paths:
        top_level_key = str(path).split(".", 1)[0]
        for key, value in payload.items():
            if str(key).lower() == top_level_key.lower():
                result[key] = copy.deepcopy(value)
                break
    return result


def _merge_parameter_groups(base_payload: dict, groups: list[list[dict]]) -> list[dict]:
    if not groups:
        return []
    row_count = min(
        MAX_DATA_ASSET_PARAMETER_ROWS,
        max(len(group) for group in groups),
    )
    rows = []
    for index in range(row_count):
        row = copy.deepcopy(base_payload)
        for group in groups:
            if not group:
                continue
            selected = group[index] if index < len(group) else group[-1]
            _deep_merge_dict(row, copy.deepcopy(selected))
        rows.append(row)
    return rows


def _asset_has_tags(asset_tags: list, required_tags: list) -> bool:
    if not required_tags:
        return True
    tags = {str(item) for item in asset_tags or []}
    return {str(item) for item in required_tags}.issubset(tags)


def _testhub_data_asset_db_path() -> str:
    return os.environ.get(
        "TEST_DATA_ASSETS_DB_PATH",
        os.path.abspath(os.path.join(settings.ROOT, os.pardir, "testhub", "db.sqlite3")),
    )


def _fetch_test_data_context_api(aliases: list) -> Optional[dict]:
    """从 TestHub API 拉取数据资产上下文；不可用时返回 None 以回退文件读取。

    对应 testhub /api/core/test-data-assets/agent-context/，返回的
    requirements/assets 为 dict 列表，形状与共享 SQLite 文件读取结果一致。
    """
    base_url = os.environ.get("TEST_DATA_CENTER_API_URL", "").strip()
    if not base_url or not aliases:
        return None
    token = os.environ.get("TEST_DATA_CENTER_AGENT_TOKEN", "runnergo-local-agent-token")
    try:
        response = requests.get(
            base_url.rstrip("/") + "/api/core/test-data-assets/agent-context/",
            params={"aliases": ",".join(str(alias) for alias in aliases)},
            headers={"X-Agent-Token": token},
            timeout=10,
        )
        if response.status_code >= 400:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    requirements = [item for item in payload.get("requirements") or [] if isinstance(item, dict)]
    assets = [item for item in payload.get("assets") or [] if isinstance(item, dict)]
    return {"requirements": requirements, "assets": assets}


def _load_test_data_asset_context(
    project_id: str,
    module: str,
    case_files: list,
    runtime_variables: dict,
    parameter_row_limit=None,
) -> dict:
    asset_references = _extract_data_asset_references(project_id, module, case_files, runtime_variables)
    aliases = list(asset_references)
    if not aliases:
        return {}

    db_path = _testhub_data_asset_db_path()
    context = {"dataAssets": {}, "data": {}}
    parameterized_aliases = {}

    api_context = _fetch_test_data_context_api(aliases)
    if api_context is not None:
        requirements = api_context["requirements"]
        assets = api_context["assets"]
    else:
        if not os.path.isfile(db_path):
            return {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return {}

        try:
            placeholders = ",".join("?" for _ in aliases)
            requirements = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM test_data_asset_requirements
                    WHERE is_active=1 AND alias IN ({placeholders})
                    ORDER BY id
                    """,
                    aliases,
                ).fetchall()
            ]
            assets = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, asset_type, status, payload, tags
                    FROM test_data_assets
                    WHERE status='AVAILABLE'
                    ORDER BY id
                    """
                ).fetchall()
            ]
        except sqlite3.Error:
            conn.close()
            return {}
        finally:
            conn.close()

    by_type = {}
    for asset in assets:
        by_type.setdefault(asset["asset_type"], []).append(asset)

    for alias in aliases:
        alias_payload = {}
        parameter_groups = []
        matched_any = False
        referenced_paths = asset_references.get(alias) or set()
        alias_requirements = [item for item in requirements if str(item["alias"]) == alias]
        for requirement in alias_requirements:
            filters = _json_or(requirement["filters"], {})
            required_tags = _json_or(requirement["tags"], [])
            quick_prefix = f"quick-bind-{requirement['target_type']}-{requirement['target_case_id']}-{alias}-"
            for asset in by_type.get(requirement["asset_type"], []):
                if "source_asset_id" in requirement.keys() and requirement["source_asset_id"]:
                    if int(asset["id"]) != int(requirement["source_asset_id"]):
                        continue
                payload = _json_or(asset["payload"], {})
                asset_tags = _json_or(asset["tags"], [])
                has_requirement_tags = _asset_has_tags(asset_tags, required_tags)
                has_quick_bind_tag = any(str(tag).startswith(quick_prefix) for tag in asset_tags)
                if not (has_requirement_tags or has_quick_bind_tag):
                    continue
                bulk_rows = asset.get("bulk_rows")
                if bulk_rows is None:
                    bulk_rows = _load_bulk_asset_rows(db_path, payload)
                bulk_rows = [item for item in (bulk_rows or []) if isinstance(item, dict)]
                rows, parameterized = _asset_payload_rows(
                    bulk_rows if bulk_rows else payload,
                    filters,
                )
                rows = [
                    _prune_payload_to_referenced_fields(row, referenced_paths)
                    for row in rows
                    if _payload_has_referenced_field(row, referenced_paths)
                ]
                if not rows:
                    continue
                if parameterized:
                    parameter_groups.append(rows)
                else:
                    _deep_merge_dict(alias_payload, rows[0])
                matched_any = True
        if matched_any:
            parameter_rows = _merge_parameter_groups(alias_payload, parameter_groups)
            selected_payload = parameter_rows[0] if parameter_rows else alias_payload
            context["dataAssets"][alias] = selected_payload
            context["data"][alias] = selected_payload
            _merge_direct_data_fields(context["data"], selected_payload)
            if parameter_rows:
                parameterized_aliases[alias] = parameter_rows

    if parameterized_aliases:
        row_count = min(
            MAX_DATA_ASSET_PARAMETER_ROWS,
            max(len(rows) for rows in parameterized_aliases.values()),
        )
        if parameter_row_limit is not None:
            row_count = min(row_count, max(1, int(parameter_row_limit)))
        parameter_contexts = []
        for index in range(row_count):
            row_context = {
                "dataAssets": copy.deepcopy(context["dataAssets"]),
                "data": {},
            }
            for alias, rows in parameterized_aliases.items():
                selected = rows[index] if index < len(rows) else rows[-1]
                row_context["dataAssets"][alias] = copy.deepcopy(selected)
            for alias, payload in row_context["dataAssets"].items():
                row_context["data"][alias] = copy.deepcopy(payload)
                _merge_direct_data_fields(row_context["data"], payload)
            parameter_contexts.append(row_context)
        context["_parameterRows"] = parameter_contexts

    return context if context["dataAssets"] else {}


def _project_case_dir(project_id: str) -> str:
    if project_id == "default":
        return "cases"
    try:
        from backend.projects import service as project_service
        proj = project_service.get_project(project_id)
        return (proj or {}).get("case_dir") or f"cases/{project_id}"
    except Exception:
        return f"cases/{project_id}"


def _default_targets(module: str = "ui", project_id: str = "default") -> list:
    """默认执行目标：内置白名单 + 当前项目新增 YAML 对应脚本。

    这样既能避开旧的占位/重复脚本，又能保证平台新增 case 后，
    不选具体用例直接点"执行全部"时也会执行到最新新增脚本。
    """
    if module != "ui":
        raise ValueError("UI 自动化执行器仅支持 ui 模块")
    if project_id != "default":
        return [f"tests/{project_id}/ui/"]

    targets = [
        target for target in DEFAULT_UI_TARGETS
        if os.path.isfile(os.path.join(settings.ROOT, target))
    ]

    case_dir = _project_case_dir(project_id)
    pattern = os.path.join(settings.ROOT, case_dir, "ui", "*.y*ml")
    for case_path in sorted(glob.glob(pattern)):
        rel_case = os.path.relpath(case_path, settings.ROOT)
        py = _yaml_to_py(rel_case, project_id=project_id, module="ui")
        if py and py not in EXCLUDED_DEFAULT_TARGETS and py not in targets:
            targets.append(py)
    return targets


def _now():
    return utc_now_text()


def _task_dirs(task_id: str) -> tuple:
    task_id = safe_identifier(task_id, label="task_id")
    tasks_root = os.path.join(settings.REPORT_DIR, "tasks")
    base = safe_child_path(tasks_root, task_id, filename_only=True)
    return base, os.path.join(base, "results"), os.path.join(base, "report")


def _clear_runtime_caches():
    """清理 pytest / Python 运行缓存，避免平台任务读取旧代码或旧 case。"""
    shutil.rmtree(os.path.join(settings.ROOT, ".pytest_cache"), ignore_errors=True)
    skip_dirs = {".git", ".venv", "venv", "node_modules", "reports"}
    for root, dirs, _files in os.walk(settings.ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            dirs.remove("__pycache__")


def _remove_queued_task(task_id: str) -> int:
    try:
        from backend import redis_queue
        return redis_queue.remove_task(task_id)
    except Exception:
        return 0


def _terminate_task_runner(task_id: str, proc=None) -> tuple[bool, str]:
    target_pgid = 0
    target_pid = 0
    if proc:
        try:
            target_pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            target_pgid = 0
        target_pid = int(getattr(proc, "pid", 0) or 0)
    if not target_pgid:
        task = _get_task(task_id)
        target_pgid = int(task.get("runner_pgid") or 0)
        target_pid = int(task.get("runner_pid") or 0)
    if not target_pgid:
        if target_pid:
            try:
                os.kill(target_pid, _signal.SIGTERM)
                log_store.write(task_id, "⛔ 用户手动停止任务，已发送 SIGTERM 到运行进程\n")
                if proc:
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.kill(target_pid, _signal.SIGKILL)
                        log_store.write(task_id, "⛔ 任务进程未及时退出，已发送 SIGKILL\n")
                return True, "已停止"
            except ProcessLookupError:
                return True, "任务已标记为停止"
            except PermissionError as e:
                log_store.write(task_id, f"⛔ 没有权限停止任务进程: {e}\n")
                return False, "没有权限停止任务进程"
            except Exception as e:
                log_store.write(task_id, f"⛔ 停止任务进程异常: {e}\n")
                return False, str(e)
        log_store.write(task_id, "⛔ 用户手动停止任务，未发现运行进程组\n")
        return True, "任务已标记为停止"

    try:
        os.killpg(target_pgid, _signal.SIGTERM)
        log_store.write(task_id, "⛔ 用户手动停止任务，已发送 SIGTERM\n")
        if proc:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(target_pgid, _signal.SIGKILL)
                log_store.write(task_id, "⛔ 任务未及时退出，已发送 SIGKILL\n")
        else:
            time.sleep(1)
            try:
                os.killpg(target_pgid, 0)
                os.killpg(target_pgid, _signal.SIGKILL)
                log_store.write(task_id, "⛔ 远端任务未及时退出，已发送 SIGKILL\n")
            except ProcessLookupError:
                pass
    except ProcessLookupError:
        pass
    except PermissionError as e:
        log_store.write(task_id, f"⛔ 没有权限停止任务进程: {e}\n")
        return False, "没有权限停止任务进程"
    except Exception as e:
        log_store.write(task_id, f"⛔ 停止任务异常: {e}\n")
        return False, str(e)
    return True, "已停止"


def _start_task_stop_monitor(task_id: str, proc, interval: float = 0.5) -> threading.Event:
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(interval):
            if proc.poll() is not None:
                return
            try:
                status = _get_task(task_id).get("status")
            except Exception:
                continue
            if status == "stopped":
                _terminate_task_runner(task_id, proc)
                return

    threading.Thread(target=monitor, name=f"task-stop-monitor-{task_id}", daemon=True).start()
    return stopped


def stop_task(task_id: str) -> dict:
    """停止正在运行的任务，终止子进程组。"""
    proc = _running_procs.get(task_id)

    _update_task(task_id, status="stopped", finished_at=_now())
    step_store.finalize(task_id, "stopped", error="用户手动停止任务")
    removed = _remove_queued_task(task_id)
    discard_test_object_bundle(task_id)
    if removed:
        log_store.write(task_id, f"⛔ 用户手动停止任务，已从队列移除 {removed} 条待执行记录\n")
    ok, msg = _terminate_task_runner(task_id, proc)
    return {"ok": ok, "msg": msg, "removed_queued": removed}


def pause_task(task_id: str) -> dict:
    """暂停正在运行或排队中的任务。"""
    task = _get_task(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task.get("status") not in {"running", "pending"}:
        return {"ok": False, "msg": f"任务状态为'{task.get('status')}'，无法暂停"}
    proc = _running_procs.get(task_id)
    target_pgid = 0
    if proc:
        try:
            target_pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            target_pgid = 0
    if not target_pgid:
        target_pgid = int(task.get("runner_pgid") or 0)
    if target_pgid:
        try:
            os.killpg(target_pgid, _signal.SIGSTOP)
            log_store.write(task_id, "⏸ 用户暂停任务，已发送 SIGSTOP\n")
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
    else:
        log_store.write(task_id, "⏸ 用户暂停队列任务，将暂缓执行\n")
    _update_task(task_id, status="paused")
    return {"ok": True, "msg": "已暂停"}


def resume_task(task_id: str) -> dict:
    """恢复暂停中的任务。"""
    task = _get_task(task_id)
    if not task:
        return {"ok": False, "msg": "任务不存在"}
    if task.get("status") != "paused":
        return {"ok": False, "msg": f"任务状态为'{task.get('status')}'，无法恢复"}
    target_pgid = int(task.get("runner_pgid") or 0)
    if target_pgid:
        try:
            os.killpg(target_pgid, _signal.SIGCONT)
            log_store.write(task_id, "▶ 用户恢复任务，已发送 SIGCONT\n")
            _update_task(task_id, status="running")
            return {"ok": True, "msg": "已恢复"}
        except ProcessLookupError:
            target_pgid = 0
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
    _update_task(task_id, status="pending")
    log_store.write(task_id, "▶ 用户恢复队列任务，已重新进入等待执行\n")
    return {"ok": True, "msg": "已恢复为等待执行"}


def _yaml_to_py(case_file: str, project_id: str = "default", module: str = "") -> str:
    """将 case_file 映射为 pytest 脚本路径。
    默认项目：cases/ui/test_x.yaml -> tests/ui/test_x.py
    新项目：  cases/{pid}/ui/test_x.yaml -> tests/{pid}/ui/test_x.py
    找不到则返回空字符串。
    """
    if not case_file or module != "ui":
        return ""
    value = str(case_file).replace("\\", "/").strip()
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        return ""

    case_dir = _project_case_dir(project_id).replace("\\", "/").strip("/")
    accepted_prefixes = {f"{case_dir}/ui/"}
    if project_id == "default":
        accepted_prefixes.add("cases/ui/")

    filename = value
    if "/" in value:
        prefix = next((item for item in accepted_prefixes if value.startswith(item)), "")
        if not prefix:
            return ""
        filename = value[len(prefix):]
    if "/" in filename or not filename.endswith((".yaml", ".yml")):
        return ""

    stem = filename.rsplit(".", 1)[0] + ".py"
    base = "tests" if project_id == "default" else f"tests/{project_id}"
    rel = f"{base}/ui/{stem}"
    root = os.path.realpath(os.path.join(settings.ROOT, base, "ui"))
    candidate = os.path.realpath(os.path.join(settings.ROOT, rel))
    try:
        if os.path.commonpath([root, candidate]) != root:
            return ""
    except ValueError:
        return ""
    if os.path.isfile(candidate) and not os.path.islink(candidate):
        return rel
    return ""


def _py_to_yaml_case_file(py_target: str, project_id: str = "default", module: str = "") -> str:
    """Map an executable pytest target back to its YAML case for runtime data scanning."""
    if module != "ui":
        return ""
    value = str(py_target or "").replace("\\", "/").strip()
    if not value or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        return ""

    test_base = "tests/ui/" if project_id == "default" else f"tests/{project_id}/ui/"
    if not value.startswith(test_base):
        return ""
    filename = value[len(test_base):]
    if "/" in filename or not filename.endswith(".py"):
        return ""

    case_dir = _project_case_dir(project_id).replace("\\", "/").strip("/")
    stem = filename.rsplit(".", 1)[0]
    for suffix in (".yaml", ".yml"):
        rel = f"{case_dir}/ui/{stem}{suffix}"
        candidate = os.path.realpath(os.path.join(settings.ROOT, rel))
        root = os.path.realpath(os.path.join(settings.ROOT, case_dir, "ui"))
        try:
            if os.path.commonpath([root, candidate]) != root:
                continue
        except ValueError:
            continue
        if os.path.isfile(candidate) and not os.path.islink(candidate):
            return rel
    return ""


def _case_files_for_execution(project_id: str, module: str, case_files: list) -> list:
    """Return YAML cases that will be executed, including implicit "run all" targets."""
    if case_files:
        return case_files
    if module != "ui":
        return []

    targets = _default_targets(module, project_id=project_id)
    resolved = []
    for target in targets:
        value = str(target or "").replace("\\", "/").strip()
        if value.endswith("/"):
            case_dir = _project_case_dir(project_id).replace("\\", "/").strip("/")
            pattern = os.path.join(settings.ROOT, case_dir, "ui", "*.y*ml")
            for case_path in sorted(glob.glob(pattern)):
                rel_case = os.path.relpath(case_path, settings.ROOT).replace("\\", "/")
                py = _yaml_to_py(rel_case, project_id=project_id, module=module)
                if py and py not in EXCLUDED_DEFAULT_TARGETS and rel_case not in resolved:
                    resolved.append(rel_case)
            continue
        case_file = _py_to_yaml_case_file(value, project_id=project_id, module=module)
        if case_file and case_file not in resolved:
            resolved.append(case_file)
    return resolved


def _build_pytest_args(module: str, case_files: list, results_dir: str,
                       project_id: str = "default") -> list:
    if module != "ui":
        raise ValueError("UI 自动化执行器仅支持 ui 模块")
    args = [sys.executable, "-m", "pytest", "-v", "--tb=short",
            "-W", "ignore::Warning", f"--alluredir={results_dir}",
            "-p", "no:cacheprovider"]

    if importlib.util.find_spec("xdist"):
        # 并行策略：不同 YAML 文件并行执行，同一文件内用例串行
        # --dist=loadfile: 按测试文件分组分配到不同 worker
        # UI 测试限制 2 个并发，避免被测系统首页限流。
        args.extend(["-n", "2", "--dist=loadfile"])

    targets = []
    if case_files:
        for f in case_files:
            py = _yaml_to_py(f, project_id=project_id, module=module)
            if py and py not in targets:
                targets.append(py)
    if not targets:
        if case_files:
            # 指定了 YAML 但未找到对应 pytest 脚本，避免误跑全量 tests/
            targets = ["tests/__init__.py"]
        elif project_id == "default":
            targets = _default_targets(module, project_id=project_id)
        else:
            targets = _default_targets(module, project_id=project_id)
    # 过滤已删除/不存在的目标，避免 pytest 收集 0 项导致整批执行被判失败
    existing = [
        target for target in targets
        if os.path.exists(os.path.join(settings.ROOT, target))
    ]
    targets = existing or ["tests/__init__.py"]
    args.extend(targets)
    args.extend(["-m", "ui"])
    return args


def _parse_results(results_dir: str) -> dict:
    passed = failed = broken = skipped = total = 0
    for f in glob.glob(os.path.join(results_dir, "*-result.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                st = json.load(fp).get("status", "")
            total += 1
            if st == "passed":
                passed += 1
            elif st == "failed":
                failed += 1
            elif st == "broken":
                broken += 1
            elif st == "skipped":
                skipped += 1
        except Exception:
            pass
    return {"passed": passed, "failed": failed + broken, "skipped": skipped, "total": total}


def _prefer_step_store_stats(task_id: str, fallback: dict, *, enabled: bool = True) -> dict:
    """Use persisted UI step counts when available, otherwise keep pytest counts.

    A WebFlowRunner test is one pytest item but may contain dozens of executable
    steps.  The step store is therefore the authoritative source for a single
    UI case; pytest result files remain the fallback for fixture/setup failures
    and multi-case batches that do not yet have per-case step partitions.
    """
    fallback = dict(fallback or {})
    if not enabled:
        return fallback
    try:
        state = step_store.read(task_id) or {}
        counts = state.get("counts") or {}
        total = int(state.get("total") or 0)
        pending = int(counts.get("pending") or 0)
        running = int(counts.get("running") or 0)
        passed = int(counts.get("passed") or 0)
        failed = int(counts.get("failed") or 0)
        skipped = int(counts.get("skipped") or 0)
    except (AttributeError, TypeError, ValueError):
        return fallback

    if total <= 0 or pending or running:
        return fallback
    if passed + failed + skipped != total:
        return fallback
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
    }


def _auto_save_defects(results_dir: str, task: dict):
    """解析失败用例，保存为内部失败证据。

    判断逻辑：
    - status=failed/broken 的用例视为失败证据
    - 已存在相同 task_id+tc_id 的记录不重复保存
    - 自动推断严重等级：broken>failed，含"超时"降为中
    """
    import uuid as _uuid
    try:
        from backend import db as _db
    except Exception:
        return 0

    saved = 0
    now = _now()
    task_id = task.get("id", "")
    project_id = task.get("project_id", "default")

    # 读取任务完整日志（用于生成失败证据）
    task_log = ""
    try:
        task_log = log_store.read(task_id) or ""
    except Exception:
        pass

    for f in glob.glob(os.path.join(results_dir, "*-result.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                result = json.load(fp)
        except Exception:
            continue

        status = result.get("status", "")
        if status not in ("failed", "broken"):
            continue

        # 提取用例信息
        name = result.get("name", "unknown")
        full_name = result.get("fullName", "")  # 如 tests.ui.test_jdy_flow.test_smoke_full_flow[case0]
        labels = result.get("labels", [])
        tc_id = ""
        scenario = ""
        for label in labels:
            if label.get("name") == "tc_id":
                tc_id = label.get("value", "")
            elif label.get("name") == "scenario":
                scenario = label.get("value", "")

        # 从 fullName 解析测试文件路径和函数名
        # fullName 格式：tests.ui.test_jdy_flow.test_smoke_full_flow[case0]
        # 转换为：tests/ui/test_jdy_flow.py::test_smoke_full_flow[case0]
        test_file_path = ""
        test_func_name = name
        if full_name:
            parts = full_name.rsplit(".", 1)
            if len(parts) == 2:
                module_path = parts[0]  # tests.ui.test_jdy_flow
                test_func_name = parts[1]  # test_smoke_full_flow[case0]
                test_file_path = module_path.replace(".", "/") + ".py"

        # 从 parameters 中的 case 参数提取 scenario、tc_id 和 case_file_path（YAML 数据驱动场景）
        case_file_path = ""
        if not scenario or not tc_id:
            import ast as _ast
            for p in result.get("parameters", []):
                if p.get("name") == "case":
                    try:
                        case_dict = _ast.literal_eval(p.get("value", "{}"))
                        if isinstance(case_dict, dict):
                            if not scenario:
                                scenario = case_dict.get("scenario", "")
                            if not tc_id:
                                tc_id = case_dict.get("tc_id", "")
                            case_file_path = case_dict.get("case_file_path", "")
                    except Exception:
                        pass

        # 错误信息
        status_details = result.get("statusDetails", {})
        error_msg = status_details.get("message", "") or status_details.get("trace", "")
        # 截取前 2000 字符避免过长
        error_msg = error_msg[:2000] if error_msg else ""

        # 提取截图路径（allure attachments）
        screenshot_path = ""
        attachments = result.get("attachments", [])
        for att in attachments:
            if att.get("type", "").startswith("image/"):
                src = att.get("source", "")
                if src:
                    full_path = os.path.join(results_dir, src)
                    if os.path.exists(full_path):
                        screenshot_path = full_path
                        break
        # 兜底：从 screenshots/task_id/ 目录查找含用例名的截图
        if not screenshot_path:
            shots_dir = os.path.join(settings.SCREENSHOTS_DIR, task_id)
            if os.path.isdir(shots_dir):
                for shot in sorted(os.listdir(shots_dir), reverse=True):
                    if name in shot and shot.endswith(".png"):
                        screenshot_path = os.path.join(shots_dir, shot)
                        break

        # 提取该用例相关的日志片段（用例名到下一个用例名之间）
        case_log = ""
        if task_log and name:
            idx = task_log.find(name)
            if idx >= 0:
                # 往后取 2000 字符或到下一个测试用例
                segment = task_log[idx:idx + 3000]
                case_log = segment[:2000]
            else:
                case_log = error_msg or "(未找到用例日志)"
        else:
            case_log = error_msg or "(无日志)"

        # 推断严重等级
        severity = "中"
        if status == "broken":
            severity = "高"
        if error_msg and ("超时" in error_msg or "timeout" in error_msg.lower()):
            severity = "中"

        # 推断错误类型
        error_type = "断言失败" if status == "failed" else "执行异常"
        if error_msg:
            if "TimeoutError" in error_msg or "超时" in error_msg:
                error_type = "超时"
            elif "AssertionError" in error_msg or "assert" in error_msg.lower():
                error_type = "断言失败"
            elif "SyntaxError" in error_msg:
                error_type = "代码错误"
            elif "Operation not permitted" in error_msg:
                error_type = "环境错误"

        # 去重：相同 task_id+tc_id 或 task_id+name 不重复保存
        if tc_id:
            existing = _db.execute(
                "SELECT id FROM defects WHERE task_id=? AND tc_id=?",
                (task_id, tc_id), fetch=True
            )
            if existing:
                continue
        else:
            # 无 tc_id 时，按 task_id+scenario+title 去重
            existing = _db.execute(
                "SELECT id FROM defects WHERE task_id=? AND scenario=? AND title=?",
                (task_id, scenario, f"{scenario or name}失败"[:100]), fetch=True
            )
            if existing:
                continue

        # 构造失败标题（scenario + 失败）
        scenario_text = scenario or name
        title = f"{scenario_text}失败"[:100]

        # 构造执行路径：YAML文件路径 > [tc_id] scenario
        # 方便查找是执行了哪个 case 导致的失败
        exec_parts = []
        if case_file_path:
            exec_parts.append(case_file_path)
        elif test_file_path:
            exec_parts.append(f"{test_file_path}::{test_func_name}")
        else:
            exec_parts.append(test_func_name or name)
        if tc_id:
            exec_parts.append(f"[{tc_id}] {scenario_text}")
        elif scenario_text:
            exec_parts.append(scenario_text)
        execution_path = " > ".join(exec_parts)[:300]

        did = _uuid.uuid4().hex[:12]
        _db.execute(
            "INSERT INTO defects(id,task_id,project_id,tc_id,scenario,title,severity,"
            "error_type,error_message,page_url,screenshot,source,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, task_id, project_id, tc_id, scenario, title, severity,
             error_type, error_msg, execution_path, screenshot_path, "auto", "open", now, now),
        )
        saved += 1

    if saved:
        log_store.write(task_id, f"已记录 {saved} 条失败证据\n")
    return saved


def _write_environment(results_dir: str, task: dict, stats: dict):
    env_name = task.get("env", "test")
    # 优先使用任务中存储的 base_url，其次从 config/{env}.yaml 读取
    base_url = task.get("base_url") or ""
    if not base_url:
        env_config_path = os.path.join(settings.ROOT, "config", f"{env_name}.yaml")
        if os.path.exists(env_config_path):
            try:
                with open(env_config_path, "r", encoding="utf-8") as f:
                    env_info = yaml.safe_load(f) or {}
                    base_url = env_info.get("base_url", "")
            except Exception:
                pass

    os.makedirs(results_dir, exist_ok=True)
    env_file = os.path.join(results_dir, "environment.properties")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"URL={base_url}\n")
        f.write(f"ENV={env_name}\n")
        f.write(f"PROJECT={task.get('project_id', '')}\n")


def _attach_task_log_to_results(results_dir: str, task_id: str):
    """把任务执行日志补进 Allure 结果，确保正式/内置报告都能查看。"""
    content = log_store.read(task_id)
    if not content:
        return
    source = f"{uuid.uuid4().hex}-attachment.txt"
    with open(os.path.join(results_dir, source), "w", encoding="utf-8") as handle:
        handle.write(content)
    for result_path in glob.glob(os.path.join(results_dir, "*-result.json")):
        try:
            with open(result_path, "r", encoding="utf-8") as handle:
                result = json.load(handle)
            attachments = result.setdefault("attachments", [])
            if any(item.get("name") == "任务执行日志" for item in attachments):
                continue
            attachments.append({
                "name": "任务执行日志",
                "source": source,
                "type": "text/plain",
            })
            with open(result_path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False)
        except (OSError, json.JSONDecodeError):
            continue


def _generate_builtin_report(results_dir: str, report_dir: str, stats=None) -> bool:
    try:
        import html
        os.makedirs(report_dir, exist_ok=True)
        attachment_dir = os.path.join(report_dir, "attachments")
        os.makedirs(attachment_dir, exist_ok=True)
        cases = []

        def copy_attachments(attachments):
            images = []
            texts = []
            for attachment in attachments or []:
                media_type = str(attachment.get("type") or "")
                source = os.path.basename(str(attachment.get("source") or ""))
                if not source:
                    continue
                source_path = os.path.join(results_dir, source)
                if not os.path.isfile(source_path):
                    continue
                target_path = os.path.join(attachment_dir, source)
                if not os.path.exists(target_path):
                    shutil.copy2(source_path, target_path)
                item = {
                    "name": str(attachment.get("name") or "附件"),
                    "url": f"attachments/{source}",
                }
                if media_type.startswith("image/"):
                    images.append(item)
                elif media_type.startswith("text/") or media_type in {"application/json", "application/xml"}:
                    try:
                        with open(source_path, "r", encoding="utf-8", errors="replace") as handle:
                            item["content"] = handle.read(30000)
                    except OSError:
                        item["content"] = ""
                    texts.append(item)
            return images, texts

        def normalize_steps(steps):
            normalized = []
            for step in steps or []:
                details = step.get("statusDetails", {}) or {}
                images, texts = copy_attachments(step.get("attachments") or [])
                normalized.append({
                    "name": str(step.get("name") or "未命名步骤"),
                    "status": str(step.get("status") or "unknown"),
                    "message": str(details.get("message") or details.get("trace") or "")[:1200],
                    "images": images,
                    "texts": texts,
                    "steps": normalize_steps(step.get("steps") or []),
                })
            return normalized

        for f in sorted(glob.glob(os.path.join(results_dir, "*-result.json"))):
            try:
                with open(f, encoding="utf-8") as fp:
                    result = json.load(fp)
            except Exception:
                continue
            status = result.get("status", "unknown")
            name = result.get("name") or result.get("fullName") or os.path.basename(f)
            details = result.get("statusDetails", {}) or {}
            message = (details.get("message") or details.get("trace") or "")[:1200]
            images, texts = copy_attachments(result.get("attachments") or [])
            cases.append({
                "status": status,
                "name": str(name),
                "message": str(message),
                "images": images,
                "texts": texts,
                "steps": normalize_steps(result.get("steps") or []),
            })

        stats = dict(stats or _parse_results(results_dir))
        status_color = {
            "passed": "#16a34a",
            "failed": "#dc2626",
            "broken": "#ea580c",
            "skipped": "#64748b",
        }
        def render_images(images):
            if not images:
                return ""
            return '<div class="gallery">' + "".join(
                f'<a href="{html.escape(image["url"], quote=True)}" target="_blank" '
                f'title="{html.escape(image["name"], quote=True)}">'
                f'<img src="{html.escape(image["url"], quote=True)}" '
                f'alt="{html.escape(image["name"], quote=True)}" loading="lazy">'
                f'<span>{html.escape(image["name"])}</span></a>'
                for image in images
            ) + "</div>"

        def render_texts(texts):
            if not texts:
                return ""
            return '<div class="attachments">' + "".join(
                '<details class="attachment"><summary>'
                f'{html.escape(item["name"])}'
                f' <a href="{html.escape(item["url"], quote=True)}" target="_blank">下载</a>'
                '</summary>'
                f'<pre>{html.escape(item.get("content") or "")}</pre></details>'
                for item in texts
            ) + "</div>"

        def render_steps(steps, depth=0):
            if not steps:
                return '<div class="empty">没有记录到步骤明细</div>'
            return '<div class="steps">' + "".join(
                '<div class="step" style="margin-left:' + str(min(depth, 4) * 14) + 'px">'
                '<div class="step-head">'
                f'<span class="dot" style="background:{status_color.get(step["status"], "#64748b")}"></span>'
                f'<b>{html.escape(step["name"])}</b>'
                f'<span class="status" style="color:{status_color.get(step["status"], "#64748b")}">'
                f'{html.escape(step["status"])}</span></div>'
                + (f'<pre>{html.escape(step["message"])}</pre>' if step["message"] else "")
                + render_images(step["images"])
                + render_texts(step["texts"])
                + (render_steps(step["steps"], depth + 1) if step["steps"] else "")
                + '</div>'
                for step in steps
            ) + "</div>"

        case_rows = "\n".join(
            '<details class="case" open>'
            '<summary>'
            f'<span class="status" style="color:{status_color.get(case["status"], "#334155")}">'
            f'{html.escape(case["status"])}</span>'
            f'<span>{html.escape(case["name"])}</span>'
            f'<span class="step-count">{len(case["steps"])} 个步骤</span>'
            '</summary>'
            '<div class="case-body">'
            + (f'<pre>{html.escape(case["message"])}</pre>' if case["message"] else "")
            + render_images(case["images"])
            + render_texts(case["texts"])
            + render_steps(case["steps"])
            + '</div></details>'
            for case in cases
        )
        index_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>测试报告</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px;color:#0f172a;background:#f8fafc}}
.cards{{display:flex;gap:12px;margin:20px 0;flex-wrap:wrap}}
.card{{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:14px 18px;min-width:120px}}
pre{{white-space:pre-wrap;margin:0;font-size:12px;color:#475569}}
.case{{background:white;border:1px solid #e2e8f0;border-radius:10px;margin:12px 0;overflow:hidden}}
.case summary{{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;font-weight:700}}
.case-body{{padding:0 16px 16px}}
.status{{font-weight:800;text-transform:uppercase}}
.step-count{{margin-left:auto;color:#64748b;font-size:12px;font-weight:500}}
.steps{{display:flex;flex-direction:column;gap:10px;margin-top:12px}}
.step{{border-left:3px solid #e2e8f0;background:#f8fafc;border-radius:6px;padding:10px 12px}}
.step-head{{display:flex;align-items:center;gap:8px}}
.step-head .status{{margin-left:auto;font-size:12px}}
.dot{{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-top:10px}}
.gallery a{{display:flex;flex-direction:column;gap:6px;color:#334155;text-decoration:none;font-size:12px}}
.gallery img{{width:100%;height:220px;object-fit:contain;background:#0f172a;border-radius:7px;border:1px solid #cbd5e1}}
.attachments{{display:flex;flex-direction:column;gap:8px;margin-top:10px}}
.attachment{{border:1px solid #cbd5e1;border-radius:6px;background:white;padding:8px 10px}}
.attachment summary{{cursor:pointer;font-size:13px;font-weight:700}}
.attachment summary a{{float:right;color:#4f46e5;font-weight:500}}
.attachment pre{{max-height:320px;overflow:auto;margin-top:8px;background:#0f172a;color:#e2e8f0;padding:10px;border-radius:5px}}
.empty{{padding:12px;color:#94a3b8;font-size:13px}}
</style>
</head>
<body>
<h1>测试报告</h1>
<p>未检测到 Allure CLI，已生成包含步骤状态和截图的内置报告。</p>
<div class="cards">
<div class="card"><b>总数</b><br>{stats["total"]}</div>
<div class="card"><b>通过</b><br>{stats["passed"]}</div>
<div class="card"><b>失败</b><br>{stats["failed"]}</div>
<div class="card"><b>跳过</b><br>{stats["skipped"]}</div>
</div>
{case_rows or '<div class="case"><div class="case-body empty">没有找到测试结果</div></div>'}
</body>
</html>"""
        with open(os.path.join(report_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(index_html)
        return True
    except Exception:
        return False


def _generate_allure(results_dir: str, report_dir: str, stats=None) -> bool:
    try:
        allure_cmd = shutil.which(settings.ALLURE_CLI) or shutil.which("allure")
        if allure_cmd:
            subprocess.run(
                [allure_cmd, "generate", results_dir, "-o", report_dir, "--clean"],
                check=False, timeout=300,
            )

            env_src = os.path.join(results_dir, "environment.properties")
            env_dst = os.path.join(report_dir, "data", "environment.properties")
            if os.path.exists(env_src):
                os.makedirs(os.path.dirname(env_dst), exist_ok=True)
                shutil.copy2(env_src, env_dst)

            if os.path.exists(os.path.join(report_dir, "index.html")):
                return True
        return _generate_builtin_report(results_dir, report_dir, stats=stats)
    except Exception:
        return _generate_builtin_report(results_dir, report_dir, stats=stats)


def _update_task(task_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE tasks SET {cols} WHERE id=?", tuple(fields.values()) + (task_id,))


def _mark_task_running(task_id: str) -> bool:
    """Mark running only when a concurrent stop request has not already won."""
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status=?, started_at=? WHERE id=? AND status<>'stopped'",
            ("running", _now(), task_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _get_task(task_id: str) -> dict:
    rows = db.execute(
        "SELECT t.*, p.name as project_name FROM tasks t "
        "LEFT JOIN projects p ON t.project_id=p.id WHERE t.id=?", (task_id,), fetch=True)
    return db.to_dict(rows[0]) if rows else {}


# 保存正在运行的子进程，用于停止功能
_running_procs: dict = {}


def _update_scheduled_job_result(task: dict, status: str, stats: dict, report_url: str = ""):
    job_id = task.get("scheduled_job_id") or ""
    if not job_id:
        return
    success_delta = 1 if status == "success" else 0
    failed_delta = 1 if status == "failed" else 0
    result = {
        "task_id": task.get("id"),
        "status": status,
        "passed": stats.get("passed", 0),
        "failed": stats.get("failed", 0),
        "skipped": stats.get("skipped", 0),
        "total": stats.get("total", 0),
        "report_url": report_url,
        "finished_at": task.get("finished_at") or _now(),
    }
    db.execute(
        "UPDATE jobs SET successful_runs=COALESCE(successful_runs,0)+?,"
        "failed_runs=COALESCE(failed_runs,0)+?,last_result=?,updated_at=? WHERE id=?",
        (success_delta, failed_delta, json.dumps(result, ensure_ascii=False), _now(), job_id),
    )


def _update_suite_result(task: dict, status: str):
    suite_id = task.get("suite_id") or ""
    if not suite_id:
        return
    db.execute(
        "UPDATE test_suites SET status=?,last_run=?,last_task_id=?,updated_at=? WHERE id=?",
        (status, task.get("finished_at") or _now(), task.get("id") or "", _now(), suite_id),
    )


def _send_automatic_report_notification(task: dict, status: str, report_url: str = ""):
    """Send every finished UI report through the enabled unified integrations."""
    if status not in {"success", "failed"} or not task:
        return None
    payload = dict(task)
    if report_url:
        payload["report_url"] = report_url
    try:
        return feishu.send_card(payload)
    except Exception as exc:
        log_store.write(
            str(task.get("id") or ""),
            f"⚠️ 自动发送第三方通知失败: {type(exc).__name__}\n",
        )
        return None


def run_task(task_id: str):
    """执行一个任务（由 API 直接调用或 worker 从队列消费后调用）。"""
    try:
        test_object_bundle = _consume_test_object_bundle(task_id)
    except Exception as exc:
        test_object_bundle = {}
        log_store.write(task_id, f"⛔ 测试对象运行时配置读取失败: {exc}\n")
        _update_task(task_id, status="failed", finished_at=_now())
        step_store.finalize(task_id, "failed", error="测试对象运行时配置读取失败")
        return _get_task(task_id)
    task = _get_task(task_id)
    if not task:
        return {"error": "task not found"}

    # 防止重复执行：如果任务已经是 running/success/failed 状态，跳过
    current_status = (task.get("status") or "").lower()
    if current_status in ("running", "success", "failed"):
        return {"error": f"task already {current_status}"}
    # stopped 任务也不重新执行（避免队列残留导致重复执行）
    if current_status == "stopped":
        return {"error": "task was stopped, not re-running"}
    if current_status == "paused":
        return {"error": "task is paused, not running"}

    module = task.get("module") or "ui"
    if module == "all":
        module = "ui"
    if module != "ui":
        log_store.write(task_id, "⛔ 接口测试模块已移除，仅支持 UI 自动化任务\n")
        _update_task(task_id, status="failed", finished_at=_now())
        step_store.finalize(task_id, "failed", error="不支持的执行模块")
        return {"error": "unsupported module"}
    case_files = json.loads(task.get("case_files") or "[]")
    base, results_dir, report_dir = _task_dirs(task_id)
    shutil.rmtree(base, ignore_errors=True)
    _clear_runtime_caches()
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)

    if not _mark_task_running(task_id):
        log_store.write(task_id, "⛔ 任务已被用户停止，取消启动执行进程\n")
        step_store.finalize(task_id, "stopped", error="用户手动停止任务")
        return _get_task(task_id)
    log_store.write(task_id, f"🚀 任务 {task_id} 开始 | 项目={task['project_id']} 模块={module}\n")
    log_store.write(task_id, f"📌 用例文件: {case_files or '全部'}\n")

    env = os.environ.copy()
    env["TASK_ID"] = task_id  # 供 conftest 截图按任务归档
    if len(case_files) == 1:
        env["FLOW_CASE_FILE"] = str(case_files[0])
    env["ENV"] = task.get("env", "test")  # 供 yaml_loader.load_config 选择环境配置
    env["PROJECT_ID"] = task["project_id"]  # 供 conftest/yaml_loader 使用项目级配置
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 禁止生成 .pyc 缓存，确保每次执行读取最新代码
    env["PYTHONPATH"] = settings.ROOT + os.pathsep + env.get("PYTHONPATH", "")

    runtime_case_file = str(task.get("runtime_case_file") or "").strip()
    try:
        runtime_override = json.loads(task.get("runtime_override") or "[]")
        if runtime_case_file:
            if len(case_files) != 1 or os.path.basename(runtime_case_file) != os.path.basename(case_files[0]):
                raise ValueError("RuntimeCase 仅支持当前选中的单个 UI 用例")
            source_case_path = safe_child_path(
                settings.ROOT,
                runtime_case_file,
                allowed_suffixes=(".yaml", ".yml"),
            )
            with open(source_case_path, "r", encoding="utf-8") as handle:
                source_case = yaml.safe_load(handle) or {}
            runtime_case = RuntimeCase(source_case, runtime_override).build()
            runtime_payload = runtime_case.case_data
            if isinstance(runtime_payload, dict):
                runtime_payload["_runtime_data"] = runtime_case.execution_data
            runtime_case_path = os.path.join(base, "runtime-case.yaml")
            with open(runtime_case_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(runtime_payload, handle, allow_unicode=True, sort_keys=False)
            env["RUNTIME_CASE_FILE"] = runtime_case_path
            _update_task(
                task_id,
                runtime_data=json.dumps(runtime_case.execution_data, ensure_ascii=False),
            )
            log_store.write(
                task_id,
                f"🧩 RuntimeCase 已创建 | 输入字段={len(runtime_case.execution_data)} | 原始用例未修改\n",
            )
    except Exception as exc:
        message = f"RuntimeCase 创建失败: {exc}"
        log_store.write(task_id, f"❌ {message}\n")
        _update_task(task_id, status="failed", finished_at=_now())
        step_store.finalize(task_id, "failed", error=message)
        return _get_task(task_id)

    # 设置 BASE_URL 环境变量（执行时指定的 Base URL，覆盖页面对象中的默认值）
    base_url = task.get("base_url") or ""
    if base_url:
        env["BASE_URL"] = base_url
        log_store.write(task_id, f"🌐 Base URL: {base_url}\n")

    try:
        runtime_variables = json.loads(task.get("runtime_variables") or "{}")
    except (TypeError, ValueError):
        runtime_variables = {}
    runtime_context = {
        "global": {},
        "local": {},
        "outputs": {},
        "data": {},
        "dataAssets": {},
        "smartData": {},
    }
    if isinstance(runtime_variables, dict):
        parameter_row_limit = runtime_variables.pop(PARAMETER_ROW_LIMIT_VARIABLE, None)
        try:
            parameter_row_limit = (
                max(1, min(int(parameter_row_limit), MAX_DATA_ASSET_PARAMETER_ROWS))
                if parameter_row_limit is not None else None
            )
        except (TypeError, ValueError):
            parameter_row_limit = None
        runtime_context["local"].update(runtime_variables)
        applied_variables = []
        for name, value in runtime_variables.items():
            env_name = str(name or "").strip()
            if not env_name or not ENV_NAME_RE.match(env_name):
                continue
            env[env_name] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            applied_variables.append(env_name)
        if applied_variables:
            log_store.write(task_id, f"🧩 场景变量: {', '.join(applied_variables)}\n")
    else:
        runtime_variables = {}
        parameter_row_limit = None

    data_asset_case_files = _case_files_for_execution(
        task["project_id"],
        module,
        case_files,
    )
    if not case_files and data_asset_case_files:
        log_store.write(task_id, f"🧩 测试数据中心扫描用例: {len(data_asset_case_files)} 个\n")
    data_asset_context = _load_test_data_asset_context(
        task["project_id"],
        module,
        data_asset_case_files,
        runtime_variables,
        parameter_row_limit,
    )
    parameter_data_contexts = data_asset_context.pop("_parameterRows", []) if data_asset_context else []
    if data_asset_context:
        _deep_merge_dict(runtime_context, data_asset_context)
        asset_labels = []
        for alias, payload in data_asset_context.get("dataAssets", {}).items():
            keys = ", ".join(sorted(str(key) for key in payload)) if isinstance(payload, dict) else ""
            asset_labels.append(f"{alias}({keys})" if keys else str(alias))
        log_store.write(task_id, f"🧩 测试数据中心: {', '.join(asset_labels)}\n")
    runtime_context_rows = []
    if len(case_files) == 1 and len(parameter_data_contexts) > 1:
        for data_context in parameter_data_contexts:
            row_context = copy.deepcopy(runtime_context)
            _deep_merge_dict(row_context, data_context)
            runtime_context_rows.append(row_context)
        log_store.write(task_id, f"🔁 场景参数化: {len(runtime_context_rows)} 轮\n")
    env["RUNTIME_CONTEXT_JSON"] = json.dumps(runtime_context, ensure_ascii=False)
    env["RUNTIME_VARIABLES_JSON"] = json.dumps(runtime_variables, ensure_ascii=False)
    if runtime_context_rows:
        env["RUNTIME_CONTEXT_ROWS_JSON"] = json.dumps(runtime_context_rows, ensure_ascii=False)

    child_test_object_dir = ""
    if test_object_bundle:
        try:
            child_test_object_dir = os.path.join(base, ".runnergo-test-objects")
            os.makedirs(child_test_object_dir, mode=0o700, exist_ok=False)
            os.chmod(child_test_object_dir, 0o700)
            worker_ids = ["gw0", "gw1"] if importlib.util.find_spec("xdist") else ["main"]
            for worker_id in worker_ids:
                worker_path = os.path.join(child_test_object_dir, f"{worker_id}.json")
                descriptor = os.open(
                    worker_path,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(test_object_bundle, handle, ensure_ascii=False)
                os.chmod(worker_path, 0o600)
            env["RUNNERGO_TEST_OBJECTS_DIR"] = child_test_object_dir
            log_store.write(task_id, f"🔗 已加载 {len(test_object_bundle)} 个 RunnerGo 测试对象引用\n")
        except Exception as exc:
            if child_test_object_dir:
                shutil.rmtree(child_test_object_dir, ignore_errors=True)
            message = f"测试对象运行时配置准备失败: {exc}"
            log_store.write(task_id, f"❌ {message}\n")
            _update_task(task_id, status="failed", finished_at=_now())
            step_store.finalize(task_id, "failed", error=message)
            return _get_task(task_id)

    cmd = _build_pytest_args(module, case_files, results_dir, project_id=task["project_id"])
    log_store.write(task_id, f"$ {' '.join(cmd)}\n\n")

    start = time.time()
    stop_monitor = None
    try:
        import signal as _signal
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=settings.ROOT, env=env, text=True, bufsize=1,
            preexec_fn=os.setsid,  # 新建进程组，便于整组终止
        )
        _running_procs[task_id] = proc
        try:
            runner_pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            runner_pgid = 0
        _update_task(task_id, runner_pid=proc.pid, runner_pgid=runner_pgid)
        stop_monitor = _start_task_stop_monitor(task_id, proc)
        if _get_task(task_id).get("status") == "stopped":
            _terminate_task_runner(task_id, proc)
        for line in iter(proc.stdout.readline, ""):
            log_store.write(task_id, line)
            if _get_task(task_id).get("status") == "stopped" and proc.poll() is None:
                _terminate_task_runner(task_id, proc)
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        log_store.write(task_id, f"❌ 执行异常: {e}\n")
        rc = -1
    finally:
        if stop_monitor:
            stop_monitor.set()
        _running_procs.pop(task_id, None)
        _update_task(task_id, runner_pid=0, runner_pgid=0)
        if child_test_object_dir:
            shutil.rmtree(child_test_object_dir, ignore_errors=True)

    duration = round(time.time() - start, 2)
    pytest_stats = _parse_results(results_dir)
    stats = _prefer_step_store_stats(
        task_id,
        pytest_stats,
        enabled=len(case_files) == 1,
    )
    if len(case_files) != 1:
        log_store.write(
            task_id,
            "📐 批量 UI 任务统计按 pytest 用例级汇总；"
            "逐步骤状态暂不跨用例合并，避免最后一个用例覆盖批次数据\n",
        )
    if stats != pytest_stats:
        log_store.write(
            task_id,
            "📐 任务统计已按 UI 逐步骤状态汇总 | "
            f"pytest 用例级={pytest_stats['total']}，步骤级={stats['total']}；"
            f"通过={stats['passed']}，失败={stats['failed']}，跳过={stats['skipped']}\n",
        )

    # 检查是否被用户手动停止
    current = _get_task(task_id)
    if current.get("status") == "stopped":
        log_store.write(task_id, f"\n{'=' * 40}\n⛔ 任务已被用户停止 | 耗时={duration}s\n")
        step_store.finalize(task_id, "stopped", error="用户手动停止任务")
        _update_scheduled_job_result(current, "stopped", stats)
        _update_suite_result(current, "stopped")
        return current

    status = "success" if stats["failed"] == 0 and rc == 0 else "failed"
    finished_at = _now()

    task["finished_at"] = finished_at
    _write_environment(results_dir, task, stats)

    # 自动保存失败用例为缺陷
    if stats["failed"] > 0:
        try:
            _auto_save_defects(results_dir, task)
        except Exception as e:
            log_store.write(task_id, f"⚠️ 自动保存缺陷失败: {e}\n")
    current = _get_task(task_id)
    if current.get("status") == "stopped":
        log_store.write(task_id, f"\n{'=' * 40}\n⛔ 任务已被用户停止，跳过报告生成和通知 | 耗时={duration}s\n")
        step_store.finalize(task_id, "stopped", error="用户手动停止任务")
        _update_scheduled_job_result(current, "stopped", stats)
        _update_suite_result(current, "stopped")
        return current

    log_store.write(task_id, f"\n🧾 pytest 已结束，退出码={rc}，准备生成测试报告\n")
    _attach_task_log_to_results(results_dir, task_id)
    report_ok = _generate_allure(results_dir, report_dir, stats=stats)

    if report_ok:
        report_url = settings.report_url_for_task(task_id)
        # Use a signed, login-bypass URL for third-party notification cards so
        # recipients on Feishu / WeCom / DingTalk can open the report directly.
        public_report_url = settings.public_report_url_for_task(task_id)
    else:
        report_url = ""
        public_report_url = ""

    _update_task(
        task_id,
        status=status,
        passed=stats["passed"],
        failed=stats["failed"],
        skipped=stats["skipped"],
        total=stats["total"],
        duration=duration,
        finished_at=finished_at,
        report_url=report_url,
    )
    step_store.finalize(
        task_id,
        status,
        error="pytest 执行失败，详情请查看失败步骤和执行日志" if status == "failed" else "",
    )

    final = _get_task(task_id)
    _update_scheduled_job_result(final or task, status, stats, report_url)
    _update_suite_result(final or task, status)
    log_store.write(
        task_id,
        f"\n{'=' * 40}\n✅ 执行完成 | 状态={status} | 通过={stats['passed']} "
        f"失败={stats['failed']} 总计={stats['total']} | 耗时={duration}s\n"
    )
    if report_ok:
        log_store.write(task_id, f"📊 报告: {report_url}\n")

    # 手动执行和定时执行完成后，都使用“设置 → 第三方集成”中的启用通知自动发送。
    # stopped 不产生完整报告，因此不发送完成通知。
    # 通知卡使用带签名的公开 URL，免登录直接访问报告。
    _send_automatic_report_notification(
        final or task,
        status,
        public_report_url if report_ok else "",
    )
    return final


def create_task(project_id: str, module: str = "ui", case_files: list = None,
                triggered_by: str = "manual", env: str = "test",
                base_url: str = "", runtime_override: list = None,
                runtime_case_file: str = "", runtime_variables: dict = None,
                scheduled_job_id: str = "", suite_id: str = "") -> str:
    if module != "ui":
        raise ValueError("UI 自动化平台仅支持 ui 模块")
    task_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO tasks(id,project_id,module,case_files,env,status,triggered_by,started_at,base_url,"
        "runtime_override,runtime_case_file,runtime_data,runtime_variables,scheduled_job_id,suite_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, project_id, module, json.dumps(case_files or [], ensure_ascii=False),
         env, "pending", triggered_by, _now(), base_url,
         json.dumps(runtime_override or [], ensure_ascii=False), runtime_case_file, "[]",
         json.dumps(runtime_variables or {}, ensure_ascii=False), scheduled_job_id, suite_id),
    )
    return task_id


def dispatch(task_id: str) -> bool:
    """负载均衡调度：Redis 可用则入队由 worker 消费，否则直接本地执行。"""
    from backend import redis_queue
    if redis_queue.available():
        redis_queue.push_task({"task_id": task_id})
        return False  # 异步
    run_task(task_id)
    return True  # 同步已完成
