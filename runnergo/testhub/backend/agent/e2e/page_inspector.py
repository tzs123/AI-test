from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from django.conf import settings

from apps.core.outbound import validate_outbound_http_url


class PageInspector:
    """Collect a bounded, read-only DOM map for model-grounded planning."""

    def inspect(self, *, target_url: str, description: str, max_pages: int = 5) -> list[dict[str, Any]]:
        urls = self._candidate_urls(target_url, description)[:max_pages]
        if not urls:
            return []

        from playwright.sync_api import sync_playwright

        snapshots = []
        playwright = browser = context = None
        try:
            playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {'headless': True}
            channel = str(getattr(settings, 'E2E_BROWSER_CHANNEL', '') or '').strip()
            executable = str(getattr(settings, 'E2E_BROWSER_EXECUTABLE_PATH', '') or '').strip()
            if channel:
                launch_options['channel'] = channel
            elif executable:
                launch_options['executable_path'] = executable
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                viewport={'width': 1440, 'height': 900},
                ignore_https_errors=bool(getattr(settings, 'E2E_IGNORE_HTTPS_ERRORS', False)),
            )
            timeout_ms = min(
                int(getattr(settings, 'E2E_PAGE_INSPECTION_TIMEOUT_MS', 10000)),
                30000,
            )
            context.set_default_timeout(timeout_ms)
            context.set_default_navigation_timeout(timeout_ms)
            page = context.new_page()
            script_urls: list[str] = []
            for url in urls:
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                    page.wait_for_timeout(300)
                    snapshot = page.evaluate(_DOM_SNAPSHOT_SCRIPT)
                    if not script_urls:
                        script_urls = list(page.evaluate(
                            "performance.getEntriesByType('resource')"
                            ".map(item => item.name).filter(url => /\\.js(?:\\?|$)/.test(url))"
                        ) or [])[:30]
                    snapshots.append({
                        'requested_url': url,
                        'url': page.url,
                        'title': page.title(),
                        'status_code': response.status if response else None,
                        'text': str(snapshot.get('text') or '')[:6000],
                        'elements': list(snapshot.get('elements') or [])[:150],
                    })
                except Exception as exc:
                    snapshots.append({'requested_url': url, 'error': str(exc)[:500]})
            self._append_matching_routes(
                context=context,
                page=page,
                base_url=target_url,
                description=description,
                script_urls=script_urls,
                snapshots=snapshots,
                timeout_ms=timeout_ms,
                max_pages=max_pages,
            )
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()
            if playwright is not None:
                playwright.stop()
        return snapshots

    def _append_matching_routes(
        self,
        *,
        context: Any,
        page: Any,
        base_url: str,
        description: str,
        script_urls: list[str],
        snapshots: list[dict[str, Any]],
        timeout_ms: int,
        max_pages: int,
    ) -> None:
        text_points = [
            item.strip()
            for item in re.findall(r'【([^】]+)】', str(description or ''))
            if item.strip() and not re.search(r'[/:]', item)
        ]
        observed = ' '.join(str(item.get('text') or '') for item in snapshots)
        unresolved = [point for point in text_points if point not in observed]
        if not unresolved or len(snapshots) >= max_pages:
            return

        base_origin = f'{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}'
        route_paths = []
        for script_url in script_urls:
            if not script_url.startswith(base_origin):
                continue
            try:
                response = context.request.get(script_url, timeout=timeout_ms)
                if not response.ok:
                    continue
                source = response.body()[:3_000_000].decode('utf-8', errors='ignore')
            except Exception:
                continue
            for path in re.findall(r'\bpath\s*:\s*["\'](/[^"\']+)["\']', source):
                if (
                    path in {'/', '/:pathMatch(.*)*'}
                    or ':' in path
                    or path.startswith(('/api/', '/assets/'))
                    or '.' in path.rsplit('/', 1)[-1]
                ):
                    continue
                if path not in route_paths:
                    route_paths.append(path)

        known_urls = {str(item.get('requested_url') or '') for item in snapshots}
        for path in route_paths[:30]:
            if len(snapshots) >= max_pages or not unresolved:
                break
            candidate = validate_outbound_http_url(urljoin(base_origin, path), label='E2E 测试地址')
            if candidate in known_urls:
                continue
            try:
                response = page.goto(candidate, wait_until='domcontentloaded', timeout=min(timeout_ms, 5000))
                page.wait_for_timeout(200)
                snapshot = page.evaluate(_DOM_SNAPSHOT_SCRIPT)
                page_text = str(snapshot.get('text') or '')
            except Exception:
                continue
            matched = [point for point in unresolved if point in page_text]
            if not matched:
                continue
            snapshots.append({
                'requested_url': candidate,
                'url': page.url,
                'title': page.title(),
                'status_code': response.status if response else None,
                'text': page_text[:6000],
                'elements': list(snapshot.get('elements') or [])[:150],
                'matched_test_points': matched,
            })
            known_urls.add(candidate)
            unresolved = [point for point in unresolved if point not in matched]

    def _candidate_urls(self, target_url: str, description: str) -> list[str]:
        base_url = validate_outbound_http_url(target_url, label='E2E 测试地址')
        parsed_base = urlsplit(base_url)
        candidates = [base_url]
        raw_points = re.findall(r'【([^】]+)】', str(description or ''))
        raw_points.extend(re.findall(r'https?://[^\s，。；;】]+', str(description or '')))
        for raw in raw_points:
            value = str(raw or '').strip().rstrip(').]，。；;')
            if not value:
                continue
            if re.match(r'^https?://', value, flags=re.I):
                candidate = value
            elif re.match(r'^[A-Za-z0-9.-]+(?::\d+)?/', value):
                candidate = f'{parsed_base.scheme}://{value}'
            elif value.startswith('/'):
                candidate = urljoin(base_url, value)
            else:
                continue
            candidate = validate_outbound_http_url(candidate, label='E2E 测试地址')
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates


_DOM_SNAPSHOT_SCRIPT = """() => {
  const visible = element => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  };
  const clean = value => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
  const elements = Array.from(document.querySelectorAll(
    'input,textarea,button,a,select,[role],[aria-label],[contenteditable="true"],li'
  )).filter(visible).slice(0, 150).map(element => {
    const labels = element.labels ? Array.from(element.labels).map(item => item.innerText).join(' ') : '';
    return {
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role') || '',
      text: clean(element.innerText || element.textContent),
      name: clean(element.getAttribute('name')),
      label: clean(labels || element.getAttribute('aria-label')),
      placeholder: clean(element.getAttribute('placeholder')),
      type: clean(element.getAttribute('type')),
      test_id: clean(element.getAttribute('data-testid')),
      id: clean(element.id),
    };
  });
  const text = String(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 6000);
  return {text, elements};
}"""
