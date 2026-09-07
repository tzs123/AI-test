from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from backend.agent.e2e.vision_agent import ApplitoolsAdapter, VisionAgent


class _FakeOCR:
    def __init__(self, candidates):
        self.candidates = candidates

    def _read_candidates(self, _image):
        return self.candidates


def _image(path: Path, *, color='white', text_marker=False):
    image = Image.new('RGB', (640, 360), color=color)
    if text_marker:
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 140, 460, 220), fill='black')
    image.save(path)
    return path


def test_vision_agent_fuses_dom_and_ocr_and_prefers_exact_dom_match(tmp_path):
    screenshot = _image(tmp_path / 'page.png', text_marker=True)
    ocr = _FakeOCR([{
        'text': 'Login',
        'confidence': 0.91,
        'bbox': [200, 150, 320, 200],
        'backend': 'tesseract',
    }])
    agent = VisionAgent(ocr_factory=lambda **_: ocr)

    result = agent.analyze(
        screenshot,
        query='Login',
        dom_elements=[{
            'name': 'Login',
            'role': 'button',
            'bounds': {'x1': 190, 'y1': 140, 'x2': 330, 'y2': 210},
        }],
    )

    assert result['matched'] is True
    assert result['element'] == 'Login'
    assert result['position'] == {'x': 260, 'y': 175}
    assert result['confidence'] == 0.98
    assert result['source'] == 'dom'
    assert result['page']['ocr_available'] is True
    assert result['page']['ocr_backend'] == 'tesseract'


def test_vision_agent_returns_ocr_position_and_rejects_low_confidence(tmp_path):
    screenshot = _image(tmp_path / 'ocr.png', text_marker=True)
    ocr = _FakeOCR([
        {'text': 'Pay now', 'confidence': 0.86, 'center': [420, 280], 'backend': 'easyocr'},
        {'text': 'Noise', 'confidence': 0.12, 'center': [20, 20], 'backend': 'easyocr'},
    ])
    result = VisionAgent(ocr_factory=lambda **_: ocr, min_confidence=0.5).analyze(screenshot, query='Pay now')

    assert result['source'] == 'ocr'
    assert result['position'] == {'x': 420, 'y': 280}
    assert result['confidence'] == 0.98
    assert result['matched'] is True
    assert all(item['text'] != 'Noise' for item in result['candidates'])


def test_vision_agent_detects_white_screen_and_error_page(tmp_path):
    screenshot = _image(tmp_path / 'white.png')
    ocr = _FakeOCR([{
        'text': '500 Internal Server Error',
        'confidence': 0.94,
        'bbox': [20, 20, 300, 80],
        'backend': 'tesseract-line',
    }])
    result = VisionAgent(ocr_factory=lambda **_: ocr).analyze(screenshot)

    issue_types = {item['type'] for item in result['page']['issues']}
    assert result['page']['abnormal'] is True
    assert result['page']['suspected_white_screen'] is True
    assert issue_types == {'white_screen', 'error_page'}


def test_applitools_adapter_has_explicit_unavailable_and_runner_states(tmp_path):
    screenshot = _image(tmp_path / 'checkpoint.png', text_marker=True)
    disabled = ApplitoolsAdapter(api_key='')
    assert disabled.check_image(screenshot, checkpoint_name='checkout')['status'] == 'unavailable'

    calls = []

    def runner(path, checkpoint):
        calls.append((path, checkpoint))
        return {'status': 'passed', 'url': 'https://eyes.example.test/result'}

    enabled = ApplitoolsAdapter(api_key='test-key', runner_factory=lambda: runner)
    result = enabled.check_image(screenshot, checkpoint_name='checkout')
    assert result['status'] == 'passed'
    assert calls == [(screenshot, 'checkout')]
