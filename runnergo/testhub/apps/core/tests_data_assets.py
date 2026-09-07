from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .ai_test_data import generate_ai_test_data
from .data_assets import (
    create_or_update_asset_with_bindings,
    release_assets_for_execution,
    request_assets_for_case,
)
from .models import TestDataAsset, TestDataAssetLease, TestDataAssetRequirement
from .runtime_orchestration import RuntimeContext
from .serializers import TestDataAssetSerializer
from apps.app_automation.models import AppProject, AppTestCase


class TestDataAssetPoolTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='asset-user', password='pass')

    def test_request_asset_locks_and_injects_runtime_context(self):
        asset = TestDataAsset.objects.create(
            asset_type='USER',
            name='vip-user',
            status=TestDataAsset.STATUS_AVAILABLE,
            payload={'phone': '13800138000', 'level': 'vip'},
            tags=['smoke'],
            created_by=self.user,
        )
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=1001,
            target_case_name='登录',
            alias='buyer',
            asset_type='USER',
            filters={'level': 'vip'},
            tags=['smoke'],
            created_by=self.user,
        )

        context, leases = request_assets_for_case(
            'app_automation',
            1001,
            'app_automation',
            9001,
            user=self.user,
            runtime_context={},
            case_name='登录',
        )

        asset.refresh_from_db()
        self.assertEqual(asset.status, TestDataAsset.STATUS_LOCKED)
        self.assertEqual(len(leases), 1)
        self.assertEqual(context['dataAssets']['buyer']['phone'], '13800138000')
        self.assertEqual(context['data']['phone'], '13800138000')
        self.assertEqual(RuntimeContext(context).resolve('${buyer.phone}'), '13800138000')
        self.assertEqual(RuntimeContext(context).resolve('${data.phone}'), '13800138000')
        self.assertEqual(RuntimeContext(context).resolve('${dataAssets.buyer.level}'), 'vip')

    def test_requirement_can_bind_exactly_one_asset(self):
        selected = TestDataAsset.objects.create(
            asset_type='USER',
            name='selected-user',
            payload={'phone': '13800138001'},
            status=TestDataAsset.STATUS_AVAILABLE,
            created_by=self.user,
        )
        other = TestDataAsset.objects.create(
            asset_type='USER',
            name='other-user',
            payload={'phone': '13800138002'},
            status=TestDataAsset.STATUS_AVAILABLE,
            created_by=self.user,
        )
        TestDataAssetRequirement.objects.create(
            target_type='api_automation',
            target_case_id=1004,
            alias='buyer',
            asset_type='USER',
            source_asset=selected,
        )

        context, leases = request_assets_for_case(
            'api_automation', 1004, 'api_automation', 'exact-asset-run', user=self.user,
        )

        self.assertEqual(context['dataAssets']['buyer']['phone'], '13800138001')
        self.assertEqual([lease.asset_id for lease in leases], [selected.id])
        other.refresh_from_db()
        self.assertEqual(other.status, TestDataAsset.STATUS_AVAILABLE)

    def test_asset_serializer_accepts_parameterized_object_rows(self):
        serializer = TestDataAssetSerializer(data={
            'asset_type': 'USER',
            'name': 'verification-codes',
            'status': 'AVAILABLE',
            'payload': [
                {'code': '122222'},
                {'code': '122'},
                {'code': '123456'},
            ],
            'tags': [],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(len(serializer.validated_data['payload']), 3)

    def test_asset_serializer_rejects_non_object_parameter_rows(self):
        serializer = TestDataAssetSerializer(data={
            'asset_type': 'USER',
            'name': 'invalid-codes',
            'status': 'AVAILABLE',
            'payload': [{'code': '122222'}, '123456'],
            'tags': [],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('payload', serializer.errors)

    def test_parameterized_asset_rows_are_injected_in_order(self):
        asset = TestDataAsset.objects.create(
            asset_type='CUSTOM',
            name='验证码参数',
            payload=[{'code': '111111'}, {'code': '222222'}],
            status=TestDataAsset.STATUS_AVAILABLE,
            created_by=self.user,
        )
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=1999,
            alias='loginData',
            asset_type='CUSTOM',
            source_asset=asset,
            release_policy='release',
            created_by=self.user,
        )

        first_context, _ = request_assets_for_case(
            'app_automation', 1999, 'app_automation', 'round-1', user=self.user,
        )
        release_assets_for_execution('app_automation', 'round-1')
        second_context, _ = request_assets_for_case(
            'app_automation', 1999, 'app_automation', 'round-2', user=self.user,
        )

        self.assertEqual(first_context['dataAssets']['loginData']['code'], '111111')
        self.assertEqual(second_context['dataAssets']['loginData']['code'], '222222')

    def test_save_and_bind_creates_asset_and_case_binding_in_one_request(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post('/api/core/test-data-assets/save-and-bind/', {
            'asset': {
                'asset_type': 'USER',
                'name': '登录用户',
                'status': 'AVAILABLE',
                'payload': {'phone': '13800138000'},
                'tags': ['login'],
            },
            'binding': {
                'target_type': 'ui_automation',
                'target_case_id': 2002,
                'target_case_name': '登录页',
                'alias': 'loginData',
                'asset_type': 'USER',
                'quantity': 1,
                'release_policy': 'release',
                'filters': {},
                'tags': [],
                'is_active': True,
            },
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        asset = TestDataAsset.objects.get(id=response.data['asset']['id'])
        requirement = TestDataAssetRequirement.objects.get(id=response.data['binding']['id'])
        self.assertEqual(requirement.source_asset_id, asset.id)
        self.assertEqual(requirement.target_case_id, 2002)
        self.assertEqual(response.data['asset']['bindings'][0]['alias'], 'loginData')

    def test_save_and_bind_rolls_back_asset_when_binding_is_invalid(self):
        initial_count = TestDataAsset.objects.count()

        with self.assertRaisesMessage(ValueError, 'target_type'):
            create_or_update_asset_with_bindings(
                asset_values={
                    'asset_type': 'CUSTOM',
                    'name': '不应保留的数据',
                    'payload': {'value': 1},
                },
                bindings=[{
                    'target_type': 'invalid',
                    'target_case_id': 1,
                    'alias': 'invalidData',
                    'release_policy': 'release',
                }],
                user=self.user,
            )

        self.assertEqual(TestDataAsset.objects.count(), initial_count)

    def test_update_and_bind_replaces_asset_binding_atomically(self):
        asset, requirements = create_or_update_asset_with_bindings(
            asset_values={
                'asset_type': 'CUSTOM',
                'name': '旧资产名称',
                'payload': {'value': 'old'},
            },
            bindings=[{
                'target_type': 'api_automation',
                'target_case_id': 1001,
                'target_case_name': '旧接口',
                'alias': 'oldData',
                'release_policy': 'release',
            }],
            user=self.user,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.patch(
            f'/api/core/test-data-assets/{asset.id}/save-and-bind/',
            {
                'asset': {'name': '新资产名称', 'payload': {'value': 'new'}},
                'binding': {
                    'id': requirements[0].id,
                    'target_type': 'app_automation',
                    'target_case_id': 3003,
                    'target_case_name': 'APP登录',
                    'alias': 'appLoginData',
                    'asset_type': 'CUSTOM',
                    'quantity': 1,
                    'release_policy': 'release',
                    'filters': {},
                    'tags': [],
                    'is_active': True,
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        asset.refresh_from_db()
        requirements[0].refresh_from_db()
        self.assertEqual(asset.name, '新资产名称')
        self.assertEqual(requirements[0].target_type, 'app_automation')
        self.assertEqual(requirements[0].target_case_id, 3003)
        self.assertEqual(requirements[0].source_asset_id, asset.id)
        self.assertEqual(TestDataAssetRequirement.objects.filter(source_asset=asset).count(), 1)

    def test_release_policy_release_returns_asset_available(self):
        asset = TestDataAsset.objects.create(asset_type='PHONE', name='phone-1', payload={'value': '13800138001'})
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=1002,
            alias='phone',
            asset_type='PHONE',
            release_policy='release',
        )
        request_assets_for_case('app_automation', 1002, 'app_automation', 9002, user=self.user)

        result = release_assets_for_execution('app_automation', 9002)

        asset.refresh_from_db()
        lease = TestDataAssetLease.objects.get(execution_id='9002')
        self.assertEqual(asset.status, TestDataAsset.STATUS_AVAILABLE)
        self.assertEqual(lease.status, TestDataAssetLease.STATUS_RELEASED)
        self.assertEqual(result['released'], [asset.id])

    def test_release_policy_mark_used(self):
        asset = TestDataAsset.objects.create(asset_type='ORDER', name='order-1', payload={'id': 1})
        TestDataAssetRequirement.objects.create(
            target_type='api_automation',
            target_case_id=1003,
            alias='order',
            asset_type='ORDER',
            release_policy='mark_used',
        )
        request_assets_for_case('api_automation', 1003, 'api_automation', 'api-run-1', user=self.user)

        release_assets_for_execution('api_automation', 'api-run-1')

        asset.refresh_from_db()
        lease = TestDataAssetLease.objects.get(execution_id='api-run-1')
        self.assertEqual(asset.status, TestDataAsset.STATUS_USED)
        self.assertEqual(lease.status, TestDataAssetLease.STATUS_USED)

    def test_ai_generator_creates_registration_normal_abnormal_and_injection_cases(self):
        result = generate_ai_test_data('用户注册', count=1, include_security=True)

        titles = {item['title'] for item in result['cases']}
        self.assertGreaterEqual(result['summary']['normal'], 1)
        self.assertIn('空手机号', titles)
        self.assertIn('非法身份证', titles)
        self.assertTrue(any(item['type'] == 'injection' for item in result['cases']))
        normal_case = next(item for item in result['cases'] if item['type'] == 'normal')
        self.assertIn('phone', normal_case['payload'])
        self.assertIn('name', normal_case['payload'])

    def test_bulk_delete_assets_uses_one_request_and_removes_bindings(self):
        assets = [
            TestDataAsset.objects.create(
                asset_type='USER',
                name=f'user-{index}',
                payload={'index': index},
                created_by=self.user,
            )
            for index in range(3)
        ]
        TestDataAssetRequirement.objects.create(
            target_type='app_automation',
            target_case_id=1004,
            alias='users',
            asset_type='USER',
            source_asset=assets[0],
            created_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post(
            '/api/core/test-data-assets/bulk-delete/',
            {'ids': [asset.id for asset in assets]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(response.data['deleted_ids'], [asset.id for asset in assets])
        self.assertFalse(TestDataAsset.objects.filter(id__in=[asset.id for asset in assets]).exists())
        self.assertFalse(TestDataAssetRequirement.objects.filter(target_case_id=1004).exists())

    def test_bulk_delete_assets_rejects_non_integer_ids(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post(
            '/api/core/test-data-assets/bulk-delete/',
            {'ids': ['invalid']},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['message'], '数据资产ID必须是整数数组')

    def test_ai_generate_api_can_save_assets_and_requirement_for_auto_injection(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post('/api/core/test-data-assets/ai-generate/', {
            'interface_name': '用户注册',
            'count': 1,
            'save_to_assets': True,
            'auto_create_requirement': True,
            'target_type': 'api_automation',
            'target_case_id': 2001,
            'alias': 'registerData',
            'batch_tag': 'ai-generated-test-batch',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data['created_assets']), 0)
        self.assertEqual(response.data['created_requirement']['alias'], 'registerData')
        self.assertTrue(any(
            'ai-generated-test-batch' in asset.get('tags', [])
            for asset in response.data['created_assets']
        ))
        requirement = TestDataAssetRequirement.objects.get(target_case_id=2001, alias='registerData')
        self.assertEqual(requirement.tags, ['ai-generated-test-batch'])

    def test_ai_analyze_case_reuses_app_case_fields_and_builds_data_chain(self):
        project = AppProject.objects.create(name='数据编排项目', owner=self.user)
        app_case = AppTestCase.objects.create(
            project=project,
            name='购物下单支付流程',
            ui_flow={'steps': [
                {'id': 'phone-step', 'type': 'input', 'name': '输入手机号', 'config': {'value': ''}},
                {'id': 'amount-step', 'type': 'input', 'name': '输入订单金额', 'config': {'value': ''}},
            ]},
            created_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(user=self.user)

        response = client.post('/api/core/test-data-assets/ai-analyze-case/', {
            'target_type': 'app_automation',
            'target_case_id': app_case.id,
            'goal': '测试购物流程，需要1000个用户、5000订单并完成支付',
            'count': 1,
            'save_to_assets': True,
            'auto_create_requirement': True,
            'alias': 'shoppingData',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['target']['targetType'], 'app_automation')
        self.assertIn('phone', [item['type'] for item in response.data['generation']['fields']])
        self.assertTrue(response.data['dataChain']['edges'])
        self.assertEqual(response.data['agentPlan']['detectedDemands']['users'], 1000)
        self.assertEqual(response.data['agentPlan']['detectedDemands']['orders'], 5000)
        self.assertGreater(len(response.data['created_assets']), 0)
        self.assertEqual(response.data['created_requirement']['alias'], 'shoppingData')
