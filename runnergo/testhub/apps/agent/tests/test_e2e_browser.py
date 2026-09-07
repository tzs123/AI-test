from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from django.test import override_settings

from backend.agent.e2e.browser_executor import BrowserExecutor, _redact, _safe_url
from backend.agent.e2e.contracts import BrowserRunRequest, E2EContractError
from backend.agent.e2e.page_inspector import PageInspector


class _TestPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/assets/app.js':
            body = b'const routes = [{path:"/result"}];'
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/result':
            body = b'<!doctype html><html><head><title>Result</title></head><body>Borrower Info</body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/delayed-title':
            body = b'''<!doctype html><html><head><title>Loading</title></head>
<body><script>setTimeout(() => { document.title = 'Ready'; }, 100);</script></body></html>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'''<!doctype html>
<html>
  <head><title>E2E Login Fixture</title></head>
  <body>
    <script src="/assets/app.js"></script>
    <label>Username <input aria-label="Username" /></label>
    <label>Password <input aria-label="Password" type="password" /></label>
    <button id="login" onclick="document.querySelector('#result').textContent='Welcome test001'; console.error('fixture-console-error')">Login</button>
    <p id="result"></p>
  </body>
</html>'''
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture()
def e2e_fixture_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _TestPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_browser_contract_rejects_unknown_or_incomplete_actions():
    with pytest.raises(E2EContractError, match='不支持的动作'):
        BrowserRunRequest.from_payload({'url': 'https://example.test', 'steps': [{'action': 'javascript'}]})
    with pytest.raises(E2EContractError, match='缺少 value'):
        BrowserRunRequest.from_payload({
            'url': 'https://example.test',
            'steps': [{'action': 'fill', 'target': {'label': 'Username'}}],
        })


def test_browser_contract_normalizes_target_and_bounds_timeout():
    request = BrowserRunRequest.from_payload({
        'url': 'https://example.test',
        'steps': [{'action': 'click', 'target': 'Login', 'timeout_ms': 1000}],
    })
    assert request.steps[0]['target'] == {'text': 'Login'}
    assert request.steps[0]['timeout_ms'] == 1000


def test_browser_contract_turns_model_target_values_into_real_assertions():
    request = BrowserRunRequest.from_payload({
        'url': 'https://example.test',
        'steps': [
            {'action': 'assert_url', 'target': {'url': 'https://example.test/result'}},
            {'action': 'assert_title', 'target': {'title': 'Result'}},
            {'action': 'assert_text', 'target': {'text': 'Borrower Info'}},
            {'action': 'wait_for_text', 'target': {'text': 'Ready'}},
        ],
    })

    assert [step['expected'] for step in request.steps] == [
        'https://example.test/result', 'Result', 'Borrower Info', 'Ready',
    ]
    assert request.steps[-1]['value'] == 'Ready'


def test_browser_contract_rejects_empty_assertions():
    with pytest.raises(E2EContractError, match='缺少非空期望值'):
        BrowserRunRequest.from_payload({
            'url': 'https://example.test',
            'steps': [{'action': 'assert_url'}],
        })


@override_settings(AI_OUTBOUND_ALLOW_PRIVATE_URLS=True)
def test_page_inspector_discovers_spa_route_matching_uncovered_point(e2e_fixture_server: str):
    snapshots = PageInspector().inspect(
        target_url=e2e_fixture_server,
        description='1、测试【Login】 2、测试【Borrower Info】',
    )

    result = next(item for item in snapshots if item.get('matched_test_points'))
    assert result['url'] == f'{e2e_fixture_server}/result'
    assert result['matched_test_points'] == ['Borrower Info']


def test_browser_evidence_redacts_credentials_and_url_userinfo():
    value = _redact({
        'Authorization': 'Bearer raw-token',
        'nested': {'password': 'plain-password'},
        'message': 'authorization Bearer visible-token',
    })
    assert value['Authorization'] == '***'
    assert value['nested']['password'] == '***'
    assert 'visible-token' not in value['message']
    assert _safe_url('https://user:pass@example.com/path#secret') == 'https://example.com/path'


def test_browser_executor_accepts_system_browser_executable(tmp_path: Path):
    executor = BrowserExecutor(
        artifact_root=tmp_path,
        executable_path='/usr/bin/google-chrome',
    )

    assert executor.executable_path == '/usr/bin/google-chrome'
    assert executor.channel is None


@override_settings(AI_OUTBOUND_ALLOW_PRIVATE_URLS=True)
def test_browser_executor_runs_real_chromium_and_collects_evidence(tmp_path: Path, e2e_fixture_server: str):
    executor = BrowserExecutor(artifact_root=tmp_path, max_events=200)
    result = executor.run({
        'url': e2e_fixture_server,
        'trace_id': 'browser-smoke',
        'record_video': False,
        'steps': [
            {'name': '打开登录页', 'action': 'navigate', 'url': '/'},
            {'name': '输入用户名', 'action': 'fill', 'target': {'label': 'Username'}, 'value': 'test001'},
            {'name': '输入密码', 'action': 'fill', 'target': {'label': 'Password'}, 'value': 'Passw0rd@2026'},
            {'name': '提交登录', 'action': 'click', 'target': {'role': 'button', 'name': 'Login'}},
            {'name': '验证登录', 'action': 'assert_text', 'target': {'css': '#result'}, 'expected': 'Welcome test001'},
            {'name': '保存最终页面证据', 'action': 'screenshot'},
        ],
    })

    assert result['status'] == 'passed'
    assert result['summary'] == {'total': 6, 'executed': 6, 'passed': 6, 'failed': 0, 'skipped': 0}
    assert len(result['screenshots']) == 6
    assert any(item['type'] == 'error' and 'fixture-console-error' in item['text'] for item in result['console_logs'])
    assert any(item['event'] == 'response' and item['status'] == 200 for item in result['network_logs'])
    assert all(Path(path).exists() for path in result['screenshots'])


@override_settings(AI_OUTBOUND_ALLOW_PRIVATE_URLS=True)
def test_browser_executor_waits_for_spa_title_update(tmp_path: Path, e2e_fixture_server: str):
    result = BrowserExecutor(artifact_root=tmp_path).run({
        'url': e2e_fixture_server,
        'record_video': False,
        'steps': [
            {'action': 'navigate', 'url': '/delayed-title'},
            {'action': 'assert_title', 'expected': 'Ready', 'timeout_ms': 2000},
        ],
    })

    assert result['status'] == 'passed'
    assert result['steps'][-1]['actual'] == 'Ready'


@override_settings(AI_OUTBOUND_ALLOW_PRIVATE_URLS=True)
def test_browser_executor_stops_after_failed_step(tmp_path: Path, e2e_fixture_server: str):
    result = BrowserExecutor(artifact_root=tmp_path).run({
        'url': e2e_fixture_server,
        'record_video': False,
        'steps': [
            {'action': 'navigate', 'url': '/'},
            {'action': 'assert_text', 'target': {'css': '#result'}, 'expected': 'not present'},
            {'action': 'click', 'target': {'text': 'Login'}},
        ],
    })

    assert result['status'] == 'failed'
    assert result['summary']['executed'] == 2
    assert result['summary']['skipped'] == 1
    assert result['steps'][-1]['error']
    assert Path(result['steps'][-1]['screenshot']).exists()
