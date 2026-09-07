"""测试规划器：把用户业务需求拆解为 ReAct Agent 测试计划。

- decompose_requirement：拆解为步骤计划（data / api / ui / app / browser / security / performance / report）
- build_test_dimension_plan：生成功能/异常/边界/安全/性能测试点
- build_workflow_plan：组合步骤计划 + 维度计划
"""
from __future__ import annotations

import re
from typing import Any, Optional

from backend.llm.provider import chat_json, is_configured


_LOGIN_TERMS = re.compile(r"登录|登陆|sign\s*in|login|认证|验证码", re.IGNORECASE)
_REGISTER_TERMS = re.compile(r"注册|登记|sign\s*up|register", re.IGNORECASE)
_MALL_TERMS = re.compile(r"商城|购物|商品|搜索|订单|下单|购买|支付|结算|库存|优惠券|购物车", re.IGNORECASE)
_PERF_TERMS = re.compile(r"性能|压测|压力|并发|负载|吞吐|tps|qps|响应时间", re.IGNORECASE)
_APP_TERMS = re.compile(r"app|移动端|android|ios|手机应用|客户端", re.IGNORECASE)
_API_TERMS = re.compile(r"接口|api|rest|http|后端", re.IGNORECASE)
_UI_TERMS = re.compile(r"页面|ui|前端|浏览器|web", re.IGNORECASE)
_SECURITY_TERMS = re.compile(r"安全|漏洞|注入|sql|xss|越权|jwt|ssrf|扫描|敏感信息", re.IGNORECASE)
_BROWSER_TERMS = re.compile(r"页面|浏览器|视觉|点击|输入框|登录页|购物车页面|表单|chrome", re.IGNORECASE)


def _detect_type(requirement: str) -> set[str]:
    kinds = set()
    if _API_TERMS.search(requirement):
        kinds.add("api")
    if _UI_TERMS.search(requirement) or _MALL_TERMS.search(requirement) or _LOGIN_TERMS.search(requirement):
        kinds.add("ui")
    if _APP_TERMS.search(requirement):
        kinds.add("app")
    if _PERF_TERMS.search(requirement):
        kinds.add("performance")
    if _SECURITY_TERMS.search(requirement):
        kinds.add("security")
    if _BROWSER_TERMS.search(requirement):
        kinds.add("browser")
    return kinds


def discover_ui_case_files(requirement: str, project_id: str = "default") -> list[str]:
    """把需求匹配到项目里真实存在的 UI 用例 YAML。

    优先采用需求里显式点名的脚本（如 test_xxx.yaml），否则复用 legacy
    的启发式匹配（文件名/描述/步骤名称相似度），全程不依赖模型。
    """
    requirement = str(requirement or "").strip()
    if not requirement:
        return []
    try:
        from backend.agent.legacy import (
            _collect_case_assets,
            _heuristic_case_selection,
            _normalize_selected_files,
        )
        assets = _collect_case_assets(project_id or "default")
        if not assets:
            return []
        explicit = re.findall(
            r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]+\.ya?ml",
            requirement,
            flags=re.IGNORECASE,
        )
        if explicit:
            selected = _normalize_selected_files(explicit, assets)
            if selected:
                return selected
        return _heuristic_case_selection(requirement, assets)
    except Exception:  # noqa: BLE001 - 匹配失败不阻塞工作流
        return []


def _attach_ui_case_files(steps: list[dict], requirement: str, project_id: str) -> list[dict]:
    """给尚未指定 case_files 的 run_web_test 步骤补上匹配到的真实用例。"""
    case_files = discover_ui_case_files(requirement, project_id)
    if not case_files:
        return steps
    for step in steps:
        if step.get("tool") != "run_web_test":
            continue
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        if payload.get("case_files"):
            continue
        payload["case_files"] = list(case_files)
        step["payload"] = payload
        step["action"] = f"执行已匹配 UI 用例：{', '.join(case_files)}"
    return steps


