from django.test import SimpleTestCase

from apps.core.runtime_orchestration import (
    RuntimeContext,
    build_runtime_override_from_row,
    perform_runtime_cleanup,
)


class RuntimeOrchestrationTests(SimpleTestCase):
    def test_resolves_dynamic_dot_syntax_and_context_values(self):
        context = RuntimeContext({'local': {'token': 'abc123'}})
        value = context.resolve('${phone.random}|${random_phone}|${name.random}|${uuid}|${time.now+7d}|${token}')
        phone, random_phone, name, uuid_value, future_time, token = value.split('|')

        self.assertEqual(len(phone), 11)
        self.assertTrue(phone.startswith('1'))
        self.assertEqual(len(random_phone), 11)
        self.assertTrue(random_phone.startswith('1'))
        self.assertGreaterEqual(len(name), 2)
        self.assertEqual(len(uuid_value), 36)
        self.assertIn('T', future_time)
        self.assertEqual(token, 'abc123')

    def test_build_runtime_override_from_dataset_row(self):
        fields = [
            {'stepId': 'phone-step', 'stepPath': 'steps.0', 'step': '输入手机号', 'element': '手机号'},
            {'stepId': 'name-step', 'stepPath': 'steps.1', 'step': '输入姓名', 'element': '姓名'},
        ]
        row = {
            'phone-step': '${phone.random}',
            '姓名': '张三',
        }

        overrides = build_runtime_override_from_row(fields, row)

        self.assertEqual(overrides, [
            {'stepId': 'phone-step', 'stepPath': 'steps.0', 'runtimeValue': '${phone.random}'},
            {'stepId': 'name-step', 'stepPath': 'steps.1', 'runtimeValue': '张三'},
        ])

    def test_perform_runtime_cleanup_clears_only_runtime_context(self):
        context = {
            'local': {'token': 'abc123', 'keep': 'yes'},
            'data': {'phone': '13800000000'},
            'outputs': {'last': {'id': 1}},
        }

        cleaned, result = perform_runtime_cleanup(context, {
            'clearKeys': ['token', 'data.phone'],
        })

        self.assertNotIn('token', cleaned['local'])
        self.assertNotIn('phone', cleaned['data'])
        self.assertEqual(cleaned['local']['keep'], 'yes')
        self.assertEqual(len(result['cleared']), 2)
