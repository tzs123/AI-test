from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from django.conf import settings

logger = logging.getLogger(__name__)

_ERROR_PATTERNS = (
    re.compile(r'\b(500|502|503|504)\b'),
    re.compile(r'page\s+not\s+found|internal\s+server\s+error|service\s+unavailable', re.I),
    re.compile(r'页面不存在|系统异常|服务器错误|服务不可用|应用无响应|已停止运行'),
)


class VisionAgent:
    """Fuse DOM/accessibility and screenshot OCR into a stable vision contract."""

    def __init__(
        self,
        *,
        ocr_factory: Callable[..., Any] | None = None,
        applitools: 'ApplitoolsAdapter | None' = None,
        min_confidence: float = 0.45,
    ):
        self.ocr_factory = ocr_factory or _default_ocr_factory
        self.applitools = applitools or ApplitoolsAdapter()
        self.min_confidence = min(max(float(min_confidence), 0.0), 1.0)

    def analyze(
        self,
        screenshot: str | os.PathLike[str],
        *,
        query: str = '',
        dom_elements: list[dict[str, Any]] | None = None,
        use_applitools: bool = False,
        checkpoint_name: str = 'E2E checkpoint',
    ) -> dict[str, Any]:
        path = Path(screenshot)
        if not path.is_file():
            raise FileNotFoundError(f'截图不存在: {path}')
        image_signals = self._image_signals(path)
        dom_candidates = self._dom_candidates(dom_elements or [], query)
        ocr = self._ocr_candidates(path, query)
        candidates = sorted(
            [*dom_candidates, *ocr['candidates']],
            key=lambda item: float(item.get('confidence') or 0),
            reverse=True,
        )
        selected = candidates[0] if candidates else None
        visible_text = ' '.join(str(item.get('text') or '') for item in ocr['candidates'])
        issues = self._detect_issues(visible_text, image_signals)
        applitools_result = (
            self.applitools.check_image(path, checkpoint_name=checkpoint_name)
            if use_applitools else
            {'status': 'skipped', 'reason': 'not-requested'}
        )
        return {
            'element': str((selected or {}).get('element') or ''),
            'position': (selected or {}).get('position'),
            'confidence': round(float((selected or {}).get('confidence') or 0), 4),
            'source': str((selected or {}).get('source') or 'none'),
            'text': str((selected or {}).get('text') or ''),
            'matched': bool(selected and float(selected.get('confidence') or 0) >= self.min_confidence),
            'candidates': candidates[:50],
            'page': {
                **image_signals,
                'ocr_available': ocr['available'],
                'ocr_backend': ocr['backend'],
                'visible_text': visible_text[:5000],
                'issues': issues,
                'abnormal': bool(issues),
            },
            'applitools': applitools_result,
        }

    def _dom_candidates(self, elements: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        query_normalized = _normalize(query)
        candidates = []
        for item in elements[:500]:
            if not isinstance(item, dict):
                continue
            text = str(item.get('name') or item.get('text') or item.get('label') or '').strip()
            score = _text_score(text, query_normalized, base=0.72)
            if query_normalized and score < self.min_confidence:
                continue
            position = _position(item.get('bbox') or item.get('bounds') or item.get('position'))
            candidates.append({
                'element': text or str(item.get('role') or item.get('tag') or 'element'),
                'position': position,
                'confidence': round(score, 4),
                'source': 'dom',
                'text': text,
                'role': str(item.get('role') or item.get('tag') or ''),
            })
        return candidates

    def _ocr_candidates(self, path: Path, query: str) -> dict[str, Any]:
        try:
            from PIL import Image

            helper = self.ocr_factory(languages=['ch_sim', 'en'], use_gpu=False)
            with Image.open(path) as image:
                raw = helper._read_candidates(image.convert('RGB'))
        except (ImportError, RuntimeError) as exc:
            return {'available': False, 'backend': '', 'candidates': [], 'error': str(exc)}
        except Exception as exc:
            logger.warning('Vision OCR failed for %s: %s', path, exc)
            return {'available': False, 'backend': '', 'candidates': [], 'error': str(exc)}

        query_normalized = _normalize(query)
        candidates = []
        backends = []
        for item in raw[:500]:
            text = str(item.get('text') or '').strip()
            if not text:
                continue
            backend = str(item.get('backend') or 'ocr')
            if backend not in backends:
                backends.append(backend)
            ocr_confidence = min(max(float(item.get('confidence') or 0), 0.0), 1.0)
            score = _text_score(text, query_normalized, base=ocr_confidence)
            if query_normalized and score < self.min_confidence:
                continue
            candidates.append({
                'element': text,
                'position': _position(item.get('bbox') or item.get('center')),
                'confidence': round(score, 4),
                'source': 'ocr',
                'text': text,
                'backend': backend,
            })
        return {'available': True, 'backend': ','.join(backends), 'candidates': candidates}

    def _image_signals(self, path: Path) -> dict[str, Any]:
        from PIL import Image, ImageStat

        with Image.open(path) as image:
            grayscale = image.convert('L').resize((64, 64))
            stat = ImageStat.Stat(grayscale)
            mean = float(stat.mean[0])
            variance = float(stat.var[0])
            width, height = image.size
        return {
            'width': width,
            'height': height,
            'brightness': round(mean, 2),
            'variance': round(variance, 2),
            'suspected_white_screen': mean >= 245 and variance <= 18,
            'suspected_black_screen': mean <= 10 and variance <= 18,
        }

    def _detect_issues(self, visible_text: str, signals: dict[str, Any]) -> list[dict[str, Any]]:
        issues = []
        if signals.get('suspected_white_screen'):
            issues.append({'type': 'white_screen', 'severity': 'P1', 'evidence': '截图亮度高且像素方差极低'})
        if signals.get('suspected_black_screen'):
            issues.append({'type': 'black_screen', 'severity': 'P1', 'evidence': '截图亮度低且像素方差极低'})
        for pattern in _ERROR_PATTERNS:
            match = pattern.search(visible_text)
            if match:
                issues.append({'type': 'error_page', 'severity': 'P1', 'evidence': match.group(0)[:200]})
                break
        return issues


class ApplitoolsAdapter:
    """Optional Applitools Images adapter without making it a hard dependency."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        server_url: str | None = None,
        app_name: str | None = None,
        batch_name: str | None = None,
        runner_factory: Callable[[], Any] | None = None,
    ):
        self.api_key = api_key if api_key is not None else getattr(settings, 'APPLITOOLS_API_KEY', '')
        self.server_url = server_url if server_url is not None else getattr(settings, 'APPLITOOLS_SERVER_URL', '')
        self.app_name = app_name or getattr(settings, 'APPLITOOLS_APP_NAME', 'RunnerGo AI E2E')
        self.batch_name = batch_name or getattr(settings, 'APPLITOOLS_BATCH_NAME', 'RunnerGo E2E')
        self.runner_factory = runner_factory

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def check_image(self, image_path: Path, *, checkpoint_name: str) -> dict[str, Any]:
        if not self.enabled:
            return {'status': 'unavailable', 'reason': 'APPLITOOLS_API_KEY-not-configured'}
        try:
            if self.runner_factory:
                runner = self.runner_factory()
                return runner(image_path, checkpoint_name)
            from applitools.images import BatchInfo, Eyes, Target
            from applitools.selenium import ClassicRunner

            eyes = Eyes(ClassicRunner())
            eyes.api_key = self.api_key
            if self.server_url:
                eyes.server_url = self.server_url
            eyes.batch = BatchInfo(self.batch_name)
            eyes.open(app_name=self.app_name, test_name=checkpoint_name)
            eyes.check(checkpoint_name, Target.image(str(image_path)))
            results = eyes.close(False)
            return {'status': 'passed' if results.is_passed else 'failed', 'url': str(results.url or '')}
        except ImportError:
            return {'status': 'unavailable', 'reason': 'applitools-sdk-not-installed'}
        except Exception as exc:
            logger.exception('Applitools image check failed')
            return {'status': 'failed', 'reason': str(exc)[:1000]}


def _default_ocr_factory(**kwargs):
    from apps.app_automation.utils.ocr_helper import get_ocr_helper

    return get_ocr_helper(**kwargs)


def _normalize(value: Any) -> str:
    return re.sub(r'\s+', '', str(value or '')).casefold()


def _text_score(text: str, query: str, *, base: float) -> float:
    if not query:
        return min(max(base, 0.0), 1.0)
    actual = _normalize(text)
    if not actual:
        return 0.0
    if actual == query:
        return max(base, 0.98)
    if query in actual or actual in query:
        return max(base, 0.88)
    query_chars = set(query)
    overlap = len(query_chars.intersection(actual)) / max(len(query_chars), 1)
    return min(base * overlap, 0.79)


def _position(value: Any) -> dict[str, int] | None:
    if isinstance(value, dict):
        if {'x', 'y'}.issubset(value):
            return {'x': int(value['x']), 'y': int(value['y'])}
        if {'x1', 'y1', 'x2', 'y2'}.issubset(value):
            return {
                'x': int((float(value['x1']) + float(value['x2'])) / 2),
                'y': int((float(value['y1']) + float(value['y2'])) / 2),
            }
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {'x': int(value[0]), 'y': int(value[1])}
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return {'x': int((float(value[0]) + float(value[2])) / 2), 'y': int((float(value[1]) + float(value[3])) / 2)}
    return None