def _rule_step_plan(requirement: str, project_id: str = "default") -> tuple[list[dict], str]:
    """确定性规则：拆解步骤计划，保证无模型时依然可用。"""
    steps: list[dict] = []
    kinds = _detect_type(requirement)
    has_api = any(s.get("type") == "api" for s in steps)
    has_ui = any(s.get("type") == "ui" for s in steps)

    if _LOGIN_TERMS.search(requirement) or _REGISTER_TERMS.search(requirement):
        steps.append({
            "step": len(steps) + 1,
            "type": "data",
            "tool": "generate_test_data",
            "action": "生成100个测试用户（用户名/手机号/邮箱/密码）",
            "payload": {"asset_type": "USER", "count": 100,
                        "fields": ["name", "phone", "email", "password"]},
        })
        action = "注册" if _REGISTER_TERMS.search(requirement) else "登录"
        steps.append({
            "step": len(steps) + 1,
            "type": "api",
            "tool": "run_api_test",
            "action": f"测试{action}接口（正确/错误密码/账号不存在/验证码错误）",
            "payload": {"method": "POST", "url": "/api/auth/login" if action == "登录" else "/api/auth/register"},
        })
        has_api = True
        steps.append({
            "step": len(steps) + 1,
            "type": "ui",
            "tool": "run_web_test",
            "action": f"执行{action}页面 UI 自动化测试",
            "payload": {},
        })
        has_ui = True

    if _MALL_TERMS.search(requirement):
        steps.append({
            "step": len(steps) + 1,
            "type": "data",
            "tool": "generate_test_data",
            "action": "生成商品与订单测试数据",
            "payload": {"asset_type": "PRODUCT", "count": 50, "fields": ["search", "name", "amount"]},
        })
        steps.append({
            "step": len(steps) + 1,
            "type": "api",
            "tool": "run_api_test",
            "action": "测试商品搜索/加购/下单接口",
            "payload": {"method": "GET", "url": "/api/products/search"},
        })
        has_api = True
        steps.append({
            "step": len(steps) + 1,
            "type": "ui",
            "tool": "run_web_test",
            "action": "执行商城核心流程 UI 测试（搜索→加购→提交订单）",
            "payload": {},
        })
        has_ui = True

    if "app" in kinds:
        steps.append({
            "step": len(steps) + 1,
            "type": "app",
            "tool": "run_app_test",
            "action": "执行 APP 端自动化测试",
            "payload": {},
        })
    if "security" in kinds:
        steps.append({
            "step": len(steps) + 1,
            "type": "security",
            "tool": "run_security_test",
            "action": "调用 AI Security Agent 分析接口并执行 SQL注入/越权/JWT/敏感信息探测",
            "payload": {"target": "", "parameters": []},
        })
    if "browser" in kinds:
        steps.append({
            "step": len(steps) + 1,
            "type": "browser",
            "tool": "run_browser_agent",
            "action": "通过 Playwright MCP 打开 Chrome，截图并用视觉模型分析页面后执行操作",
            "payload": {"url": "", "goal": requirement},
        })
    if "performance" in kinds:
        steps.append({
            "step": len(steps) + 1,
            "type": "performance",
            "tool": "run_api_test",
            "action": "执行性能/并发基线验证",
            "payload": {"method": "GET", "url": "/api/health", "timeout": 30},
        })

    if "api" in kinds and not has_api:
        steps.append({
            "step": len(steps) + 1,
            "type": "api",
            "tool": "run_api_test",
            "action": "测试需求相关 API 接口",
            "payload": {"method": "GET", "url": "/api/health"},
        })
    if "ui" in kinds and not has_ui:
        steps.append({
            "step": len(steps) + 1,
            "type": "ui",
            "tool": "run_web_test",
            "action": "执行需求相关 UI 自动化测试",
            "payload": {},
        })
    if not steps:
        steps.append({
            "step": 1,
            "type": "ui",
            "tool": "run_web_test",
            "action": "执行需求相关 UI 自动化测试",
            "payload": {},
        })

    steps = _attach_ui_case_files(steps, requirement, project_id)

    steps.append({
        "step": len(steps) + 1,
        "type": "report",
        "tool": "create_report",
        "action": "生成测试报告",
        "payload": {},
    })
    return steps, "agent_planner"


