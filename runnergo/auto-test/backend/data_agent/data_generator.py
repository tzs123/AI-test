"""正常测试数据生成器：根据参数 Schema 生成合法数据。"""
from __future__ import annotations

import random
import string
import time
from typing import Any, Optional

_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
_GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "霞", "平", "刚", "文", "辉", "力"]
_EMAIL_DOMAINS = ["example.com", "test.cn", "runnergo.dev", "mail.test"]
_PRODUCTS = ["手机", "笔记本电脑", "蓝牙耳机", "运动鞋", "保温杯", "键盘", "显示器", "背包", "连衣裙", "扫地机器人"]


def _now_minute_seed() -> int:
    return int(time.time()) // 60


def _rng(seed: int = 0) -> random.Random:
    return random.Random(seed or _now_minute_seed())


def _validators() -> dict[str, list[Any]]:
    return {
        "username": ["user_001", "test_login", "runnergo_2026", "qa_tester", "tester_zhang"],
        "phone": ["13800138000", "13912345678", "15800001111", "17766668888", "18612345678"],
        "email": ["test001@example.com", "user_002@test.cn", "qa@runnergo.dev", "demo@mail.test"],
        "password": ["Passw0rd@2026", "Qwerty!234", "Aa123456#", "Test@run123", "Strong#Pass1"],
        "name": ["张三", "李四", "王小明", "赵丽", "test_user"],
        "code": ["100000", "200001", "abc123", "XYZ789", "000111"],
    }


def generate_normal_row(param: dict, index: int, rng: Optional[random.Random] = None) -> Any:
    """为单个参数生成一条合法数据。"""
    rng = rng or _rng()
    name = str(param.get("name") or "").lower()
    ptype = str(param.get("type") or "").lower()
    pformat = str(param.get("format") or "").lower()
    schema = param.get("schema") or {}
    enum = schema.get("enum") or param.get("enum") or []
    if enum:
        return rng.choice(list(enum))
    default = schema.get("default")
    if default is not None:
        return default
    example = schema.get("example") or param.get("example")
    if example is not None:
        return example

    validators = _validators()
    for key, values in validators.items():
        if key in name and name.endswith(key):
            return values[index % len(values)]
    if "email" in name:
        return f"user_{index:06d}@{rng.choice(_EMAIL_DOMAINS)}"
    if "phone" in name or "mobile" in name:
        return f"1{rng.choice('35789')}{rng.randint(100000000, 999999999)}"
    if "password" in name or "pwd" in name:
        return f"Passw0rd@{rng.randint(1000, 9999)}"
    if "username" in name or "account" in name or "login" in name:
        return f"user_{index:06d}"
    if ptype in {"integer", "number"} or pformat in {"int32", "int64", "float", "double"}:
        if "age" in name:
            return rng.randint(18, 60)
        if "count" in name or "num" in name:
            return rng.randint(1, 100)
        if "amount" in name or "price" in name:
            return round(rng.uniform(1, 9999), 2)
        return rng.randint(1, 99999)
    if ptype == "boolean":
        return rng.choice([True, False])
    if ptype == "array":
        return [rng.choice(_PRODUCTS) for _ in range(rng.randint(1, 3))]
    if pformat in {"date", "date-time"} or "date" in name or "time" in name:
        return f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    if "name" in name:
        return rng.choice(_SURNAMES) + rng.choice(_GIVEN)
    if "url" in name or "link" in name:
        return f"https://test.example.com/item/{index}"
    if "code" in name or "token" in name:
        return f"code_{index:06d}"
    if "search" in name or "keyword" in name or "query" in name:
        return rng.choice(_PRODUCTS)
    if "id" == name or name.endswith("_id") or name.endswith("id"):
        return index
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and max_length > 0:
        return "".join(rng.choices(string.ascii_lowercase + string.digits, k=max_length))
    return f"value_{index:06d}"


def generate_normal_data(parameters: list[dict], count: int = 100) -> list[dict]:
    """生成 count 条正常请求数据（每条覆盖全部参数）。"""
    count = max(1, min(int(count or 100), 10000))
    rng = _rng()
    rows = []
    for index in range(1, count + 1):
        row = {}
        for param in parameters:
            row[param["name"]] = generate_normal_row(param, index, rng)
        rows.append(row)
    return rows
