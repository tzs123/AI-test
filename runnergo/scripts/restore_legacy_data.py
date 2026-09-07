#!/usr/bin/env python3
"""Generate idempotent SQL for restoring the legacy RunnerGo UI data.

The source of truth is the preserved auto-test SQLite database and YAML cases.
The generated records use a ``legacy_`` prefix so this migration can be rerun
without touching data created by the user in the legacy application.
"""

from __future__ import annotations

import json
import argparse
import shlex
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "auto-test" / "runtime" / "platform.db"
CASES_DIR = ROOT / "auto-test" / "cases" / "ui"

TEAM_ID = "e64b9392-7ab9-4d94-a6d7-00acd96540af"
USER_ID = "d0291907-03f6-4be2-b536-bfbe10a4cfa9"


def sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def dt(value: Any, fallback: str = "2026-08-29 00:00:00") -> str:
    if not value:
        return fallback
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def compact_id(prefix: str, value: str, limit: int = 96) -> str:
    return (prefix + str(value).replace("-", ""))[:limit]


def locator_rows(raw: Any) -> list[dict[str, Any]]:
    try:
        source = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        source = []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(source if isinstance(source, list) else []):
        if not isinstance(item, dict):
            continue
        strategy = str(item.get("strategy") or item.get("method") or "css")
        value = item.get("value", "")
        row: dict[str, Any] = {
            "id": f"loc_{index}",
            "method": strategy,
            "type": strategy,
            "value": value,
            "key": item.get("name") or item.get("role") or "",
            "index": int(item.get("index", -1) or -1),
            "is_checked": 1,
        }
        if strategy == "role":
            row["type"] = "role"
            row["value"] = item.get("name") or item.get("value") or ""
        rows.append(row)
    return rows or [{"id": "loc_0", "method": "css", "type": "css", "value": "", "key": "", "index": -1, "is_checked": 1}]


def operator_kind(action: str) -> tuple[str, str]:
    mapping = {
        "goto": ("browser", "open_page"),
        "open_page": ("browser", "open_page"),
        "click": ("mouse", "mouse_clicking"),
        "fill": ("input_operations", "input_operations"),
        "press": ("input_operations", "input_operations"),
        "hover": ("mouse", "mouse_movement"),
        "scroll": ("mouse", "mouse_scrolling"),
        "assert": ("assert", "assert"),
        "wait": ("wait_events", "wait_events"),
        "screenshot": ("browser", "screenshot"),
    }
    return mapping.get(str(action).lower(), ("browser", str(action or "action")))


def emit_insert(table: str, columns: Iterable[str], values: Iterable[Any], key: str, prefix: str = "") -> None:
    cols = list(columns)
    vals = list(values)
    where = f"{key} LIKE {sql(prefix + '%')}" if prefix else f"{key}={sql(vals[cols.index(key)])}"
    print(f"DELETE FROM `{table}` WHERE {where};")
    print(f"INSERT INTO `{table}` ({','.join('`'+c+'`' for c in cols)}) VALUES ({','.join(sql(v) for v in vals)});")


def mongo_mode_conf(config: dict[str, Any]) -> dict[str, int]:
    protocol = str(config.get("mode") or "").lower() == "protocol"
    concurrency = int(config.get("virtual_users") or config.get("max_concurrency") or 0)
    return {
        "reheat_time": int(config.get("ramp_up_seconds") or 0),
        "round_num": int(config.get("iterations_per_user") or 0),
        "concurrency": concurrency,
        "threshold_value": 0,
        "start_concurrency": int(config.get("start_concurrency") or (concurrency if protocol else 1)),
        "step": int(config.get("step") or (0 if protocol else 1)),
        "step_run_time": int(config.get("step_run_time") or (0 if protocol else 30)),
        "max_concurrency": int(config.get("max_concurrency") or concurrency),
        "duration": int(config.get("duration_seconds") or 0),
    }


def write_mongo_restore(path: Path, runs: Iterable[sqlite3.Row]) -> None:
    documents = []
    for run in runs:
        config = json.loads(run["config_json"] or "{}")
        report_id = compact_id("legacy_stress_report_", str(run["id"]))
        plan_id = compact_id("legacy_stress_plan_", str(run["id"]))
        scenario_name = run["scenario_name"] or run["case_file"] or str(run["id"])
        task_mode = 1 if run["mode"] == "protocol" else 2
        documents.append({
            "team_id": TEAM_ID,
            "plan_id": plan_id,
            "plan_name": f"性能恢复计划 - {scenario_name}",
            "report_id": report_id,
            "task_type": 1,
            "task_mode": task_mode,
            "mode_conf": mongo_mode_conf(config),
        })
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    script = (
        "// Generated by restore_legacy_data.py; safe to rerun.\n"
        f"const legacyReportTasks = {payload};\n"
        "legacyReportTasks.forEach(function (doc) {\n"
        "  db.report_task.replaceOne({report_id: doc.report_id}, doc, {upsert: true});\n"
        "});\n"
    )
    path.write_text(script, encoding="utf-8")