def _model_step_plan(requirement: str) -> Optional[list[dict]]:
    """LLM 拆解步骤计划；失败或格式非法时返回 None。"""
    if not is_configured():
        return None
    system = (
        "你是资深测试平台架构师。把用户业务需求拆解为 AI Agent 测试步骤计划。"
        "只输出 JSON：{\"steps\": [{\"step\": 1, \"type\": \"data|api|ui|app|browser|security|performance|report\", "
        "\"action\": \"步骤描述\", \"tool\": \"generate_test_data|generate_test_case|generate_from_api|run_api_test|run_web_test|run_browser_agent|run_app_test|run_security_test|analyze_failure|create_report\", "
        "\"payload\": {}}], \"summary\": \"一句话计划摘要\"}。"
        "步骤必须包含 data、api、ui、browser、analysis、report；"
        "需求含安全时增加 security，含移动端时增加 app，含性能时增加 performance。"
    )
    try:
        data = chat_json(system, f"需求：{requirement}", max_tokens=800, temperature=0.2)
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            return None
        allowed_types = {"data", "api", "ui", "app", "browser", "security", "analysis", "performance", "report"}
        allowed_tools = {
            "generate_test_data", "generate_test_case", "generate_from_api",
            "run_api_test", "run_web_test", "run_browser_agent", "run_app_test",
            "run_security_test", "analyze_failure", "create_report",
        }
        cleaned = []
        for index, step in enumerate(steps[:20], 1):
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type", "")).strip()
            tool = str(step.get("tool", "")).strip()
            if step_type not in allowed_types or tool not in allowed_tools:
                continue
            cleaned.append({
                "step": index,
                "type": step_type,
                "tool": tool,
                "action": str(step.get("action") or "")[:200],
                "payload": step.get("payload") if isinstance(step.get("payload"), dict) else {},
            })
        if not cleaned:
            return None
        return cleaned
    except Exception:  # noqa: BLE001 - 模型失败回退规则
        return None


def decompose_requirement(requirement: str, project_id: str = "default") -> dict:
    """拆解需求 -> ReAct 步骤计划。模型可用时优先，失败自动走 Agent Planner。"""
    requirement = str(requirement or "").strip()
    if not requirement:
        raise ValueError("测试需求不能为空")
    model_steps = _model_step_plan(requirement) if is_configured() else None
    if model_steps:
        steps, source = model_steps, "agent_planner"
    else:
        steps, source = _rule_step_plan(requirement, project_id)
    steps = _attach_ui_case_files(steps, requirement, project_id)
    summary = f"针对「{requirement[:40]}」的 AI Agent 测试计划"
    has_report = any(s.get("type") == "report" for s in steps)
    if not has_report:
        steps.append({
            "step": len(steps) + 1,
            "type": "report",
            "tool": "create_report",
            "action": "生成测试报告",
            "payload": {},
        })
    return {"summary": summary, "source": source, "steps": steps}


def _dimension_item(title: str, steps: list[str], test_data: str = "", expected: str = "") -> dict:
    return {
        "title": title,
        "steps": steps,
        "test_data": test_data,
        "expected": expected or "按预期完成，无异常报错",
    }


