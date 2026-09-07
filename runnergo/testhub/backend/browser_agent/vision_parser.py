from __future__ import annotations


class VisionParser:
    """Maps DOM/accessibility signals into semantic UI elements."""

    def parse(self, elements: list[dict], goal: str = '') -> list[dict]:
        goal_lower = str(goal or '').lower()
        parsed = []
        for item in elements:
            name = str(item.get('name') or item.get('placeholder') or item.get('text') or '')
            role = str(item.get('role') or item.get('tag') or '')
            lowered = name.lower()
            kind = 'click'
            if role in {'input', 'textbox', 'searchbox'} or any(word in lowered for word in ['用户名', '账号', '密码', 'email', 'phone', '搜索']):
                kind = 'input'
            if any(word in lowered for word in ['登录', 'login', 'submit', '确定', '保存', '加入购物车', '购物车']):
                kind = 'click'
            parsed.append({**item, 'name': name, 'role': role, 'kind': kind, 'score': self._score(name, goal_lower)})
        return sorted(parsed, key=lambda row: row.get('score', 0), reverse=True)

    def _score(self, name: str, goal_lower: str) -> int:
        score = 1
        lowered = name.lower()
        for token in ['login', '登录', '用户名', '账号', '密码', '购物车', 'cart', '提交', '搜索']:
            if token in lowered or token in goal_lower:
                score += 2
        return score