def main(mongo_output: Path | None = None) -> None:
    source = sqlite3.connect(SOURCE_DB)
    source.row_factory = sqlite3.Row
    projects = source.execute("SELECT * FROM projects WHERE id='default'").fetchall()
    if not projects:
        raise SystemExit(f"source project not found: {SOURCE_DB}")

    project = dict(projects[0])
    project_name = str(project.get("name") or "国信小米")
    cases: list[tuple[Path, dict[str, Any]]] = []
    for case_path in sorted(CASES_DIR.glob("*.y*ml")):
        payload = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        cases.append((case_path, payload))

    print("SET NAMES utf8mb4;")
    print("SET FOREIGN_KEY_CHECKS=0;")
    print("START TRANSACTION;")

    # Only delete records generated by this script. Existing non-legacy data is preserved.
    for table, column in (
        ("target", "target_id"),
        ("ui_scene_element", "scene_id"),
        ("ui_scene_operator", "scene_id"),
        ("ui_scene", "scene_id"),
        ("element", "element_id"),
        ("ui_plan_task_conf", "plan_id"),
        ("ui_plan_timed_task_conf", "plan_id"),
        ("ui_plan_report", "report_id"),
        ("ui_plan", "plan_id"),
        ("stress_plan_task_conf", "plan_id"),
        ("stress_plan_timed_task_conf", "plan_id"),
        ("stress_plan_report", "report_id"),
        ("stress_plan", "plan_id"),
    ):
        print(f"DELETE FROM `{table}` WHERE `{column}` LIKE 'legacy_%';")

    elements = source.execute(
        "SELECT * FROM web_elements WHERE project_id='default' ORDER BY created_at,id"
    ).fetchall()
    element_ids: dict[str, str] = {}
    for row in elements:
        src_id = str(row["id"])
        element_id = compact_id("legacy_elem_", src_id)
        element_ids[src_id] = element_id
        locators = locator_rows(row["locators"])
        for index, item in enumerate(locators):
            item["id"] = f"{element_id}_{index}"
        created = dt(row["created_at"])
        updated = dt(row["updated_at"], created)
        print(
            "INSERT INTO `element` (`element_id`,`element_type`,`team_id`,`name`,`parent_id`,`locators`,`sort`,`version`,"
            "`created_user_id`,`description`,`source`,`source_id`,`created_at`,`updated_at`) VALUES ("
            + ",".join(
                sql(v)
                for v in (
                    element_id,
                    "element",
                    TEAM_ID,
                    row["name"] or "未命名元素",
                    "0",
                    locators,
                    0,
                    1,
                    USER_ID,
                    f"从 TestHub UI 自动化恢复：{row['page_url'] or ''}",
                    1,
                    src_id,
                    created,
                    updated,
                )
            )
            + ");"
        )

    scene_ids: dict[str, str] = {}
    for case_path, payload in cases:
        case_name = case_path.name
        scene_id = compact_id("legacy_scene_", case_path.stem)
        scene_ids[case_name] = scene_id
        browsers = payload.get("browser") or {"engine": "chromium", "viewport": {"width": 1280, "height": 720}}
        browser_record = [{"browser_type": "chromium", "headless": False, "size_type": "default", "set_size": {"x": 0, "y": 0}, "source": browsers}]
        created = "2026-08-29 00:00:00"
        print(
            "INSERT INTO `ui_scene` (`scene_id`,`scene_type`,`team_id`,`name`,`parent_id`,`sort`,`status`,`version`,"
            "`source`,`plan_id`,`created_user_id`,`recent_user_id`,`description`,`ui_machine_key`,`source_id`,`browsers`,`created_at`,`updated_at`) VALUES ("
            + ",".join(
                sql(v)
                for v in (
                    scene_id,
                    "scene",
                    TEAM_ID,
                    f"{payload.get('name') or 'UI 自动化场景'} - {case_name}",
                    "0",
                    len(scene_ids),
                    1,
                    1,
                    1,
                    "",
                    USER_ID,
                    USER_ID,
                    payload.get("description") or f"从 TestHub 恢复的 UI 用例：{case_name}",
                    "",
                    case_name,
                    browser_record,
                    created,
                    created,
                )
            )
            + ");"
        )
        for position, step in enumerate(payload.get("steps") or [], start=1):
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "action")
            op_id = compact_id(f"legacy_op_{case_path.stem}_", str(step.get("id") or position))
            op_type, op_action = operator_kind(action)
            print(
                "INSERT INTO `ui_scene_operator` (`operator_id`,`scene_id`,`name`,`parent_id`,`sort`,`status`,`type`,`action`,`created_at`,`updated_at`) VALUES ("
                + ",".join(sql(v) for v in (op_id, scene_id, step.get("name") or action, "0", position, 1, op_type, op_action, created, created))
                + ");"
            )
            element = step.get("element")
            if isinstance(element, dict) and element.get("id") in element_ids:
                print(
                    "INSERT INTO `ui_scene_element` (`scene_id`,`operator_id`,`element_id`,`team_id`,`status`,`created_at`,`updated_at`) VALUES ("
                    + ",".join(sql(v) for v in (scene_id, op_id, element_ids[element["id"]], TEAM_ID, 1, created, created))
                    + ");"
                )

    jobs = source.execute("SELECT * FROM jobs WHERE project_id='default' ORDER BY created_at,id").fetchall()
    plan_ids: list[str] = []
    for rank, job in enumerate(jobs, start=1):
        plan_id = compact_id("legacy_ui_plan_", str(job["id"]))
        plan_ids.append(plan_id)
        created = dt(job["created_at"])
        print(
            "INSERT INTO `ui_plan` (`plan_id`,`team_id`,`rank_id`,`name`,`task_type`,`create_user_id`,`head_user_id`,`run_count`,`init_strategy`,`description`,`browsers`,`ui_machine_key`,`created_at`,`updated_at`) VALUES ("
            + ",".join(sql(v) for v in (plan_id, TEAM_ID, rank, job["name"] or f"UI 自动化计划 {rank}", 2 if job["cron"] else 1, USER_ID, USER_ID, job["total_runs"] or 0, 1, job["description"] or f"恢复自 TestHub 定时任务 {job['id']}", [{"browser_type": "chromium", "headless": False, "size_type": "default", "set_size": {"x": 0, "y": 0}}], "", created, dt(job["updated_at"], created)))
            + ");"
        )
        if job["cron"]:
            print(
                "INSERT INTO `ui_plan_timed_task_conf` (`plan_id`,`team_id`,`frequency`,`task_exec_time`,`task_close_time`,`fixed_interval_start_time`,`fixed_interval_time`,`fixed_run_num`,`fixed_interval_time_type`,`task_type`,`scene_run_order`,`status`,`run_user_id`,`created_at`,`updated_at`) VALUES ("
                + ",".join(sql(v) for v in (plan_id, TEAM_ID, 1, 0, 0, 0, 0, 0, 0, 2, 1, 0, USER_ID, created, dt(job["updated_at"], created)))
                + ");"
            )
        else:
            print(
                "INSERT INTO `ui_plan_task_conf` (`plan_id`,`team_id`,`task_type`,`scene_run_order`,`run_user_id`,`created_at`,`updated_at`) VALUES ("
                + ",".join(sql(v) for v in (plan_id, TEAM_ID, 1, 1, USER_ID, created, dt(job["updated_at"], created)))
                + ");"
            )

    ui_tasks = source.execute("SELECT * FROM tasks WHERE project_id='default' AND module='ui' ORDER BY started_at,id").fetchall()
    default_plan = plan_ids[0] if plan_ids else "legacy_ui_plan_default"
    for rank, task in enumerate(ui_tasks, start=1):
        report_id = compact_id("legacy_ui_report_", str(task["id"]))
        created = dt(task["started_at"] or task["finished_at"])
        finished = dt(task["finished_at"], created)
        try:
            duration_ms = max(0, int((datetime.fromisoformat(finished) - datetime.fromisoformat(created)).total_seconds() * 1000))
        except ValueError:
            duration_ms = 0
        status = 2 if task["status"] in ("success", "failed", "stopped") else 1
        print(
            "INSERT INTO `ui_plan_report` (`report_id`,`report_name`,`plan_id`,`plan_name`,`team_id`,`rank_id`,`task_type`,`scene_run_order`,`run_duration_time`,`status`,`run_user_id`,`remark`,`browsers`,`ui_machine_key`,`created_at`,`updated_at`) VALUES ("
            + ",".join(sql(v) for v in (report_id, f"UI 自动化报告 {rank} - {task['id']}", default_plan, jobs[0]["name"] if jobs else project_name, TEAM_ID, rank, 1, 1, duration_ms, status, USER_ID, f"状态：{task['status']}，通过 {task['passed'] or 0}，失败 {task['failed'] or 0}，总计 {task['total'] or 0}", [{"browser_type": "chromium", "headless": False, "size_type": "default", "set_size": {"x": 0, "y": 0}}], "", created, finished))
            + ");"
        )

    runs = source.execute("SELECT * FROM load_test_runs ORDER BY created_at").fetchall()
    if mongo_output:
        write_mongo_restore(mongo_output, runs)
    for rank, run in enumerate(runs, start=1):
        config = json.loads(run["config_json"] or "{}")
        summary = json.loads(run["summary_json"] or "{}")
        report_id = compact_id("legacy_stress_report_", str(run["id"]))
        plan_id = compact_id("legacy_stress_plan_", str(run["id"]))
        scene_id = compact_id("legacy_perf_scene_", str(run["id"]))
        created = dt(run["started_at"] or run["created_at"])
        finished = dt(run["finished_at"], created)
        duration_ms = int(float(summary.get("elapsed_seconds") or 0) * 1000)
        task_mode = 1 if run["mode"] == "protocol" else 2
        plan_name = f"性能恢复计划 - {run['scenario_name'] or run['case_file']}"
        print(
            "INSERT INTO `stress_plan` (`plan_id`,`team_id`,`rank_id`,`plan_name`,`task_type`,`task_mode`,`status`,`create_user_id`,`run_user_id`,`remark`,`run_count`,`created_at`,`updated_at`) VALUES ("
            + ",".join(sql(v) for v in (plan_id, TEAM_ID, rank, plan_name, 1, task_mode, 1, USER_ID, USER_ID, f"恢复自性能记录 {run['id']}", 1, created, finished))
            + ");"
        )
        print(
            "INSERT INTO `stress_plan_task_conf` (`plan_id`,`team_id`,`scene_id`,`task_type`,`task_mode`,`control_mode`,`debug_mode`,`mode_conf`,`is_open_distributed`,`machine_dispatch_mode_conf`,`run_user_id`,`created_at`,`updated_at`) VALUES ("
            + ",".join(sql(v) for v in (plan_id, TEAM_ID, scene_id, 1, task_mode, 0, "stop", config, 0, "{}", USER_ID, created, finished))
            + ");"
        )
        # The legacy performance dashboard derives API/scene totals from the
        # target table. Keep a target entry for each recovered performance
        # scenario so the plan is visible as a referenced scene there.
        target_id = compact_id("legacy_perf_target_", str(run["id"]))
        print(
            "INSERT INTO `target` (`target_id`,`team_id`,`target_type`,`name`,`parent_id`,`method`,`sort`,`type_sort`,`status`,`version`,`created_user_id`,`recent_user_id`,`description`,`source`,`plan_id`,`source_id`,`is_checked`,`is_disabled`,`created_at`,`updated_at`) VALUES ("
            + ",".join(
                sql(v)
                for v in (
                    target_id,
                    TEAM_ID,
                    "scene",
                    run["scenario_name"] or run["case_file"] or f"性能场景 {rank}",
                    "0",
                    "",
                    rank,
                    0,
                    1,
                    1,
                    USER_ID,
                    USER_ID,
                    f"从 TestHub 性能记录恢复：{run['id']}",
                    2,
                    plan_id,
                    scene_id,
                    1,
                    0,
                    created,
                    finished,
                )
            )
            + ");"
        )
        print(
            "INSERT INTO `stress_plan_report` (`report_id`,`report_name`,`team_id`,`plan_id`,`rank_id`,`plan_name`,`scene_id`,`scene_name`,`task_type`,`task_mode`,`control_mode`,`debug_mode`,`run_duration_time`,`status`,`remark`,`run_user_id`,`created_at`,`updated_at`) VALUES ("
            + ",".join(sql(v) for v in (report_id, f"性能报告 {rank} - {run['id']}", TEAM_ID, plan_id, rank, plan_name, scene_id, run["scenario_name"] or run["case_file"], 1, task_mode, 0, "stop", duration_ms, 2, f"请求数 {summary.get('completed', 0)}，成功 {summary.get('passed', 0)}，失败 {summary.get('failed', 0)}，P95 {summary.get('p95_ms', 0)} ms", USER_ID, created, finished))
            + ");"
        )

    print("COMMIT;")
    print("SET FOREIGN_KEY_CHECKS=1;")
    print("-- restore_legacy_data completed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mongo-js",
        type=Path,
        help="同时生成可由 mongo shell 执行的 report_task 恢复脚本",
    )
    main(mongo_output=parser.parse_args().mongo_js)