def _rule_dimension_plan(requirement: str) -> dict:
    """确定性规则：功能/异常/边界/安全/性能测试点。"""
    subject = "登录" if _LOGIN_TERMS.search(requirement) else (
        "注册" if _REGISTER_TERMS.search(requirement) else
        ("下单" if "下单" in requirement or "订单" in requirement else
         ("搜索" if "搜索" in requirement else "核心业务"))
    )
    is_login = subject in {"登录", "注册"}
    plan: dict[str, list[dict]] = {}
    if is_login:
        plan["functional"] = [
            _dimension_item(f"{subject}功能-正常流程", [f"输入正确账号密码", f"点击{subject}按钮", "断言进入首页"], "正确账号/密码", "登录成功并跳转首页"),
            _dimension_item(f"{subject}功能-记住登录态", ["勾选记住我", "登录", "刷新页面"], "", "登录态保持"),
        ]
        plan["exception"] = [
            _dimension_item(f"{subject}异常-密码错误", ["输入错误密码", "提交"], "错误密码", "提示「密码错误」"),
            _dimension_item(f"{subject}异常-账号不存在", ["输入未注册账号", "提交"], "未注册账号", "提示「账号不存在」"),
            _dimension_item(f"{subject}异常-验证码错误", ["输入错误验证码", "提交"], "错误验证码", "提示「验证码错误」"),
        ]
        plan["boundary"] = [
            _dimension_item(f"{subject}边界-密码最大长度", ["输入 64 位密码", "提交"], "64 位密码", "按设计处理，不崩溃"),
            _dimension_item(f"{subject}边界-特殊字符", ["输入含特殊字符的账号", "提交"], "special!@#$%", "正确校验或明确提示"),
        ]
        plan["security"] = [
            _dimension_item(f"{subject}安全-SQL注入", [f"在账号框输入 SQL 注入载荷", "提交"], "' OR '1'='1", "拒绝执行并提示非法输入"),
            _dimension_item(f"{subject}安全-暴力破解", ["连续错误密码 5 次", "继续尝试"], "错误密码 x5", "触发锁定/验证码/限流"),
            _dimension_item(f"{subject}安全-Token测试", ["登录后篡改 Token", "访问受保护接口"], "篡改 Token", "返回 401 并拒绝访问"),
        ]
    else:
        plan["functional"] = [
            _dimension_item(f"{subject}功能-正常流程", [f"执行{subject}主流程", "断言关键结果"], "", "流程成功且数据正确"),
            _dimension_item(f"{subject}功能-空数据", ["无历史数据时执行", "观察展示"], "", "空态友好提示"),
        ]
        plan["exception"] = [
            _dimension_item(f"{subject}异常-服务异常", ["后端返回 500 时", "观察前端"], "500 响应", "友好错误提示，不白屏"),
            _dimension_item(f"{subject}异常-网络超时", ["接口超时", "观察前端"], "超时响应", "重试/提示，不阻塞"),
        ]
        plan["boundary"] = [
            _dimension_item(f"{subject}边界-超长输入", ["输入 1000 字符", "提交"], "1000 字符", "截断或明确提示"),
            _dimension_item(f"{subject}边界-分页边界", ["访问第 1/最后一页", "切换"], "", "数据完整，无重复遗漏"),
        ]
        plan["security"] = [
            _dimension_item(f"{subject}安全-越权访问", ["低权限访问高权限接口", "观察"], "", "返回 403"),
            _dimension_item(f"{subject}安全-注入防护", ["提交注入载荷", "观察"], "' OR 1=1--", "拒绝并提示"),
        ]
    plan["performance"] = [
        _dimension_item(f"{subject}性能-单用户基线", ["单用户重复执行 10 次", "统计响应时间"], "", "P95 响应时间达标"),
        _dimension_item(f"{subject}性能-并发验证", ["50 并发执行", "观察错误率"], "50 并发", "错误率 < 1%，无雪崩"),
    ]
    return plan


def build_test_dimension_plan(requirement: str) -> dict:
    """生成功能/异常/边界/安全/性能测试点。模型可用时增强，失败回退规则。"""
    requirement = str(requirement or "").strip()
    rule_plan = _rule_dimension_plan(requirement)
    if not is_configured():
        return rule_plan
    system = (
        "你是资深测试架构师。根据业务需求输出测试点 JSON："
        "{\"functional\":[{\"title\":\"...\",\"steps\":[...],\"test_data\":\"...\",\"expected\":\"...\"}],"
        "\"exception\":[...],\"boundary\":[...],\"security\":[...],\"performance\":[...]}。"
        "每个维度至少 2 条，必须包含功能/异常/边界/安全/性能。"
    )
    try:
        data = chat_json(system, f"需求：{requirement}", max_tokens=1200, temperature=0.2)
        plan: dict[str, list[dict]] = {}
        for key in ("functional", "exception", "boundary", "security", "performance"):
            items = data.get(key)
            if not isinstance(items, list) or not items:
                plan[key] = rule_plan.get(key, [])
                continue
            cleaned = []
            for item in items[:10]:
                if not isinstance(item, dict):
                    continue
                cleaned.append({
                    "title": str(item.get("title") or "")[:120],
                    "steps": [str(s) for s in (item.get("steps") or []) if s][:10],
                    "test_data": str(item.get("test_data") or ""),
                    "expected": str(item.get("expected") or ""),
                })
            plan[key] = cleaned if cleaned else rule_plan.get(key, [])
        return plan
    except Exception:  # noqa: BLE001 - 模型失败回退规则
        return rule_plan


def build_workflow_plan(requirement: str, project_id: str = "default") -> dict:
    """组合步骤计划 + 维度计划。"""
    step_plan = decompose_requirement(requirement, project_id=project_id)
    dimension_plan = build_test_dimension_plan(requirement)
    return {
        "summary": step_plan["summary"],
        "source": step_plan["source"],
        "steps": step_plan["steps"],
        "dimensions": dimension_plan,
    }
