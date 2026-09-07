from __future__ import annotations

import re
from typing import Any


class DataGeneratorAgent:
    """Generate normal, abnormal and boundary data from an E2E scenario."""

    def generate(self, *, description: str, scenario: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        text = f"{description} {(scenario or {}).get('name', '')}".lower()
        fields = self._fields(text)
        normal = self._normal(fields)
        abnormal = self._abnormal(fields)
        boundary = self._boundary(fields)
        return {'normal': [normal], 'abnormal': abnormal, 'boundary': boundary}

    def _fields(self, text: str) -> list[str]:
        fields: list[str] = []
        mapping = [
            (('登录', 'login', '账号', '用户'), ['username', 'password']),
            (('注册', 'register'), ['username', 'password', 'email', 'phone']),
            (('搜索', 'search', '商品', '商城', '购买', '下单'), ['keyword']),
            (('下单', '订单', '购买', 'checkout'), ['recipient', 'phone', 'address']),
            (('支付', 'payment', '付款'), ['payment_method', 'amount']),
            (('邮箱', 'email'), ['email']),
            (('手机号', '手机', 'phone'), ['phone']),
            (('详细地址', '地址', 'address'), ['address']),
            (('单位名称', 'companyname'), ['companyName']),
            (('年收入', 'yearpackage'), ['yearPackage']),
            (('联系人姓名', 'contactname'), ['contactName']),
            (('联系人手机号', 'contactmobile'), ['contactMobile']),
            (('借款人姓名', 'borrower_name'), ['borrower_name']),
            (('身份证号', 'id_number'), ['id_number']),
        ]
        for hints, names in mapping:
            if any(hint in text for hint in hints):
                fields.extend(names)
        return list(dict.fromkeys(fields or ['input']))

    def _normal(self, fields: list[str]) -> dict[str, Any]:
        values = {
            'username': 'test001',
            'password': 'Passw0rd@2026',
            'email': 'test001@example.test',
            'phone': '13800138000',
            'keyword': '测试商品',
            'recipient': '测试用户',
            'address': '测试地址 1 号',
            'payment_method': 'default',
            'amount': '0.01',
            'input': 'test-data',
            'companyName': '测试科技有限公司',
            'yearPackage': '20',
            'contactName': '测试联系人',
            'contactMobile': '13900139000',
            'borrower_name': '测试用户',
            'id_number': '110101199001010015',
        }
        return {field: values[field] for field in fields}

    def _abnormal(self, fields: list[str]) -> list[dict[str, Any]]:
        rows = []
        for field in fields:
            rows.append({field: '', '_case': f'{field} 为空'})
        if 'password' in fields:
            rows.append({'username': 'test001', 'password': '123', '_case': '密码格式错误'})
        if 'phone' in fields:
            rows.append({'phone': '123', '_case': '手机号格式错误'})
        if 'amount' in fields:
            rows.extend([
                {'amount': '-0.01', '_case': '负数金额'},
                {'amount': 'not-a-number', '_case': '金额格式错误'},
            ])
        return rows[:20]

    def _boundary(self, fields: list[str]) -> list[dict[str, Any]]:
        rows = []
        for field in fields:
            if field == 'amount':
                rows.extend([
                    {'amount': '0.00', '_case': '零金额'},
                    {'amount': '99999999.99', '_case': '最大金额'},
                ])
            else:
                rows.extend([
                    {field: 'a', '_case': f'{field} 最小长度'},
                    {field: self._long_value(field), '_case': f'{field} 超长'},
                ])
        return rows[:20]

    def _long_value(self, field: str) -> str:
        prefix = re.sub(r'[^a-z0-9]', '', field.lower()) or 'value'
        return (prefix + '_') * 128
