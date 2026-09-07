from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from django.conf import settings

from backend.llm.provider import LLMProvider
from backend.agent.url_utils import infer_http_url

from .contracts import E2EContractError, normalize_browser_step
from .data_generator_agent import DataGeneratorAgent
from .page_inspector import PageInspector


class PlannerAgent:
    """Turn a natural-language test goal into a validated E2E plan."""

    def __init__(
        self,
        *,
        llm: LLMProvider | None = None,
        data_generator: DataGeneratorAgent | None = None,
        page_inspector: PageInspector | None = None,
    ):
        self.llm = llm or LLMProvider()
        self.data_generator = data_generator or DataGeneratorAgent()
        self.page_inspector = page_inspector if page_inspector is not None else (
            PageInspector() if llm is None else None
        )

    def plan(
        self,
        description: str,
        *,
        executor_type: str = 'web',
        target_url: str = '',
        mobile_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        description = str(description or '').strip()
        if not description:
            raise E2EContractError('description 不能为空')
        executor_type = str(executor_type or 'web').lower()
        if executor_type not in {'web', 'android', 'ios'}:
            raise E2EContractError('executor_type 仅支持 web/android/ios')
        target_url = target_url or self._infer_url(description)
        page_context = self._inspect_pages(
            description=description,
            executor_type=executor_type,
            target_url=target_url,
        )
        generated_test_data = self.data_generator.generate(
            description=f'{description}\n{json.dumps(page_context, ensure_ascii=False)}',
            scenario=None,
        )
        generated = self._plan_with_llm(
            description,
            executor_type=executor_type,
            target_url=target_url,
            mobile_config=mobile_config or {},
            page_context=page_context,
            test_data=generated_test_data,
        )
        plan = self._normalize_plan(
            generated,
            description=description,
            executor_type=executor_type,
            target_url=target_url,
        )
        normalization_fallback = plan.pop('_used_fallback', False)
        used_model_plan = bool(generated) and not normalization_fallback
        plan_source = 'llm' if used_model_plan else 'rules'
        plan['steps'] = self._ensure_execution_target(
            plan['steps'],
            executor_type=executor_type,
            target_url=target_url,
            mobile_config=mobile_config or {},
        )
        plan['steps'] = self._ensure_requested_assertions(
            plan['steps'],
            description=description,
            executor_type=executor_type,
        )
        plan['scenario']['test_points'] = self._extract_test_points(description)
        plan['test_data'] = generated_test_data
        missing = self._missing_inputs(executor_type, target_url, mobile_config or {})
        missing.extend(self._missing_coverage_inputs(
            description,
            steps=plan['steps'],
            executor_type=executor_type,
            plan_source=plan_source,
        ))
        missing.extend(self._missing_test_data(plan['steps'], plan['test_data']))
        plan['source'] = plan_source
        plan['planner'] = {
            'model_enabled': bool(self.llm.enabled),
            'configuration_source': str(getattr(self.llm, 'configuration_source', '') or ''),
            'configuration_name': str(getattr(self.llm, 'configuration_name', '') or ''),
            'fallback_reason': (
                str(getattr(self.llm, 'last_error', '') or '')[:1000]
                or ('模型输出未通过 E2E 动作校验' if generated and normalization_fallback else '')
            ) if not used_model_plan else '',
            'inspected_pages': len([item for item in page_context if not item.get('error')]),
        }
        plan['needs_input'] = list(dict.fromkeys(missing))
        return plan

    def _plan_with_llm(
        self,
        description: str,
        *,
        executor_type: str,
        target_url: str,
        mobile_config: dict[str, Any],
        page_context: list[dict[str, Any]],
        test_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.llm.enabled:
            return None
        if executor_type == 'web':
            action_contract = (
                'Web action 只能是 navigate/fill/click/submit/wait_for_url/wait_for_text/'
                'wait_for_element/assert_text/assert_visible/assert_url/assert_title/assert_value/'
                'select/check/uncheck/press/hover/screenshot。'
                'Web target 只能使用 role+name、label、placeholder、text、test_id、css 等声明式定位。'
            )
        else:
            action_contract = (
                '移动端 action 只能是 launch_app/tap/input/swipe/wait/assert_text/'
                'assert_visible/back/hide_keyboard/screenshot。'
                '移动端 target 只能使用 accessibility_id、id/resource_id、class_name、'
                'android_uiautomator、ios_predicate 等 Appium 语义定位。'
                '复杂业务流程必须包含 tap/input/swipe/back 中的真实交互以及结果断言。'
            )
        system = (
            '你是 AI E2E 测试规划 Agent。只输出 JSON 对象，禁止输出代码或说明。'
            '输出字段必须包含 scenario{name,objective,preconditions}, steps[], expected_result。'
            f'{action_contract}'
            '不得生成 javascript、shell、Python 或 XPath。'
            '每个 steps 项必须是可直接执行的原子动作，禁止输出 actions/expected 子数组。'
            '必须逐项覆盖用户列出的每一个测试点；复杂业务流程不能只输出打开页面和截图。'
            'value 可以引用 ${test_data.normal.0.<field>}，不得编造验证码、生产账号或真实支付数据。'
            'page_context 是浏览器对真实页面的只读探测结果；定位器、页面文案和 URL 必须以它为准。'
            'page_context 没有出现的控件或路由不得猜测，缺少前置数据时写入 scenario.preconditions。'
            '执行范围只来自用户描述和传入的目标配置，不得自行添加生产环境、沙箱或隔离账号等前提。'
        )
        user = json.dumps({
            'description': description,
            'executor_type': executor_type,
            'target_url': target_url,
            'mobile_config': mobile_config,
            'explicit_test_points': self._extract_test_points(description),
            'page_context': page_context,
            'available_test_data': test_data.get('normal', [{}])[0],
        }, ensure_ascii=False)
        result = self.llm.chat_json([
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ], timeout=float(getattr(settings, 'E2E_PLANNER_TIMEOUT_SECONDS', 90)), temperature=0.1)
        return result if isinstance(result, dict) else None

    def _normalize_plan(
        self,
        raw: dict[str, Any] | None,
        *,
        description: str,
        executor_type: str,
        target_url: str,
    ) -> dict[str, Any]:
        fallback = self._fallback_plan(description, executor_type=executor_type, target_url=target_url)
        if not raw:
            return fallback
        scenario_raw = raw.get('scenario')
        if isinstance(scenario_raw, str):
            scenario_raw = {'name': scenario_raw}
        scenario_raw = scenario_raw if isinstance(scenario_raw, dict) else {}
        scenario = {
            'name': str(scenario_raw.get('name') or fallback['scenario']['name'])[:200],
            'objective': str(scenario_raw.get('objective') or raw.get('test_objective') or description)[:2000],
            'preconditions': [str(item)[:500] for item in (scenario_raw.get('preconditions') or []) if str(item).strip()][:20],
        }
        raw_steps = raw.get('steps') if isinstance(raw.get('steps'), list) else []
        used_fallback = False
        try:
            steps = self._normalize_steps(raw_steps, executor_type=executor_type)
        except E2EContractError:
            steps = []
        if not steps:
            steps = fallback['steps']
            used_fallback = True
        return {
            'scenario': scenario,
            'steps': steps,
            'expected_result': str(raw.get('expected_result') or fallback['expected_result'])[:2000],
            '_used_fallback': used_fallback,
        }

    def _normalize_steps(self, steps: list[Any], *, executor_type: str) -> list[dict[str, Any]]:
        if executor_type == 'web':
            normalized = []
            for index, item in enumerate(steps[:200], 1):
                try:
                    normalized.append(normalize_browser_step(item, index))
                except E2EContractError:
                    continue
            return normalized
        allowed = {
            'launch_app', 'tap', 'input', 'swipe', 'wait', 'assert_text',
            'assert_visible', 'screenshot', 'back', 'hide_keyboard',
        }
        normalized = []
        for index, raw in enumerate(steps[:200], 1):
            if not isinstance(raw, dict):
                raise E2EContractError(f'第 {index} 步必须是对象')
            action = str(raw.get('action') or '').lower()
            if action not in allowed:
                raise E2EContractError(f'移动端动作不受支持: {action}')
            normalized.append({
                'id': str(raw.get('id') or index),
                'name': str(raw.get('name') or raw.get('description') or f'步骤 {index}')[:300],
                'action': action,
                'target': raw.get('target') if isinstance(raw.get('target'), dict) else {},
                'value': raw.get('value'),
                'expected': raw.get('expected'),
                'timeout_ms': min(max(int(raw.get('timeout_ms') or 15000), 100), 120000),
                'continue_on_failure': bool(raw.get('continue_on_failure', False)),
            })
        return normalized

    def _fallback_plan(self, description: str, *, executor_type: str, target_url: str) -> dict[str, Any]:
        subject = re.sub(r'https?://\S+', '', description).strip(' ，。')[:80] or '端到端业务流程'
        if executor_type != 'web':
            steps = [
                {'id': '1', 'name': '启动 App', 'action': 'launch_app', 'target': {}, 'value': None, 'expected': None, 'timeout_ms': 30000, 'continue_on_failure': False},
                {'id': '2', 'name': '采集启动页', 'action': 'screenshot', 'target': {}, 'value': None, 'expected': None, 'timeout_ms': 15000, 'continue_on_failure': False},
            ]
        else:
            raw_steps: list[dict[str, Any]] = [
                {'name': '打开目标页面', 'action': 'navigate', 'url': target_url or 'https://example.test'},
            ]
            lower = description.lower()
            if any(word in lower for word in ('登录', 'login', '登陆')):
                raw_steps.extend([
                    {'name': '输入用户名', 'action': 'fill', 'target': {'role': 'textbox', 'name': '用户名'}, 'value': '${test_data.normal.0.username}'},
                    {'name': '输入密码', 'action': 'fill', 'target': {'label': '密码'}, 'value': '${test_data.normal.0.password}'},
                    {'name': '点击登录', 'action': 'click', 'target': {'role': 'button', 'name': '登录'}},
                ])
            if any(word in lower for word in ('搜索', '商品', '商城', '购买', '下单')):
                raw_steps.extend([
                    {'name': '搜索商品', 'action': 'fill', 'target': {'role': 'searchbox'}, 'value': '${test_data.normal.0.keyword}'},
                    {'name': '提交搜索', 'action': 'press', 'target': {'role': 'searchbox'}, 'value': 'Enter'},
                ])
            if any(word in lower for word in ('购物车', '加购')):
                raw_steps.append({'name': '加入购物车', 'action': 'click', 'target': {'role': 'button', 'name': '加入购物车'}})
            if any(word in lower for word in ('下单', '购买', '结算')):
                raw_steps.append({'name': '进入结算', 'action': 'click', 'target': {'role': 'button', 'name': '结算'}})
            if any(word in lower for word in ('支付', '付款')):
                raw_steps.append({
                    'name': '点击支付',
                    'action': 'click',
                    'target': {'role': 'button', 'name': '支付'},
                })
            raw_steps.extend(self._business_flow_steps(description))
            raw_steps.extend(self._structured_test_point_steps(
                description,
                target_url=target_url,
            ))
            raw_steps.append({'name': '保存最终页面证据', 'action': 'screenshot'})
            steps = self._normalize_steps(raw_steps, executor_type='web')
        return {
            'scenario': {
                'name': subject,
                'objective': description,
                'preconditions': [],
            },
            'steps': steps,
            'expected_result': f'{subject}完整流程符合预期，关键页面、状态和错误日志均有执行证据。',
        }

    def _business_flow_steps(self, description: str) -> list[dict[str, Any]]:
        """Create conservative text-based steps for structured Chinese requirements.

        The rules fallback cannot infer arbitrary form locators, but a requirement
        containing several explicit business clauses still provides useful targets
        such as page labels and submit buttons. Keep a single generic clause as
        NEEDS_INPUT so vague requests are not silently treated as executable.
        """
        if not self._requires_business_flow(description):
            return []
        supplemented_text = self._supplemented_business_text(description)
        if self._extract_test_points(description) and not supplemented_text:
            # A numbered test-point list describes coverage, not literal button
            # labels. Turning those phrases into clicks creates false execution.
            return []
        flow_text = supplemented_text or description
        clauses = [
            clause.strip(' \t\r\n，,。；;、')
            for clause in re.split(r'(?:->|→|[，,。；;、\n])+', flow_text)
        ]
        clauses = [
            re.sub(r'^(?:\d+[\s、.．)]+)?(?:测试|验证|检查)?\s*', '', clause).strip()
            for clause in clauses
            if clause.strip()
        ]
        if len(clauses) < 2:
            return []

        action_words = (
            '申请', '提交', '签约', '下单', '购买', '登录', '注册', '搜索', '查询',
            '确认', '下一步', '继续', '保存', '删除', '新增', '创建', '审批', '支付',
            '付款', '结算', '加购', '补充', '填写', '输入', '选择', '点击',
        )
        context_words = ('页面', '信息', '资料', '详情', '首页', '列表', '状态')
        already_covered_words = (
            '登录', '登陆', '注册', '搜索', '购物车', '加购', '下单', '购买',
            '结算', '支付', '付款',
        )
        steps: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for clause in clauses:
            if len(clause) < 2:
                continue
            if any(word in clause for word in already_covered_words):
                continue
            target_text = re.sub(r'(?:功能|流程)$', '', clause).strip()
            if len(target_text) < 2:
                continue
            if any(word in clause for word in action_words):
                action = 'click'
                step = {
                    'name': f'执行{target_text}',
                    'action': action,
                    'target': {'text': target_text},
                }
            elif any(word in clause for word in context_words):
                action = 'wait_for_text'
                step = {
                    'name': f'确认{target_text}',
                    'action': action,
                    'value': target_text,
                    'expected': target_text,
                }
            else:
                continue
            key = (action, target_text)
            if key not in seen:
                seen.add(key)
                steps.append(step)
        return steps

    def _supplemented_business_text(self, description: str) -> str:
        marker = '--- 用户补充的执行条件 ---'
        if marker not in str(description or ''):
            return ''
        supplement = str(description).split(marker, 1)[1]
        match = re.search(
            r'可执行业务步骤：\s*(.*?)(?:\n验收标准：|$)',
            supplement,
            flags=re.S,
        )
        return match.group(1).strip() if match else ''

    def _structured_test_point_steps(
        self,
        description: str,
        *,
        target_url: str,
    ) -> list[dict[str, Any]]:
        """Turn explicit page targets into executable, evidence-producing checks."""
        steps: list[dict[str, Any]] = []
        current_url = target_url
        for point in self._extract_test_points(description):
            page_url = self._test_point_url(point, target_url=target_url)
            if page_url:
                if page_url != current_url:
                    steps.append({
                        'name': f'打开测试页面：{point}',
                        'action': 'navigate',
                        'url': page_url,
                    })
                    current_url = page_url
                steps.extend([
                    {
                        'name': f'验证页面地址：{point}',
                        'action': 'assert_url',
                        'expected': page_url,
                    },
                    {
                        'name': f'保存页面证据：{point}',
                        'action': 'screenshot',
                    },
                ])
                continue
            steps.append({
                'name': f'验证测试点：{point}',
                'action': 'assert_visible',
                'target': {'text': point},
            })
        return steps

    def _test_point_url(self, point: str, *, target_url: str) -> str:
        value = str(point or '').strip().rstrip(').]，。；;')
        if re.match(r'^https?://', value, flags=re.I):
            return value
        if re.match(r'^[A-Za-z0-9.-]+(?::\d+)?/', value):
            scheme = urlsplit(target_url).scheme or 'http'
            return f'{scheme}://{value}'
        if value.startswith('/') and target_url:
            return urljoin(target_url, value)
        return ''

    def _ensure_execution_target(
        self,
        steps: list[dict[str, Any]],
        *,
        executor_type: str,
        target_url: str,
        mobile_config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized = [dict(step) for step in steps]
        if executor_type == 'web':
            if not target_url:
                return normalized
            if normalized and normalized[0].get('action') == 'navigate':
                normalized[0]['url'] = target_url
                return normalized
            return [
                normalize_browser_step({
                    'id': 'open-target-url',
                    'name': '打开目标页面',
                    'action': 'navigate',
                    'url': target_url,
                }, 1),
                *normalized,
            ]

        app_identifier = str(
            mobile_config.get('bundle_id') or mobile_config.get('app_package') or ''
        ).strip()
        if not app_identifier:
            return normalized
        if normalized and normalized[0].get('action') == 'launch_app':
            normalized[0]['value'] = app_identifier
            return normalized
        return [{
            'id': 'launch-target-app',
            'name': '启动目标 App',
            'action': 'launch_app',
            'target': {},
            'value': app_identifier,
            'expected': None,
            'timeout_ms': 30000,
            'continue_on_failure': False,
        }, *normalized]

    def _missing_inputs(self, executor_type: str, target_url: str, mobile_config: dict[str, Any]) -> list[str]:
        missing = []
        if executor_type == 'web' and not target_url:
            missing.append('target_url')
        if executor_type in {'android', 'ios'}:
            if not mobile_config.get('server_url'):
                missing.append('mobile_config.server_url')
            if not mobile_config.get('device_name') and not mobile_config.get('udid'):
                missing.append('mobile_config.device_name_or_udid')
            if not (mobile_config.get('app') or mobile_config.get('app_package') or mobile_config.get('bundle_id')):
                missing.append('mobile_config.app_identifier')
        return missing

    def _inspect_pages(
        self,
        *,
        description: str,
        executor_type: str,
        target_url: str,
    ) -> list[dict[str, Any]]:
        if executor_type != 'web' or not target_url or not self.llm.enabled or self.page_inspector is None:
            return []
        try:
            return self.page_inspector.inspect(target_url=target_url, description=description)
        except Exception as exc:
            return [{'requested_url': target_url, 'error': str(exc)[:500]}]

    def _missing_coverage_inputs(
        self,
        description: str,
        *,
        steps: list[dict[str, Any]],
        executor_type: str,
        plan_source: str,
    ) -> list[str]:
        if not self._requires_business_flow(description):
            return []
        actions = {str(step.get('action') or '').lower() for step in steps}
        meaningful_actions = actions & (
            {
                'fill', 'click', 'submit', 'select', 'check', 'uncheck', 'press', 'hover',
            }
            if executor_type == 'web'
            else {'tap', 'input', 'swipe', 'back'}
        )
        missing = []
        if not meaningful_actions:
            missing.append('business_steps')

        test_points = self._extract_test_points(description)
        uncovered = [point for point in test_points if not self._step_covers_point(steps, point)]
        if uncovered:
            missing.append('test_point_coverage')
        return missing

    def _requires_business_flow(self, description: str) -> bool:
        text = str(description or '').lower()
        test_points = self._extract_test_points(description)
        if len(test_points) >= 2 and all(
            self._test_point_url(point, target_url=self._infer_url(description))
            for point in test_points
        ):
            return False
        if len(test_points) >= 2:
            return True
        if any(word in text for word in ('smoke', '冒烟', '可用性', '打开页面', '访问页面', '页面可达', '截图')):
            return False
        keywords = (
            '流程', '链路', '申请', '提交', '交易', '下单', '购买', '结算', '支付',
            '注册', '登录', '登陆', '搜索', '创建', '新增', '编辑', '删除', '审批',
            '表单', 'checkout', 'order', 'purchase', 'payment', 'login', 'register',
            'submit', 'create', 'edit', 'delete', 'flow', 'journey', 'e2e',
        )
        return any(keyword in text for keyword in keywords)

    def _missing_test_data(self, steps: list[dict[str, Any]], test_data: dict[str, Any]) -> list[str]:
        references = set()
        for step in steps:
            references.update(re.findall(
                r'\$\{(test_data(?:\.[A-Za-z0-9_-]+)+)\}',
                json.dumps(step, ensure_ascii=False),
            ))
        missing = []
        for reference in sorted(references):
            current: Any = test_data
            try:
                for part in reference.split('.')[1:]:
                    current = current[int(part)] if isinstance(current, list) else current[part]
            except (KeyError, IndexError, TypeError, ValueError):
                missing.append(reference)
        return missing

    def _extract_test_points(self, description: str) -> list[str]:
        text = str(description or '')
        bracketed = [item.strip() for item in re.findall(r'【([^】]+)】', text) if item.strip()]
        if bracketed:
            return list(dict.fromkeys(bracketed))[:50]
        page_urls = [
            item.rstrip('，,。；;、）)】]')
            for item in re.findall(
                r'https?://[^\s，,。；;、）)】\]]+'
                r'|(?<![\w@])(?:localhost|(?:\d{1,3}\.){3}\d{1,3})'
                r'(?::\d{1,5})?/[^\s，,。；;、）)】\]]*',
                text,
                flags=re.I,
            )
        ]
        if len(page_urls) >= 2:
            return list(dict.fromkeys(page_urls))[:50]
        numbered = [
            item.strip(' ，。；;、')
            for item in re.split(r'(?:^|\s)\d+[、.．)]\s*', text)
            if item.strip(' ，。；;、')
        ]
        return list(dict.fromkeys(numbered))[:50] if len(numbered) >= 2 else []

    def _step_covers_point(self, steps: list[dict[str, Any]], point: str) -> bool:
        normalized_point = self._coverage_text(point)
        if not normalized_point:
            return False
        for step in steps:
            haystack = self._coverage_text(json.dumps(step, ensure_ascii=False, sort_keys=True))
            if normalized_point in haystack or haystack in normalized_point:
                return True
            if '/' in normalized_point:
                path = normalized_point.split('/', 1)[1]
                if path and path in haystack:
                    return True
        return False

    def _coverage_text(self, value: Any) -> str:
        text = str(value or '').lower().strip()
        text = re.sub(r'^https?://', '', text)
        text = re.sub(r'^测试\s*', '', text)
        return re.sub(r'[\s，。；;、【】/?=&:_-]+', '', text)

    def _ensure_requested_assertions(
        self,
        steps: list[dict[str, Any]],
        *,
        description: str,
        executor_type: str,
    ) -> list[dict[str, Any]]:
        if executor_type != 'web':
            return steps
        expected_title = self._requested_title(description)
        if not expected_title or any(step.get('action') == 'assert_title' for step in steps):
            return steps
        assertion = normalize_browser_step({
            'id': 'requested-title-assertion',
            'name': '验证页面标题',
            'action': 'assert_title',
            'expected': expected_title,
        }, len(steps) + 1)
        insert_at = len(steps)
        while insert_at and steps[insert_at - 1].get('action') == 'screenshot':
            insert_at -= 1
        return [*steps[:insert_at], assertion, *steps[insert_at:]]

    def _requested_title(self, description: str) -> str:
        patterns = (
            r'(?:验证|断言|检查|确认)?(?:当前|目标)?(?:页面)?标题\s*(?:包含|含有|为|等于|是)\s*[“”"\'「」『』]?(.+?)(?=，|。|；|;|\s+(?:并且|并|且)\s*|$)',
            r'(?:page\s+)?title\s+(?:contains?|equals?|is)\s*["\']?(.+?)(?=,|\.|;|\s+and\s+|$)',
        )
        for pattern in patterns:
            match = re.search(pattern, description, flags=re.I)
            if match:
                return match.group(1).strip(' \t\r\n“”"\'「」『』')[:500]
        return ''

    def _infer_url(self, description: str) -> str:
        return infer_http_url(description)
