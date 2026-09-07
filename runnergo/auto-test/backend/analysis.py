"""结果分析层：AI 日志分析、自动归因（失败原因分类）。

能力：
- 解析 Allure result.json 提取失败用例
- 根据错误信息自动归因到具体原因类别
"""
import os
import json
import glob
import re

from backend import db, settings


# ===== 归因规则 =====
ATTRIBUTION_RULES = [
    {
        "category": "代码问题",
        "description": "测试代码本身的缺陷（JS拼接、语法错误、类型错误等）",
        "patterns": [
            r"SyntaxError",
            r"Page\.evaluate.*missing",
            r"NameError",
            r"AttributeError",
            r"TypeError",
            r"KeyError",
            r"IndexError",
            r"ImportError",
        ],
        "suggestion": "检查测试代码中的 JS 拼接、变量定义、导入语句",
    },
    {
        "category": "用例数据问题",
        "description": "测试数据不合法或与业务规则不匹配",
        "patterns": [
            r"身份证.*格式不正确",
            r"校验码.*错误",
            r"格式不正确",
            r"AssertionError.*期望.*但",
            r"Failed.*期望.*但未跳转",
        ],
        "suggestion": "检查 YAML 用例数据是否符合业务规则（如身份证校验码、车牌号等）",
    },
    {
        "category": "环境问题",
        "description": "测试环境或基础设施异常",
        "patterns": [
            r"Operation not permitted",
            r"Connection refused",
            r"Connection reset",
            r"ERR_CONNECTION",
            r"ECONNREFUSED",
            r"ENOTFOUND",
            r"502 Bad Gateway",
            r"503 Service Unavailable",
        ],
        "suggestion": "检查被测系统是否正常运行、网络是否通畅、权限是否正确",
    },
    {
        "category": "被测系统缺陷",
        "description": "被测系统功能异常或业务逻辑限制",
        "patterns": [
            r"超时.*URL包含.*submit",
            r"超时.*URL包含.*fill",
            r"超时.*URL包含.*result",
            r"TimeoutError.*等待URL",
            r"应在网申签约页",
            r"未跳转",
        ],
        "suggestion": "排查被测系统业务逻辑（必填字段校验、跳转条件、人车一致性等）",
    },
    {
        "category": "断言失败",
        "description": "测试断言未通过，可能是 UI 文本不一致或功能未实现",
        "patterns": [
            r"AssertionError",
            r"assert False",
            r"婚姻状况",
            r"应显示",
        ],
        "suggestion": "检查断言条件是否与被测系统实际行为一致（如同义词匹配）",
    },
]


def _match_category(error_msg: str) -> dict:
    """根据错误信息匹配归因类别。"""
    if not error_msg:
        return {
            "category": "未知",
            "description": "无错误信息",
            "suggestion": "查看完整日志获取更多信息",
        }
    for rule in ATTRIBUTION_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, error_msg, re.IGNORECASE):
                return {
                    "category": rule["category"],
                    "description": rule["description"],
                    "suggestion": rule["suggestion"],
                }
    return {
        "category": "其他",
        "description": "未匹配到已知归因规则",
        "suggestion": "人工分析失败原因",
    }


def analyze_task(task_id: str) -> dict:
    """分析任务的失败用例，返回 AI 日志分析和自动归因结果。

    返回结构：
    {
        "task_id": "...",
        "summary": {"total": 98, "passed": 71, "failed": 16, ...},
        "allure_report_url": "...",
        "failures": [
            {
                "name": "test_xxx[case0]",
                "tc_id": "TC-XX-01",
                "scenario": "...",
                "status": "failed/broken",
                "error_message": "...",
                "attribution": {"category": "...", "description": "...", "suggestion": "..."},
            }
        ],
        "attribution_summary": {"代码问题": 3, "用例数据问题": 2, ...},
        "top_suggestion": "最优先修复建议",
    }
    """
    # 获取任务信息
    rows = db.execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,), fetch=True
    )
    if not rows:
        return {"error": "任务不存在"}
    task = db.to_dict(rows[0])

    # 解析 Allure result.json
    results_dir = os.path.join(settings.REPORT_DIR, "tasks", task_id, "results")
    failures = []
    passed = failed = broken = skipped = total = 0

    for f in glob.glob(os.path.join(results_dir, "*-result.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                result = json.load(fp)
        except Exception:
            continue

        status = result.get("status", "")
        total += 1
        if status == "passed":
            passed += 1
        elif status == "failed":
            failed += 1
        elif status == "broken":
            broken += 1
        elif status == "skipped":
            skipped += 1

        if status not in ("failed", "broken"):
            continue

        # broken 也算失败
        if status == "broken":
            failed += 1

        name = result.get("name", "unknown")
        labels = result.get("labels", [])
        tc_id = ""
        scenario = ""
        for label in labels:
            if label.get("name") == "tc_id":
                tc_id = label.get("value", "")
            elif label.get("name") == "scenario":
                scenario = label.get("value", "")

        # 从 parameters 提取 scenario 和 tc_id
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
                    except Exception:
                        pass

        status_details = result.get("statusDetails", {})
        error_msg = status_details.get("message", "") or status_details.get("trace", "")
        error_msg = error_msg[:2000] if error_msg else ""

        # 自动归因
        attribution = _match_category(error_msg)

        failures.append({
            "name": name,
            "tc_id": tc_id,
            "scenario": scenario,
            "status": status,
            "error_message": error_msg,
            "attribution": attribution,
        })

    # 归因汇总
    attribution_summary = {}
    for f in failures:
        cat = f["attribution"]["category"]
        attribution_summary[cat] = attribution_summary.get(cat, 0) + 1

    # 最优先修复建议
    top_suggestion = ""
    if attribution_summary:
        # 按数量排序，取最多的类别建议
        top_cat = max(attribution_summary.items(), key=lambda x: x[1])[0]
        for f in failures:
            if f["attribution"]["category"] == top_cat:
                top_suggestion = f"【{top_cat}】{f['attribution']['suggestion']}"
                break

    return {
        "task_id": task_id,
        "task_status": task.get("status"),
        "module": "ui",
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "duration": task.get("duration"),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "broken": broken,
            "skipped": skipped,
        },
        "allure_report_url": settings.normalize_report_url(task.get("report_url", "")),
        "failures": failures,
        "attribution_summary": attribution_summary,
        "top_suggestion": top_suggestion,
    }
