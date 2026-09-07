from __future__ import annotations

import hashlib
import math
import re
import uuid
from typing import Any, Iterable
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.agent.models import (
    AgentKnowledgeChunk,
    AgentKnowledgeDocument,
    AgentMemory,
    AgentTask,
    TestAsset,
    TestExecutionResult,
)
from apps.core.models import TestDataAsset
from apps.projects.models import Project
from backend.agent.analyzer import FailureAnalyzer
from backend.agent.core.executor import ReActAgentExecutor
from backend.agent.core.planner import ReActPlanner
from backend.agent.url_utils import infer_http_url
from backend.browser_agent.browser_controller import WebVisionAgent
from backend.data_agent.data_generator import DataAgent
from backend.security_agent.scanner import SecurityAgent


class AgentCapabilityService:
    """High-level AI Test Agent capabilities shared by UI and APP automation."""

    def __init__(self, user: Any):
        self.user = user

    def explore_web_system(
        self,
        *,
        url: str,
        goal: str = '建立系统地图',
        inputs: dict[str, Any] | None = None,
        project_id: int | None = None,
        save_memory: bool = True,
    ) -> dict[str, Any]:
        browser_result = WebVisionAgent().run(url=url, goal=goal, inputs=inputs or {})
        elements = browser_result.get('elements') or []
        opened = browser_result.get('opened') or {'url': url, 'title': ''}
        page_name = self._page_name(opened.get('title') or '', url, elements)
        page = {
            'name': page_name,
            'url': opened.get('url') or url,
            'elements': [self._element_name(item) for item in elements[:40]],
            'buttons': [self._element_name(item) for item in elements if item.get('kind') == 'button' or item.get('role') in {'button', 'link'}][:30],
            'forms': self._forms_from_elements(elements),
        }
        flow = self._infer_flow(page, goal)
        system_map = {
            'entry': url,
            'domain': urlparse(url).netloc,
            'pages': [{'name': page['name'], 'url': page['url'], 'next': flow}],
            'flows': flow,
        }
        output = {
            'status': browser_result.get('status', 'success'),
            'message': 'Agent Explorer 已完成页面打开、DOM/视觉元素分析、表单识别和系统地图生成',
            'pages': [page],
            'system_map': system_map,
            'raw_browser_result': browser_result,
        }
        if save_memory:
            project = self._project(project_id)
            AgentMemory.objects.create(
                project=project,
                memory_type='system_knowledge',
                role='agent_explorer',
                content=f'{page_name}：{url}',
                payload={
                    'source': 'agent_explorer',
                    'url': url,
                    'pages': output['pages'],
                    'system_map': system_map,
                },
            )
        return output

    def list_memories(self, project_id: int | None = None, memory_type: str = '', query: str = '') -> list[dict[str, Any]]:
        queryset = AgentMemory.objects.all().select_related('project', 'task')
        project = self._project(project_id)
        if project:
            queryset = queryset.filter(project=project)
        elif project_id is not None:
            queryset = queryset.none()
        if memory_type:
            queryset = queryset.filter(memory_type=memory_type)
        if query:
            queryset = queryset.filter(content__icontains=query)
        return [
            {
                'id': item.id,
                'task': item.task_id,
                'project': item.project_id,
                'memory_type': item.memory_type,
                'role': item.role,
                'content': item.content,
                'payload': item.payload,
                'created_at': item.created_at,
            }
            for item in queryset.order_by('-created_at')[:50]
        ]

    def create_memory(self, payload: dict[str, Any]) -> AgentMemory:
        task = payload.get('task')
        project = payload.get('project')
        fields = {
            'project': project if isinstance(project, Project) else self._project(project),
            'memory_type': payload.get('memory_type') or 'system_knowledge',
            'role': payload.get('role') or 'agent',
            'content': payload.get('content') or '',
            'payload': payload.get('payload') or {},
        }
        if isinstance(task, AgentTask):
            fields['task'] = task
        elif task:
            fields['task_id'] = task
        return AgentMemory.objects.create(**fields)

    def upload_knowledge(
        self,
        *,
        title: str,
        content: str,
        document_type: str = 'other',
        project_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentKnowledgeDocument:
        project = self._project(project_id)
        document = AgentKnowledgeDocument.objects.create(
            title=title,
            document_type=document_type,
            project=project,
            content=content,
            metadata={
                'vector_store': 'json-embedding-dev',
                'recommended_store': 'PostgreSQL + pgvector',
                **(metadata or {}),
            },
            uploaded_by=self.user if getattr(self.user, 'is_authenticated', False) else None,
        )
        chunks = self._chunk_text(content)
        for index, chunk in enumerate(chunks):
            AgentKnowledgeChunk.objects.create(
                document=document,
                project=project,
                chunk_index=index,
                content=chunk,
                embedding=self._embedding(chunk),
                tokens=max(1, len(chunk) // 2),
                metadata={'title': title, 'document_type': document_type},
            )
        document.chunks_count = len(chunks)
        document.save(update_fields=['chunks_count', 'updated_at'])
        return document

    def rag_search(self, *, query: str, project_id: int | None = None, limit: int = 5) -> dict[str, Any]:
        project = self._project(project_id)
        queryset = AgentKnowledgeChunk.objects.select_related('document', 'project')
        if project:
            queryset = queryset.filter(project=project)
        elif project_id is not None:
            queryset = queryset.none()
        query_embedding = self._embedding(query)
        hits = []
        for chunk in queryset[:1000]:
            score = self._cosine(query_embedding, chunk.embedding or [])
            keyword_bonus = self._keyword_score(query, chunk.content)
            hits.append((score + keyword_bonus, chunk))
        hits.sort(key=lambda item: item[0], reverse=True)
        results = [
            {
                'score': round(score, 4),
                'document_id': chunk.document_id,
                'document_title': chunk.document.title,
                'document_type': chunk.document.document_type,
                'chunk_id': chunk.id,
                'content': chunk.content,
                'metadata': chunk.metadata,
            }
            for score, chunk in hits[:limit]
        ]
        context = '\n\n'.join(item['content'] for item in results)
        return {
            'status': 'success',
            'query': query,
            'vector_store': 'PostgreSQL + pgvector compatible; current dev fallback uses JSON embeddings',
            'hits': results,
            'context': context,
            'test_strategy': self._strategy_from_context(query, context),
        }

    def generate_assets(
        self,
        *,
        requirement: str,
        project_id: int | None = None,
        base_url: str = '',
        api_doc: Any = None,
        schedule: str = 'daily',
        save_assets: bool = True,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        plan = ReActPlanner().plan(requirement, {'url': base_url, 'api_doc': api_doc or {}})
        scenarios = plan.get('test_plan') or {}
        data_result = DataAgent().generate_from_api(swagger_doc=api_doc or {}, api='', count=3) if api_doc else {
            'status': 'skipped',
            'message': '未提供接口文档，测试数据按场景占位生成',
            'cases': [{'mode': 'normal', 'data': {'username': 'test001', 'password': 'Passw0rd@2026'}}],
        }
        script = self._script_from_plan(requirement, base_url)
        security = SecurityAgent().scan(api_doc=api_doc or '', target='')
        package = {
            'status': 'success',
            'assets': {
                'scenarios': scenarios,
                'test_cases': self._case_titles(scenarios),
                'test_data': data_result,
                'automation_script': script,
                'scheduled_task': {
                    'enabled': True,
                    'cron': self._schedule_to_cron(schedule),
                    'name': f'Agent定时-{requirement[:32]}',
                },
                'security_plan': security,
            },
            'plan': plan,
        }
        if save_assets:
            task = AgentTask.objects.create(
                task_name=f'资产生成-{requirement[:80]}',
                user_requirement=requirement,
                project=project,
                created_by=self._safe_user(),
                status=AgentTask.STATUS_COMPLETED,
                progress=100,
                current_step='完整测试资产已生成',
                plan=plan.get('steps') or [],
                test_plan=scenarios,
                context={'base_url': base_url, 'schedule': schedule},
                started_at=timezone.now(),
                finished_at=timezone.now(),
            )
            created = []
            for asset_type, name, metadata in [
                ('case', 'AI生成测试场景与用例', {'scenarios': scenarios, 'cases': package['assets']['test_cases']}),
                ('data', 'AI生成测试数据', data_result),
                ('ui', 'AI生成UI自动化脚本', {'script': script}),
                ('report', 'AI测试资产包', package['assets']),
            ]:
                asset = TestAsset.objects.create(
                    asset_id=f'{asset_type}_{uuid.uuid4().hex[:12]}',
                    asset_type=asset_type,
                    name=name,
                    task=task,
                    project=project,
                    source_model='backend.agent.capability_service',
                    source_id=str(task.id),
                    metadata=metadata,
                    created_by=self._safe_user(),
                )
                created.append({'asset_id': asset.asset_id, 'asset_type': asset.asset_type, 'name': asset.name})
            data_assets = self._publish_test_data_assets(
                requirement=requirement,
                data_result=data_result,
                project_name=project.name if project else '',
            )
            package['task_id'] = task.id
            package['created_assets'] = created
            package['created_test_data_assets'] = data_assets
        return package

    def run_full_cycle(
        self,
        *,
        requirement: str,
        agent_service: str = 'ui',
        task_name: str = '',
        project_id: int | None = None,
        frontend_url: str = '',
        api_url: str = '',
        api_doc: Any = None,
        api_test_objects: list[dict[str, Any]] | None = None,
        test_object_refs: list[dict[str, Any]] | None = None,
        ui_case_ids: list[int] | None = None,
        ui_test_case_ids: list[int] | None = None,
        ui_case_names: list[str] | None = None,
        ui_test_case_names: list[str] | None = None,
        ui_case_files: list[str] | None = None,
        case_files: list[str] | None = None,
        ui_project_id: str = '',
        app_project: int | None = None,
        app_project_id: int | None = None,
        device: str = '',
        device_id: str = '',
        app_package: int | None = None,
        app_package_id: int | None = None,
        app_test_object_refs: list[dict[str, Any]] | None = None,
        source_entry: str = '',
        reference_module: str = '',
        app_case_ids: list[int] | None = None,
        app_test_case_ids: list[int] | None = None,
        test_data_count: int = 3,
        auto_execute: bool = True,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        inferred_url = self._infer_url(requirement)
        test_data_count = max(1, min(int(test_data_count or 3), 20))
        agent_service = 'app' if agent_service == 'app' else 'ui'
        context = {
            'agent_service': agent_service,
            'require_case_save_confirmation': True,
            'case_save': {'status': 'pending'},
            'url': frontend_url or ('' if api_url else inferred_url),
            'frontend_url': frontend_url or ('' if api_url else inferred_url),
            'api_url': api_url,
            'target': api_url or frontend_url or inferred_url,
            'api_doc': api_doc or {},
            'api_test_objects': api_test_objects or test_object_refs or [],
            'test_object_refs': test_object_refs or api_test_objects or [],
            'ui_case_ids': ui_case_ids or ui_test_case_ids or [],
            'ui_test_case_ids': ui_test_case_ids or ui_case_ids or [],
            'ui_case_names': ui_case_names or ui_test_case_names or [],
            'ui_test_case_names': ui_test_case_names or ui_case_names or [],
            'ui_case_files': ui_case_files or case_files or [],
            'case_files': case_files or ui_case_files or [],
            'ui_project_id': ui_project_id,
            'app_project': app_project or app_project_id,
            'app_project_id': app_project_id or app_project,
            'device': device or device_id,
            'device_id': device_id or device,
            'app_package': app_package or app_package_id,
            'app_package_id': app_package_id or app_package,
            'app_test_object_refs': app_test_object_refs or [],
            'app_case_ids': app_case_ids or app_test_case_ids or [],
            'app_test_case_ids': app_test_case_ids or app_case_ids or [],
            'test_data_count': test_data_count,
            'workflow': 'full_cycle_ai_test_agent',
            'entrypoint': source_entry or 'business_requirement_box',
            'reference_module': reference_module,
            'required_capabilities': [
                'Agent真正决策',
                '浏览器视觉能力',
                '安全测试Agent',
                '智能测试数据生成',
                'AI失败分析',
                'Agent调用全部能力闭环',
            ],
        }
        task = AgentTask.objects.create(
            task_name=task_name or f'全链路AI测试-{requirement[:60]}',
            user_requirement=requirement,
            project=project,
            created_by=self._safe_user(),
            context=context,
        )
        if auto_execute:
            ReActAgentExecutor(task).run()
            task.refresh_from_db()
        result_status = {
            AgentTask.STATUS_FAILED: 'failed',
            AgentTask.STATUS_NEEDS_INPUT: 'needs_input',
        }.get(task.status, 'success')
        result_message = {
            'failed': 'AI Test Agent 执行存在失败，已保留失败证据和分析结果。',
            'needs_input': 'AI Test Agent 已完成现有证据收集，但缺少可执行测试资产，请补充后继续执行。',
        }.get(result_status, 'AI Test Agent 全能力闭环任务已创建并执行。')
        return {
            'status': result_status,
            'message': result_message,
            'task': task,
            'workflow': self._workflow_summary(task),
        }

    def _publish_test_data_assets(
        self,
        *,
        requirement: str,
        data_result: dict[str, Any],
        project_name: str = '',
    ) -> list[dict[str, Any]]:
        """Publish Agent-generated test data into the visible Test Data Center."""
        rows: list[dict[str, Any]] = []
        for item in data_result.get('sample') or []:
            if isinstance(item, dict):
                rows.append(dict(item))
        for item in data_result.get('cases') or []:
            if not isinstance(item, dict):
                continue
            data = item.get('data') if isinstance(item.get('data'), dict) else {}
            if data:
                rows.append(dict(data))
                continue
            parameter = item.get('parameter')
            if parameter:
                rows.append({
                    str(parameter): item.get('value'),
                    'case_type': item.get('case_type') or item.get('mode') or item.get('category') or '',
                    'expected': item.get('expected') or '',
                })
        rows = [row for row in rows if row][:100]
        if not rows:
            return []

        asset = TestDataAsset.objects.create(
            asset_type='API_DATA' if data_result.get('api') else 'CUSTOM',
            name=f'AI生成测试数据-{requirement[:40] or project_name or "Agent"}',
            status=TestDataAsset.STATUS_AVAILABLE,
            payload=rows,
            tags=list(dict.fromkeys([
                'ai-generated',
                'agent-assets',
                'test-data-center',
                *(str(mode) for mode in data_result.get('modes') or []),
            ])),
            created_by=self._safe_user(),
        )
        return [{
            'id': asset.id,
            'asset_type': asset.asset_type,
            'name': asset.name,
            'status': asset.status,
            'count': len(rows),
            'tags': asset.tags,
        }]

    def self_heal(
        self,
        *,
        logs: str = '',
        screenshot: str = '',
        response: dict[str, Any] | None = None,
        old_locator: str = '',
        new_dom: str = '',
        apply: bool = False,
    ) -> dict[str, Any]:
        analysis = FailureAnalyzer().analyze(logs=logs or f'Element not found: {old_locator}', screenshot=screenshot, response=response or {})
        repair = analysis.get('locator_repair') or {}
        if old_locator:
            repair['old_locator'] = old_locator
        if new_dom:
            candidate = self._locator_from_dom(new_dom)
            if candidate:
                repair['new_locator'] = candidate
        return {
            'status': 'success',
            'reason': analysis.get('reason'),
            'error_type': analysis.get('error_type'),
            'old_locator': repair.get('old_locator') or analysis.get('old_locator') or old_locator,
            'new_locator': repair.get('new_locator') or analysis.get('new_locator') or 'button:has-text("提交")',
            'auto_fixable': bool(repair or analysis.get('new_locator')),
            'applied': bool(apply),
            'approval_required': not apply,
            'diff': {
                'before': repair.get('old_locator') or old_locator or '#login',
                'after': repair.get('new_locator') or 'button.submit',
            },
            'suggestion': '页面改版导致旧定位失效，建议更新为更稳定的语义定位并补充 data-testid/accessibility 兜底。',
        }

    def quality_score(self, project_id: int | None = None, project_name: str = '') -> dict[str, Any]:
        project = self._project(project_id)
        tasks = AgentTask.objects.all()
        assets = TestAsset.objects.all()
        results = TestExecutionResult.objects.all()
        memories = AgentMemory.objects.all()
        if project:
            tasks = tasks.filter(project=project)
            assets = assets.filter(project=project)
            results = results.filter(task__project=project)
            memories = memories.filter(project=project)
        api_assets = assets.filter(asset_type='api').count()
        ui_assets = assets.filter(asset_type__in=['ui', 'app']).count()
        case_assets = assets.filter(asset_type='case').count()
        security_findings = sum((asset.metadata.get('summary') or {}).get('confirmed', 0) for asset in assets.filter(asset_type='report')[:50])
        total_results = results.count()
        passed = results.filter(status='PASSED').count()
        failed = results.filter(status='FAILED').count()
        stability = 90 if not total_results else round(max(0, (passed + results.filter(status='SKIPPED').count() * 0.5) / total_results * 100))
        functional = min(95, 45 + case_assets * 8 + memories.filter(memory_type='business_flow').count() * 6)
        api = min(95, 40 + api_assets * 10 + memories.filter(memory_type='api_knowledge').count() * 8)
        security_score = max(40, 92 - security_findings * 8 - failed * 3)
        overall = round(functional * 0.3 + api * 0.25 + security_score * 0.25 + stability * 0.2)
        return {
            'status': 'success',
            'project': project.name if project else project_name or '当前项目',
            'score': overall,
            'dimensions': {
                '功能覆盖': functional,
                '接口覆盖': api,
                '安全风险': security_findings,
                '稳定性': stability,
            },
            'summary': {
                'agent_tasks': tasks.count(),
                'assets': assets.count(),
                'execution_results': total_results,
                'failed_results': failed,
            },
            'recommendations': self._quality_recommendations(functional, api, security_findings, stability),
        }

    def _workflow_summary(self, task: AgentTask) -> dict[str, Any]:
        steps = list(task.steps.order_by('step', 'id'))
        executions = list(task.execution_results.order_by('created_at', 'id'))
        return {
            'task_id': task.id,
            'status': task.status,
            'progress': task.progress,
            'current_step': task.current_step,
            'phases': [
                {
                    'step': step.step,
                    'phase': step.action,
                    'tool': step.tool_name,
                    'status': step.status,
                    'message': step.error_message or (step.output_payload or {}).get('message', ''),
                    'real_execution': (step.output_payload or {}).get('real_execution'),
                }
                for step in steps
            ],
            'execution_evidence': [
                {
                    'module': item.module,
                    'status': item.status,
                    'logs': item.logs,
                    'screenshot': item.screenshot,
                    'report_url': item.report_url,
                }
                for item in executions
            ],
            'failure_analysis': task.failure_analysis,
            'self_heal': (task.context or {}).get('self_heal') or {},
            'quality_report': self._latest_report_summary(task),
        }

    def _latest_report_summary(self, task: AgentTask) -> dict[str, Any]:
        report_asset = task.assets.filter(asset_type='report').order_by('-created_at').first()
        if not report_asset:
            return {}
        return report_asset.metadata or {}

    def _infer_url(self, text: str) -> str:
        return infer_http_url(text)

    def _project(self, project_id: int | None) -> Project | None:
        if isinstance(project_id, Project):
            return project_id
        if not project_id:
            return None
        queryset = Project.objects.filter(id=project_id)
        if getattr(self.user, 'is_authenticated', False):
            queryset = queryset.filter(Q(owner=self.user) | Q(members=self.user)).distinct()
        return queryset.first()

    def _safe_user(self):
        if getattr(self.user, 'is_authenticated', False):
            return self.user
        user_model = get_user_model()
        return user_model.objects.filter(is_superuser=True).first() or user_model.objects.first()

    def _page_name(self, title: str, url: str, elements: list[dict[str, Any]]) -> str:
        text = f'{title} {" ".join(str(item.get("name") or "") for item in elements[:20])}'.lower()
        if any(word in text for word in ['login', '登录', '密码']):
            return '登录页'
        if any(word in text for word in ['cart', '购物车']):
            return '购物车页'
        if any(word in text for word in ['order', '订单']):
            return '订单页'
        parsed = urlparse(url)
        return title or parsed.path.strip('/').replace('-', ' ').title() or '首页'

    def _element_name(self, element: dict[str, Any]) -> str:
        name = str(element.get('name') or '').strip()
        kind = element.get('kind') or element.get('role') or element.get('tag') or '元素'
        if not name:
            name = {'input': '输入框', 'button': '按钮', 'link': '链接', 'select': '下拉框'}.get(kind, '交互元素')
        if kind == 'input' and not name.endswith('输入框'):
            return f'{name}输入框'
        if kind in {'button', 'link'} and not name.endswith(('按钮', '链接')):
            return f'{name}按钮' if kind == 'button' else f'{name}链接'
        return name

    def _forms_from_elements(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        inputs = [self._element_name(item) for item in elements if item.get('kind') == 'input' or item.get('tag') in {'input', 'textarea', 'select'}]
        buttons = [self._element_name(item) for item in elements if item.get('kind') == 'button' or item.get('role') == 'button']
        return [{'name': '主表单', 'fields': inputs, 'submit': buttons[0] if buttons else '提交按钮'}] if inputs else []

    def _infer_flow(self, page: dict[str, Any], goal: str) -> list[str]:
        elements = ''.join(page.get('elements') or [])
        flow = [page['name']]
        if '登录' in elements or 'login' in goal.lower():
            flow += ['认证成功', '首页']
        if any(word in f'{elements}{goal}' for word in ['搜索', '商品', '订单', '购物车', '支付']):
            flow += ['搜索', '购物车', '支付']
        return flow

    def _chunk_text(self, content: str, size: int = 900) -> list[str]:
        paragraphs = [item.strip() for item in re.split(r'\n{2,}', content) if item.strip()]
        chunks: list[str] = []
        current = ''
        for paragraph in paragraphs or [content]:
            if len(current) + len(paragraph) > size and current:
                chunks.append(current)
                current = paragraph
            else:
                current = f'{current}\n{paragraph}'.strip()
        if current:
            chunks.append(current)
        return chunks or [content[:size]]

    def _embedding(self, text: str, dims: int = 64) -> list[float]:
        vector = [0.0] * dims
        tokens = re.findall(r'[\w\u4e00-\u9fff]+', text.lower())
        for token in tokens:
            digest = hashlib.md5(token.encode('utf-8')).hexdigest()
            vector[int(digest[:8], 16) % dims] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 6) for value in vector]

    def _cosine(self, left: Iterable[float], right: Iterable[float]) -> float:
        left_list = list(left)
        right_list = list(right)
        if not left_list or not right_list:
            return 0.0
        size = min(len(left_list), len(right_list))
        return sum(left_list[index] * right_list[index] for index in range(size))

    def _keyword_score(self, query: str, content: str) -> float:
        query_tokens = set(re.findall(r'[\w\u4e00-\u9fff]+', query.lower()))
        content_lower = content.lower()
        return min(0.5, sum(0.08 for token in query_tokens if token and token in content_lower))

    def _strategy_from_context(self, query: str, context: str) -> dict[str, list[str]]:
        seed = query[:40] or '目标功能'
        has_order = any(word in f'{query}{context}' for word in ['订单', '购物车', '支付'])
        return {
            'functional': [f'{seed}主流程验证', '关键状态、数据落库和页面反馈一致'],
            'data': ['基于知识库字段生成正常、边界、异常和安全数据'],
            'automation': ['优先生成 API + UI 组合脚本', '关键按钮使用语义定位和视觉定位双保险'],
            'security': ['覆盖 SQL注入、XSS、越权、JWT、SSRF、文件上传'],
            'flow': ['登录', '搜索', '购物车', '支付'] if has_order else ['进入页面', '提交表单', '校验结果'],
        }

    def _case_titles(self, scenarios: dict[str, Any]) -> list[dict[str, str]]:
        rows = []
        for category, items in scenarios.items():
            for title in (items or [])[:5]:
                rows.append({'category': category, 'title': str(title)})
        return rows

    def _script_from_plan(self, requirement: str, base_url: str) -> dict[str, Any]:
        return {
            'framework': 'playwright',
            'vision_fallback': True,
            'language': 'python',
            'script': [
                f'page.goto("{base_url or "https://example.com"}")',
                'page.get_by_label("用户名").fill(test_data["username"])',
                'page.get_by_label("密码").fill(test_data["password"])',
                'page.get_by_role("button", name=re.compile("登录|提交")).click()',
                f'# goal: {requirement}',
            ],
        }

    def _schedule_to_cron(self, schedule: str) -> str:
        return {'hourly': '0 * * * *', 'weekly': '0 9 * * 1', 'daily': '0 9 * * *'}.get(schedule, schedule or '0 9 * * *')

    def _locator_from_dom(self, dom: str) -> str:
        for pattern in [
            r'data-testid=["\']([^"\']+)["\']',
            r'class=["\']([^"\']*(?:submit|login|primary|button)[^"\']*)["\']',
            r'id=["\']([^"\']+)["\']',
        ]:
            match = re.search(pattern, dom, re.I)
            if match:
                value = match.group(1).split()[0]
                if pattern.startswith('data-testid'):
                    return f'[data-testid="{value}"]'
                if pattern.startswith('class'):
                    return f'.{value}'
                return f'#{value}'
        text = re.search(r'>([^<>]{1,30})<', dom)
        return f'button:has-text("{text.group(1).strip()}")' if text else ''

    def _quality_recommendations(self, functional: int, api: int, security_findings: int, stability: int) -> list[str]:
        items = []
        if functional < 80:
            items.append('补齐核心业务流的场景资产，尤其是登录、搜索、下单、支付等链路。')
        if api < 80:
            items.append('上传 OpenAPI/接口文档并生成接口覆盖矩阵。')
        if security_findings:
            items.append('优先处理已确认安全风险，并将 Payload 固化为回归用例。')
        if stability < 85:
            items.append('启用 Self-Healing Test，降低页面改版导致的定位失败。')
        return items or ['质量基线良好，可继续增加真实执行数据来校准评分。']
