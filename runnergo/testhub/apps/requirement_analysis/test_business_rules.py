from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from apps.requirement_analysis.models import (
    AIModelService,
    BusinessRuleKnowledge,
    BusinessWorkflow,
    KnowledgeNode,
    KnowledgeRelation,
)
from apps.requirement_analysis.rule_services import (
    collect_knowledge_context,
    extract_rule_keywords,
    merge_duplicate_domain_nodes,
    recommend_business_rules,
    score_business_rule,
    sync_rule_to_knowledge_node,
)
from apps.projects.models import Project
from apps.requirement_analysis.serializers import TestCaseGenerationRequestSerializer
from apps.requirement_analysis.views import _current_rule_save_context, _normalize_workflow_definition


def make_rule(**overrides):
    values = {
        'id': 1,
        'title': '贷款年龄限制',
        'content': '申请人年龄必须在22至60岁之间',
        'keywords': ['贷款', '年龄限制'],
        'module': '额度评估',
        'applicable_conditions': '个人贷款申请',
        'expected_behavior': '超出范围时拒绝提交',
        'exceptions': '',
        'project_id': 10,
        'usage_count': 5,
        'accepted_count': 4,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class BusinessRuleRecommendationTests(SimpleTestCase):
    def test_same_project_and_semantic_terms_rank_first(self):
        matching = make_rule()
        unrelated = make_rule(
            id=2,
            title='登录验证码',
            content='验证码60秒后允许重新发送',
            keywords=['验证码'],
            module='登录',
            project_id=10,
        )

        results = recommend_business_rules(
            [unrelated, matching],
            '汽车贷款额度评估时校验申请人年龄范围',
            project_id=10,
            module='额度评估',
        )

        self.assertEqual(results[0][0].id, matching.id)
        self.assertGreater(results[0][1], results[1][1])

    def test_project_match_increases_score(self):
        same_project = score_business_rule(make_rule(project_id=10), '贷款年龄校验', project_id=10)
        other_project = score_business_rule(make_rule(project_id=20), '贷款年龄校验', project_id=10)
        self.assertGreater(same_project, other_project)

    def test_extract_keywords_removes_duplicates(self):
        self.assertEqual(
            extract_rule_keywords('年龄限制、贷款额度、年龄限制'),
            ['年龄限制', '贷款额度'],
        )


class GenerationRuleSerializerTests(SimpleTestCase):
    def test_focus_rule_is_kept_when_quota_is_disabled(self):
        serializer = TestCaseGenerationRequestSerializer(data={
            'title': '贷款申请',
            'requirement_text': '验证贷款申请',
            'case_type_rules': {
                'enabled': False,
                'focus_keywords': '申请人年龄必须在22至60岁之间',
            },
            'selected_rule_ids': [3, 3, 4],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data['case_type_rules']['focus_keywords'],
            '申请人年龄必须在22至60岁之间',
        )
        self.assertEqual(serializer.validated_data['selected_rule_ids'], [3, 4])

    def test_all_selected_rule_ids_are_accepted(self):
        selected_rule_ids = list(range(1, 51))
        serializer = TestCaseGenerationRequestSerializer(data={
            'title': '全量业务规则',
            'requirement_text': '验证全量业务规则',
            'selected_rule_ids': selected_rule_ids,
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['selected_rule_ids'], selected_rule_ids)

    def test_boundary_and_equivalence_types_are_supported(self):
        serializer = TestCaseGenerationRequestSerializer(data={
            'title': '移动端贷款申请',
            'requirement_text': '验证年龄边界和手机系统兼容性',
            'case_type_rules': {
                'enabled': True,
                'focused_types': ['boundary', 'equivalence', 'compatibility'],
                'focused_count': 6,
            },
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data['case_type_rules']['focused_types'],
            ['boundary', 'equivalence', 'compatibility'],
        )

    def test_current_rule_requires_content_and_parent_node_before_save(self):
        content, parent, should_save = _current_rule_save_context({
            'save_current_rule': True,
            'current_rule_content': '仅本次生成使用的规则',
            'current_rule_parent_node': None,
        })

        self.assertEqual(content, '仅本次生成使用的规则')
        self.assertIsNone(parent)
        self.assertFalse(should_save)

        _content, _parent, should_save = _current_rule_save_context({
            'save_current_rule': True,
            'current_rule_content': '',
            'current_rule_parent_node': object(),
        })
        self.assertFalse(should_save)

        _content, _parent, should_save = _current_rule_save_context({
            'save_current_rule': True,
            'current_rule_content': '保存到指定节点的规则',
            'current_rule_parent_node': object(),
        })
        self.assertTrue(should_save)


class BusinessRuleBatchDeleteTests(APITestCase):
    url = '/api/requirement-analysis/business-rules/batch-delete/'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='business-rule-tester',
            password='test-password',
        )
        self.client.force_authenticate(self.user)

    def create_rule(self, title, status='approved'):
        return BusinessRuleKnowledge.objects.create(
            title=title,
            content=f'{title}的规则内容',
            status=status,
            created_by=self.user,
        )

    def test_batch_delete_removes_rules_and_reports_missing_ids(self):
        approved_rule = self.create_rule('已审核规则')
        draft_rule = self.create_rule('草稿规则', status='draft')
        sync_rule_to_knowledge_node(approved_rule)
        sync_rule_to_knowledge_node(draft_rule)
        missing_id = max(approved_rule.id, draft_rule.id) + 1000

        response = self.client.post(self.url, {
            'ids': [approved_rule.id, str(draft_rule.id), approved_rule.id, missing_id],
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['deleted'], 2)
        self.assertEqual(response.data['missing_ids'], [missing_id])
        self.assertFalse(BusinessRuleKnowledge.objects.filter(id=approved_rule.id).exists())
        self.assertFalse(BusinessRuleKnowledge.objects.filter(id=draft_rule.id).exists())
        self.assertFalse(KnowledgeNode.objects.filter(rule_id__in=[approved_rule.id, draft_rule.id]).exists())

    def test_batch_delete_removes_rules_already_deprecated(self):
        deprecated_rule = self.create_rule('已废弃规则', status='deprecated')

        response = self.client.post(self.url, {'ids': [deprecated_rule.id]}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['deleted'], 1)
        self.assertFalse(BusinessRuleKnowledge.objects.filter(id=deprecated_rule.id).exists())

    def test_batch_delete_requires_valid_ids(self):
        response = self.client.post(self.url, {'ids': ['invalid']}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['message'], '请提供有效的业务规则 ID')


class KnowledgeGraphTests(APITestCase):
    graph_url = '/api/knowledge/graph/'
    node_url = '/api/knowledge/nodes/'
    relation_url = '/api/knowledge/relations/'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='knowledge-graph-tester',
            password='test-password',
        )
        self.client.force_authenticate(self.user)

    def test_graph_endpoint_returns_nodes_and_edges_shape(self):
        source = KnowledgeNode.objects.create(name='贷款申请', type='entity', content='贷款申请主流程')
        target = KnowledgeNode.objects.create(name='年龄规则', type='rule', content='申请人年龄为22至60岁')
        KnowledgeRelation.objects.create(source=source, target=target, relation_type='constrains')

        response = self.client.get(self.graph_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['nodes'][0].keys() >= {'id', 'name', 'type'}, True)
        self.assertEqual(response.data['edges'][0]['source'], source.id)
        self.assertEqual(response.data['edges'][0]['target'], target.id)
        self.assertEqual(response.data['edges'][0]['relation'], 'constrains')

    def test_graph_endpoint_includes_associated_business_rule_details(self):
        rule = BusinessRuleKnowledge.objects.create(
            title='绑卡校验规则',
            content='绑卡前必须完成身份校验',
            business_domain='国信',
            module='绑卡',
            entity_name='银行卡',
            attribute_name='签约状态',
            state_values=['未签约', '已签约'],
            rule_type='business',
            applicable_conditions='用户已登录且已提交绑卡信息',
            expected_behavior='校验通过后进入签约页面',
            exceptions='校验服务超时时提示重试',
            source_requirement='绑卡需求-001',
            risk_level='high',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name='绑卡校验规则',
            type='rule',
            content=rule.content,
            rule=rule,
        )

        response = self.client.get(self.graph_url)

        self.assertEqual(response.status_code, 200)
        details = next(item['rule_details'] for item in response.data['nodes'] if item['id'] == node.id)
        self.assertEqual(details['title'], rule.title)
        self.assertEqual(details['applicable_conditions'], rule.applicable_conditions)
        self.assertEqual(details['expected_behavior'], rule.expected_behavior)
        self.assertEqual(details['exceptions'], rule.exceptions)
        self.assertEqual(details['state_values'], rule.state_values)

    def test_business_rule_patch_updates_expected_behavior(self):
        rule = BusinessRuleKnowledge.objects.create(
            title='婚姻关系规则',
            content='借款人婚姻关系和联系人关系需要匹配',
            expected_behavior='保存原预期行为',
            status='approved',
            created_by=self.user,
        )
        KnowledgeNode.objects.create(
            name=rule.title,
            type='rule',
            content=rule.content,
            rule=rule,
        )

        response = self.client.patch(
            f'/api/requirement-analysis/business-rules/{rule.id}/',
            {'expected_behavior': '婚姻关系为已婚时，联系人关系必须为配偶'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.expected_behavior, '婚姻关系为已婚时，联系人关系必须为配偶')

    def test_business_rule_patch_keeps_existing_knowledge_file(self):
        parent = KnowledgeNode.objects.create(name='身份认证', type='domain')
        rule = BusinessRuleKnowledge.objects.create(
            title='旧规则名称',
            content='旧规则内容',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name=rule.title,
            type='rule',
            content=rule.content,
            rule=rule,
        )
        node_id = node.id
        KnowledgeRelation.objects.create(source=parent, target=node, relation_type='contains')

        response = self.client.patch(
            f'/api/requirement-analysis/business-rules/{rule.id}/',
            {'title': '新规则名称', 'content': '新规则内容'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 1)
        node.refresh_from_db()
        self.assertEqual(node.id, node_id)
        self.assertEqual(node.name, '新规则内容')
        self.assertEqual(node.content, '新规则内容')
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=parent,
            target=node,
            relation_type='contains',
        ).exists())

    def test_rule_node_patch_updates_linked_business_rule(self):
        parent = KnowledgeNode.objects.create(name='资料校验', type='domain')
        rule = BusinessRuleKnowledge.objects.create(
            title='资料规则',
            content='旧节点内容',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name='资料规则',
            type='rule',
            content=rule.content,
            rule=rule,
        )
        KnowledgeRelation.objects.create(source=parent, target=node, relation_type='contains')

        response = self.client.patch(
            f'{self.node_url}{node.id}/',
            {'name': '更新后的节点', 'content': '更新后的节点内容'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        node.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual(node.name, '更新后的节点')
        self.assertEqual(node.content, '更新后的节点内容')
        self.assertEqual(rule.title, '更新后的节点')
        self.assertEqual(rule.content, '更新后的节点内容')
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 1)
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=parent,
            target=node,
            relation_type='contains',
        ).exists())

    def test_node_editor_metadata_update_does_not_create_knowledge_file(self):
        """Updating expected behavior keeps the existing node/root topology."""
        rule = BusinessRuleKnowledge.objects.create(
            title='无根规则',
            content='原始规则内容',
            expected_behavior='原始预期',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name='无根规则',
            type='rule',
            content=rule.content,
            rule=rule,
        )
        node_id = node.id

        response = self.client.patch(
            f'/api/requirement-analysis/business-rules/{rule.id}/',
            {'expected_behavior': '更新后的预期'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        rule.refresh_from_db()
        node.refresh_from_db()
        self.assertEqual(rule.expected_behavior, '更新后的预期')
        self.assertEqual(node.id, node_id)
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 0)

    def test_node_editor_update_keeps_current_file_when_rule_has_no_relation(self):
        """A node content edit must update in place even if legacy data lacks an edge."""
        rule = BusinessRuleKnowledge.objects.create(
            title='历史规则',
            content='旧内容',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name='历史规则',
            type='rule',
            content='旧内容',
            rule=rule,
        )
        node_id = node.id

        response = self.client.patch(
            f'{self.node_url}{node.id}/',
            {'name': '更新规则', 'content': '新内容'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        node.refresh_from_db()
        self.assertEqual(node.id, node_id)
        self.assertEqual(node.name, '更新规则')
        self.assertEqual(node.content, '新内容')
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 0)

    def test_child_rule_node_creates_business_rule_with_expected_behavior(self):
        parent = KnowledgeNode.objects.create(name='身份认证', type='domain')

        response = self.client.post(self.node_url, {
            'name': '身份证图片校验',
            'type': 'rule',
            'content': '校验身份证图片格式、大小和有效期',
            'expected_behavior': '仅允许合规图片，校验失败时阻止提交',
        }, format='json')

        self.assertEqual(response.status_code, 201)
        node = KnowledgeNode.objects.get(id=response.data['id'])
        self.assertIsNotNone(node.rule_id)
        self.assertEqual(node.rule.expected_behavior, '仅允许合规图片，校验失败时阻止提交')

        relation_response = self.client.post(self.relation_url, {
            'source': parent.id,
            'target': node.id,
            'relation_type': 'contains',
        }, format='json')

        self.assertEqual(relation_response.status_code, 201)
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=parent,
            target=node,
            relation_type='contains',
        ).exists())

    def test_domain_with_children_cannot_be_changed_to_rule(self):
        parent = KnowledgeNode.objects.create(name='不可变知识文件', type='domain')
        child = KnowledgeNode.objects.create(name='子规则', type='rule', content='规则内容')
        KnowledgeRelation.objects.create(source=parent, target=child, relation_type='contains')

        response = self.client.patch(
            f'{self.node_url}{parent.id}/',
            {'type': 'rule'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('不能修改', str(response.data))
        parent.refresh_from_db()
        self.assertEqual(parent.type, 'domain')

    def test_delete_rule_node_removes_its_rule_and_relations(self):
        parent = KnowledgeNode.objects.create(name='身份认证', type='domain')
        rule = BusinessRuleKnowledge.objects.create(
            title='身份证图片校验',
            content='校验身份证图片',
            expected_behavior='拒绝不合规图片',
            status='approved',
            created_by=self.user,
        )
        node = KnowledgeNode.objects.create(
            name=rule.title,
            type='rule',
            content=rule.content,
            rule=rule,
        )
        KnowledgeRelation.objects.create(source=parent, target=node, relation_type='contains')

        response = self.client.delete(f'{self.node_url}{node.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(KnowledgeNode.objects.filter(id=node.id).exists())
        self.assertFalse(BusinessRuleKnowledge.objects.filter(id=rule.id).exists())
        self.assertFalse(KnowledgeRelation.objects.filter(target_id=node.id).exists())

    def test_business_rule_search_by_knowledge_file_name_returns_descendant_rules(self):
        knowledge_file = KnowledgeNode.objects.create(name='国信', type='domain')
        rule = BusinessRuleKnowledge.objects.create(
            title='绑卡规则',
            content='绑卡前必须完成身份校验',
            status='approved',
            created_by=self.user,
        )
        detail_node = KnowledgeNode.objects.create(
            name='绑卡规则详情',
            type='rule',
            rule=rule,
        )
        KnowledgeRelation.objects.create(
            source=knowledge_file,
            target=detail_node,
            relation_type='contains',
        )

        response = self.client.get(
            '/api/requirement-analysis/business-rules/',
            {'status': 'approved', 'knowledge_file_name': '国信'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data['results']], [rule.id])

    def test_project_scoped_search_keeps_global_knowledge_rules(self):
        project = Project.objects.create(name='霖华项目', owner=self.user)
        knowledge_file = KnowledgeNode.objects.create(name='全局登录规则', type='domain')
        rule = BusinessRuleKnowledge.objects.create(
            title='全局登录规则',
            content='登录失败后锁定账号',
            status='approved',
            created_by=self.user,
        )
        detail_node = KnowledgeNode.objects.create(
            name='登录失败锁定',
            type='rule',
            rule=rule,
        )
        KnowledgeRelation.objects.create(
            source=knowledge_file,
            target=detail_node,
            relation_type='contains',
        )

        response = self.client.get(
            '/api/requirement-analysis/business-rules/',
            {
                'status': 'approved',
                'project': project.id,
                'knowledge_file_id': knowledge_file.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data['results']], [rule.id])

        update_response = self.client.patch(
            f'/api/requirement-analysis/business-rules/{rule.id}/',
            {'content': '登录失败后锁定账号并提示剩余等待时间'},
            format='json',
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 1)
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=knowledge_file,
            target=detail_node,
            relation_type='contains',
        ).exists())

    def test_project_scoped_search_includes_global_rules_under_project_file(self):
        """A project file may contain reusable global rules."""
        project = Project.objects.create(name='霖华项目文件', owner=self.user)
        knowledge_file = KnowledgeNode.objects.create(
            name='霖华',
            type='domain',
            project=project,
        )
        rule = BusinessRuleKnowledge.objects.create(
            title='全局影像刷新规则',
            content='上传影像资料后刷新页面，已上传内容仍需保留',
            status='approved',
            created_by=self.user,
        )
        detail_node = KnowledgeNode.objects.create(
            name='影像刷新',
            type='rule',
            content=rule.content,
            rule=rule,
        )
        KnowledgeRelation.objects.create(
            source=knowledge_file,
            target=detail_node,
            relation_type='contains',
        )

        response = self.client.get(
            '/api/requirement-analysis/business-rules/',
            {
                'status': 'approved',
                'project': project.id,
                'knowledge_file_id': knowledge_file.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data['results']], [rule.id])

        update_response = self.client.patch(
            f'/api/requirement-analysis/business-rules/{rule.id}/',
            {'content': '上传影像后刷新页面且保留已上传内容'},
            format='json',
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(KnowledgeNode.objects.filter(type='domain').count(), 1)
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=knowledge_file,
            target=detail_node,
            relation_type='contains',
        ).exists())

    def test_legacy_orphan_rule_node_is_backfilled_for_search(self):
        project = Project.objects.create(name='历史知识项目', owner=self.user)
        knowledge_file = KnowledgeNode.objects.create(
            name='历史文件',
            type='domain',
            project=project,
        )
        orphan_node = KnowledgeNode.objects.create(
            name='历史规则节点',
            type='rule',
            content='历史规则内容',
            project=project,
        )
        KnowledgeRelation.objects.create(
            source=knowledge_file,
            target=orphan_node,
            relation_type='contains',
        )

        from django.apps import apps as django_apps
        from importlib import import_module

        migration = import_module(
            'apps.requirement_analysis.migrations.0029_backfill_orphan_knowledge_rules'
        )
        migration.backfill_orphan_knowledge_rules(django_apps, None)
        orphan_node.refresh_from_db()

        self.assertIsNotNone(orphan_node.rule_id)
        response = self.client.get(
            '/api/requirement-analysis/business-rules/',
            {
                'status': 'approved',
                'project': project.id,
                'knowledge_file_id': knowledge_file.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data['results']], [orphan_node.rule_id])

    def test_relation_serializer_rejects_self_loop(self):
        node = KnowledgeNode.objects.create(name='同一节点', type='rule')

        response = self.client.post(self.relation_url, {
            'source': node.id,
            'target': node.id,
            'relation_type': 'related',
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('知识关系不能指向自身', str(response.data))

    def test_relation_delete_removes_edge_but_keeps_nodes(self):
        source = KnowledgeNode.objects.create(name='来源节点', type='entity')
        target = KnowledgeNode.objects.create(name='目标节点', type='rule')
        relation = KnowledgeRelation.objects.create(
            source=source,
            target=target,
            relation_type='related',
        )

        response = self.client.delete(f'{self.relation_url}{relation.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertEqual(KnowledgeNode.objects.filter(id__in=[source.id, target.id]).count(), 2)
        self.assertFalse(KnowledgeRelation.objects.filter(id=relation.id).exists())

    def test_batch_delete_removes_selected_files_and_descendants(self):
        selected_root = KnowledgeNode.objects.create(name='待删除文件', type='domain')
        selected_child = KnowledgeNode.objects.create(name='待删除规则', type='rule')
        selected_grandchild = KnowledgeNode.objects.create(name='待删除状态', type='state')
        retained_root = KnowledgeNode.objects.create(name='保留文件', type='domain')
        retained_child = KnowledgeNode.objects.create(name='保留规则', type='rule')
        KnowledgeRelation.objects.create(source=selected_root, target=selected_child, relation_type='contains')
        KnowledgeRelation.objects.create(source=selected_child, target=selected_grandchild, relation_type='related')
        KnowledgeRelation.objects.create(source=retained_root, target=retained_child, relation_type='contains')

        response = self.client.post(
            f'{self.node_url}batch-delete/',
            {'ids': [selected_root.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['deleted'], 1)
        self.assertEqual(response.data['deleted_nodes'], 3)
        self.assertFalse(KnowledgeNode.objects.filter(id__in=[
            selected_root.id, selected_child.id, selected_grandchild.id,
        ]).exists())
        self.assertEqual(KnowledgeNode.objects.filter(id__in=[
            retained_root.id, retained_child.id,
        ]).count(), 2)
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=retained_root,
            target=retained_child,
            relation_type='contains',
        ).exists())

    def test_rule_save_creates_topology_node(self):
        response = self.client.post('/api/requirement-analysis/business-rules/', {
            'title': '车抵贷押品规则',
            'content': '车辆必须完成抵押登记后才允许放款',
            'rule_type': 'business',
            'status': 'approved',
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(KnowledgeNode.objects.filter(
            rule_id=response.data['id'],
            type='rule',
        ).exists())
        root = KnowledgeNode.objects.get(name='车抵贷押品规则', type='domain')
        child = KnowledgeNode.objects.get(rule_id=response.data['id'])
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=root,
            target=child,
            relation_type='contains',
        ).exists())

    def test_same_name_domain_nodes_are_merged(self):
        first_root = KnowledgeNode.objects.create(name='车企贷', type='domain')
        second_root = KnowledgeNode.objects.create(name='车企贷', type='domain')
        first_rule = KnowledgeNode.objects.create(name='授信额度规则', type='rule')
        second_rule = KnowledgeNode.objects.create(name='绑卡规则', type='rule')
        KnowledgeRelation.objects.create(source=first_root, target=first_rule, relation_type='contains')
        KnowledgeRelation.objects.create(source=second_root, target=second_rule, relation_type='contains')

        merged_count = merge_duplicate_domain_nodes(name='车企贷')

        self.assertEqual(merged_count, 1)
        self.assertEqual(KnowledgeNode.objects.filter(name='车企贷', type='domain').count(), 1)
        root = KnowledgeNode.objects.get(name='车企贷', type='domain')
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=root,
            target=first_rule,
            relation_type='contains',
        ).exists())
        self.assertTrue(KnowledgeRelation.objects.filter(
            source=root,
            target=second_rule,
            relation_type='contains',
        ).exists())

    def test_recursive_context_and_prompt_include_upstream_downstream_relation(self):
        selected = KnowledgeNode.objects.create(name='车企贷规则', type='rule', content='借款人婚姻和联系人关系需要校验')
        upstream = KnowledgeNode.objects.create(name='联系人关系', type='entity', content='联系人必须与借款人存在有效关系')
        KnowledgeRelation.objects.create(source=upstream, target=selected, relation_type='depends_on')

        context = collect_knowledge_context([selected.id], depth=2)

        self.assertEqual({item['name'] for item in context}, {'车企贷规则', '联系人关系'})
        instruction = AIModelService.get_knowledge_rule_instruction(SimpleNamespace(
            case_type_rules={},
            knowledge_rule_context=[{
                'title': selected.name,
                'content': selected.content,
                'knowledge_neighbors': context,
            }],
        ))
        self.assertIn('联系人关系 -[depends_on]-> 车企贷规则', instruction)


class BusinessWorkflowTests(APITestCase):
    workflow_url = '/api/requirement-analysis/workflows/'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='workflow-tester',
            password='test-password',
        )
        self.client.force_authenticate(self.user)

    def test_workflow_endpoint_accepts_json_definition(self):
        response = self.client.post(self.workflow_url, {
            'name': '授信审批流程',
            'identifier': 'credit-approval',
            'definition': {
                'nodes': [
                    {'id': 'start', 'type': 'start', 'label': '开始', 'x': 80, 'y': 80},
                    {'id': 'end', 'type': 'end', 'label': '结束', 'x': 280, 'y': 80},
                ],
                'edges': [
                    {'id': 'edge-1', 'source': 'start', 'target': 'end', 'label': '审批完成'},
                ],
            },
            'status': 'active',
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['node_count'], 2)
        self.assertEqual(response.data['edge_count'], 1)
        self.assertTrue(BusinessWorkflow.objects.filter(identifier='credit-approval').exists())

    def test_import_text_workflow_builds_nodes_and_edges(self):
        upload = SimpleUploadedFile(
            'loan-flow.txt',
            '提交申请\n资料审核\n审批通过\n放款'.encode('utf-8'),
            content_type='text/plain',
        )

        response = self.client.post(
            f'{self.workflow_url}import/',
            {'file': upload, 'name': '贷款流程', 'identifier': 'loan-flow'},
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['name'], '贷款流程')
        self.assertEqual(response.data['node_count'], 4)
        self.assertEqual(response.data['edge_count'], 3)

    @mock.patch('apps.requirement_analysis.views._parse_workflow_image_import')
    def test_import_image_workflow_uses_image_parser(self, parse_image):
        parse_image.return_value = ({
            'nodes': [
                {'id': 'start', 'type': 'start', 'label': '开始', 'x': 80, 'y': 80},
                {'id': 'initial-review', 'type': 'task', 'label': '初审', 'x': 240, 'y': 80},
                {'id': 'gateway-1', 'type': 'gateway', 'label': '初审结果', 'x': 420, 'y': 80},
                {'id': 'reject', 'type': 'end', 'label': '拒绝', 'x': 580, 'y': 80},
            ],
            'edges': [
                {'id': 'edge-1', 'source': 'start', 'target': 'initial-review', 'label': ''},
                {'id': 'edge-2', 'source': 'initial-review', 'target': 'gateway-1', 'label': ''},
                {'id': 'edge-3', 'source': 'gateway-1', 'target': 'reject', 'label': '拒绝'},
            ],
            'viewport': {'scale': 1, 'offsetX': 0, 'offsetY': 0},
        }, 'AI 识别：初审 -> 拒绝')
        upload = SimpleUploadedFile(
            'approval-flow.png',
            b'\x89PNG\r\n\x1a\nworkflow-image',
            content_type='image/png',
        )

        response = self.client.post(
            f'{self.workflow_url}import/',
            {'file': upload, 'name': '审批流程', 'identifier': 'approval-flow'},
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['name'], '审批流程')
        self.assertEqual(response.data['node_count'], 4)
        self.assertEqual(response.data['edge_count'], 3)
        self.assertEqual(response.data['source_text'], 'AI 识别：初审 -> 拒绝')
        parse_image.assert_called_once()

    def test_image_workflow_normalization_marks_bpmn_layout(self):
        definition = _normalize_workflow_definition({
            'nodes': [
                {'id': 'start', 'type': 'start', 'label': '开始', 'x': 440, 'y': 0},
                {'id': 'review', 'type': 'task', 'label': '初审', 'x': 440, 'y': 110},
                {'id': 'gateway', 'type': 'gateway', 'label': '审核结果', 'x': 440, 'y': 220},
                {'id': 'end', 'type': 'end', 'label': '结束', 'x': 620, 'y': 220},
            ],
            'edges': [
                {'id': 'edge-1', 'source': 'start', 'target': 'review'},
                {'id': 'edge-2', 'source': 'review', 'target': 'gateway'},
                {'id': 'edge-3', 'source': 'gateway', 'target': 'end', 'label': '通过'},
            ],
        }, '图片流程', fit_image_layout=True)

        self.assertEqual(definition['viewport']['layout_source'], 'image_coordinates')
        self.assertEqual(definition['viewport']['layout_mode'], 'bpmn')
        self.assertEqual(definition['viewport']['scale'], 0.75)
        self.assertEqual(min(node['x'] for node in definition['nodes']), 96)
        self.assertEqual(min(node['y'] for node in definition['nodes']), 72)

    @mock.patch('apps.requirement_analysis.views.DocumentProcessor.extract_text_from_image')
    @mock.patch('apps.requirement_analysis.views._active_workflow_vision_config', return_value=None)
    def test_import_image_rejects_low_quality_ocr(self, active_config, extract_text):
        extract_text.return_value = 'J >\nHe\nica\na\nSG\nenna z'
        upload = SimpleUploadedFile(
            'bad-ocr-flow.png',
            b'\x89PNG\r\n\x1a\nworkflow-image',
            content_type='image/png',
        )

        response = self.client.post(
            f'{self.workflow_url}import/',
            {'file': upload, 'name': '坏 OCR 流程', 'identifier': 'bad-ocr-flow'},
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('OCR 结果质量不足', response.data['message'])
        self.assertFalse(BusinessWorkflow.objects.filter(identifier='bad-ocr-flow').exists())
