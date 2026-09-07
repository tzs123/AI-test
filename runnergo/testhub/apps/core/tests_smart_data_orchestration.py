from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import TestDataAsset, TestDataDecisionLog
from .runtime_orchestration import RuntimeContext
from .smart_data_orchestration import (
    analyze_data_requirements,
    prepare_smart_test_data,
    repair_failed_execution_data,
)


class SmartDataOrchestrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='smart-user', password='pass')

    @staticmethod
    def sample_flow():
        return {
            'steps': [
                {
                    'id': 'phone-step',
                    'type': 'smart_input',
                    'name': '输入手机号',
                    'config': {'value': ''},
                },
                {
                    'id': 'code-step',
                    'type': 'send_keys',
                    'name': '输入验证码',
                    'config': {'value': ''},
                },
            ],
        }

    def test_analyze_data_requirements_recognizes_fields_and_dependencies(self):
        requirements = analyze_data_requirements(self.sample_flow())

        self.assertEqual([item['fieldType'] for item in requirements], ['phone', 'verification_code'])
        self.assertEqual(requirements[1]['dependencies'], ['phone'])

    def test_analyze_data_requirements_distinguishes_address_and_income(self):
        requirements = analyze_data_requirements({
            'steps': [
                {
                    'id': 'recorded-input-a',
                    'type': 'input',
                    'name': 'iOS 录制输入 21',
                    'config': {
                        'value': '',
                        'selector': '244,382',
                        'fingerprint': {'resource_id': '单位地址'},
                    },
                },
                {
                    'id': 'recorded-input-b',
                    'type': 'input',
                    'name': 'iOS 录制输入 22',
                    'config': {
                        'value': '',
                        'selector': '261,383',
                        'fingerprint': {'resource_id': '年收入'},
                    },
                },
                {
                    'id': 'website-step',
                    'type': 'input',
                    'name': '公司网址',
                    'config': {'value': ''},
                },
            ],
        })

        self.assertEqual(
            [item['fieldType'] for item in requirements],
            ['address', 'amount', 'url'],
        )

    def test_prepare_uses_asset_pool_before_rule_generation(self):
        asset = TestDataAsset.objects.create(
            asset_type='PHONE',
            name='phone-asset',
            payload={'phone': '13800138000'},
            created_by=self.user,
        )

        context, overrides, logs, leases = prepare_smart_test_data(
            target_type='app_automation',
            case_id=1001,
            execution_type='app_automation',
            execution_id=9001,
            user=self.user,
            runtime_context={},
            runtime_override=[],
            case_payload=self.sample_flow(),
            case_name='登录',
        )

        asset.refresh_from_db()
        self.assertEqual(asset.status, TestDataAsset.STATUS_LOCKED)
        self.assertEqual(len(leases), 1)
        self.assertEqual(context['smartData']['fields']['phone']['value'], '13800138000')
        self.assertEqual(context['smartData']['fields']['verification_code']['source'], 'rule_generation')
        self.assertEqual(RuntimeContext(context).resolve(overrides[0]['runtimeValue']), '13800138000')
        self.assertEqual({log.source for log in logs}, {'asset_pool', 'rule_generation'})

    def test_prepare_does_not_replace_explicit_runtime_override(self):
        context, overrides, logs, leases = prepare_smart_test_data(
            target_type='app_automation',
            case_id=1002,
            execution_type='app_automation',
            execution_id=9002,
            user=self.user,
            runtime_context={},
            runtime_override=[{
                'stepId': 'phone-step',
                'stepPath': 'steps.0',
                'runtimeValue': '13900139000',
            }],
            case_payload=self.sample_flow(),
            case_name='登录',
        )

        self.assertEqual(overrides[-1]['runtimeValue'], '13900139000')
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(leases), 0)
        skipped = [item for item in context['smartData']['decisions'] if item.get('reason') == 'runtime_override_exists']
        self.assertEqual(len(skipped), 1)

    def test_repair_marks_asset_decision_and_next_prepare_avoids_asset(self):
        asset = TestDataAsset.objects.create(
            asset_type='PHONE',
            name='bad-phone',
            payload={'phone': '13800138001'},
            created_by=self.user,
        )
        context, _, logs, leases = prepare_smart_test_data(
            target_type='app_automation',
            case_id=1003,
            execution_type='app_automation',
            execution_id=9003,
            user=self.user,
            runtime_context={},
            runtime_override=[],
            case_payload={'steps': [self.sample_flow()['steps'][0]]},
            case_name='登录',
        )
        self.assertEqual(context['smartData']['fields']['phone']['value'], '13800138001')
        asset.status = TestDataAsset.STATUS_AVAILABLE
        asset.locked_by = None
        asset.locked_at = None
        asset.save(update_fields=['status', 'locked_by', 'locked_at', 'updated_at'])

        result = repair_failed_execution_data(
            execution_type='app_automation',
            execution_id=9003,
            failure_info={'result': 'failed'},
        )
        logs[0].refresh_from_db()

        self.assertEqual(len(result['repaired']), 1)
        self.assertEqual(logs[0].status, TestDataDecisionLog.STATUS_REPAIRED)

        next_context, _, _, next_leases = prepare_smart_test_data(
            target_type='app_automation',
            case_id=1003,
            execution_type='app_automation',
            execution_id=9004,
            user=self.user,
            runtime_context={},
            runtime_override=[],
            case_payload={'steps': [self.sample_flow()['steps'][0]]},
            case_name='登录',
        )

        self.assertEqual(next_leases, [])
        self.assertNotEqual(next_context['smartData']['fields']['phone']['value'], '13800138001')
        self.assertEqual(next_context['smartData']['fields']['phone']['source'], 'rule_generation')
