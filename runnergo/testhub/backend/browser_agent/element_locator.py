from __future__ import annotations

from typing import Any


class ElementLocator:
    """Semantic locator helper. Avoids fixed XPath."""

    def locate(self, page: Any, element: dict):
        name = element.get('name') or ''
        role = element.get('role') or ''
        candidates = []
        if role in {'button', 'link'} and name:
            candidates.append(lambda: page.get_by_role(role, name=name).first())
        if name:
            candidates.extend([
                lambda: page.get_by_label(name).first(),
                lambda: page.get_by_placeholder(name).first(),
                lambda: page.get_by_text(name, exact=True).first(),
            ])
        for factory in candidates:
            try:
                locator = factory()
                if locator.count() > 0:
                    return locator
            except Exception:
                continue
        return None
