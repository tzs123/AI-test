from django.test import SimpleTestCase

from apps.core.runtime_case import (
    RuntimeCase,
    inject_runtime_assertions,
    public_input_fields,
    resolve_dynamic_value,
)
from apps.core.runtime_orchestration import RuntimeContext


class RuntimeCaseTests(SimpleTestCase):
    @staticmethod
    def sample_flow():
        return {
            'steps': [
                {
                    'id': 'phone-step',
                    'type': 'smart_input',
                    'name': '输入手机号',
                    'config': {
                        'element_id': 214,
                        'value': '13800000000',
                    },
                },
                {
                    'id': 'custom-step',
                    'type': 'recorded_group',
                    'kind': 'custom',
                    'steps': [
                        {
                            'id': 'code-step',
                            'type': 'send_keys',
                            'name': '输入验证码',
                            'config': {'value': '123456'},
                        },
                    ],
                },
            ],
        }

    def test_scans_config_values_and_nested_aliases(self):
        fields = public_input_fields(self.sample_flow())

        self.assertEqual([item['stepId'] for item in fields], ['phone-step', 'code-step'])
        self.assertEqual(fields[0]['element'], '元素#214')
        self.assertEqual(fields[1]['stepPath'], 'steps.1.steps.0')

    def test_scan_prefers_recorded_element_fingerprint_over_coordinates(self):
        fields = public_input_fields({'steps': [{
            'id': 'recorded-input',
            'type': 'input',
            'name': 'iOS 录制输入 52',
            'config': {
                'selector_type': 'pos',
                'selector': '266,541',
                'value': '19921437750',
                'fingerprint': {
                    'resource_id': '联系人手机号',
                    'text': '请填写联系人手机号',
                },
            },
        }]})

        self.assertEqual(fields[0]['element'], '联系人手机号')

    def test_runtime_case_merges_without_mutating_model_payload(self):
        source = self.sample_flow()
        runtime = RuntimeCase(source, [{
            'stepId': 'phone-step',
            'stepPath': 'steps.0',
            'runtimeValue': '${uuid}',
        }]).build()

        self.assertEqual(source['steps'][0]['config']['value'], '13800000000')
        final_value = runtime.case_data['steps'][0]['config']['value']
        self.assertEqual(len(final_value), 36)
        self.assertEqual(runtime.execution_data[0]['finalValue'], final_value)
        self.assertEqual(runtime.execution_data[0]['source'], 'runtimeOverride')

    def test_dynamic_examples_are_supported(self):
        value = resolve_dynamic_value(
            '${timestamp}|${uuid}|${random_phone}|${random_string}'
        )
        timestamp, uuid_value, phone, random_text = value.split('|')
        self.assertTrue(timestamp.isdigit())
        self.assertEqual(len(uuid_value), 36)
        self.assertEqual(len(phone), 11)
        self.assertEqual(len(random_text), 8)

    def test_injects_data_driven_assertion_after_submit_step(self):
        source = self.sample_flow()
        source['runtime_assertions'] = [{
            'afterStepId': 'phone-step',
            'type': 'assert_text',
            'expected': '${data.expected_submit_result}',
            'name': '校验提交结果',
            'config': {'selector_type': 'region', 'selector': [0, 0, 1, 1]},
        }]

        runtime = RuntimeCase(source, [{
            'stepId': 'phone-step',
            'runtimeValue': '${data.phone}',
        }]).build(context=RuntimeContext({
            'data': {
                'phone': '1234567',
                'expected_submit_result': '手机号长度错误',
            },
        }))

        steps = runtime.case_data['steps']
        self.assertEqual(steps[0]['id'], 'phone-step')
        self.assertEqual(steps[1]['type'], 'assert_text')
        self.assertEqual(steps[1]['expected'], '手机号长度错误')
        self.assertEqual(steps[1]['config']['expected'], '手机号长度错误')
        self.assertTrue(steps[1]['runtime_generated_assertion'])
        self.assertEqual(steps[2]['id'], 'custom-step')
        self.assertEqual(source['steps'][1]['id'], 'custom-step')

    def test_rejects_runtime_assertion_with_missing_target_step(self):
        source = self.sample_flow()
        source['runtime_assertions'] = [{
            'afterStepId': 'missing-step',
            'type': 'assert_text',
            'expected': '${data.result}',
        }]

        with self.assertRaisesRegex(ValueError, '找不到插入步骤'):
            inject_runtime_assertions(source)
