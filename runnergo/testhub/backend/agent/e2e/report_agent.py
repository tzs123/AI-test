from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.template import Engine, Context

from .evidence import image_data_uri


_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
  <title>{{ task.task_name }} - E2E 测试报告</title>
  <style>
    :root { color-scheme: light; --ink:#17202a; --muted:#667085; --line:#d0d5dd; --ok:#067647; --bad:#b42318; --surface:#f8fafc; }
    * { box-sizing: border-box; }
    body { margin:0; color:var(--ink); background:#fff; font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { padding:28px max(24px,calc((100vw - 1120px)/2)); color:#fff; background:#202b38; }
    h1 { margin:0 0 6px; font-size:26px; letter-spacing:0; } h2 { margin:28px 0 12px; font-size:18px; letter-spacing:0; }
    main { max-width:1120px; margin:auto; padding:24px; } .meta { color:#d0d5dd; }
    .summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border:1px solid var(--line); border-radius:6px; overflow:hidden; }
    .metric { padding:16px; border-right:1px solid var(--line); } .metric:last-child { border:0; }
    .metric strong { display:block; font-size:24px; } .muted { color:var(--muted); }
    table { width:100%; border-collapse:collapse; table-layout:fixed; } th,td { padding:10px; border:1px solid var(--line); text-align:left; vertical-align:top; overflow-wrap:anywhere; }
    th { background:var(--surface); } .passed { color:var(--ok); font-weight:700; } .failed { color:var(--bad); font-weight:700; }
    pre { max-height:420px; overflow:auto; margin:0; padding:12px; border:1px solid var(--line); background:var(--surface); white-space:pre-wrap; overflow-wrap:anywhere; }
    img { display:block; max-width:100%; max-height:560px; margin-top:8px; border:1px solid var(--line); }
    @media (max-width:720px) { .summary { grid-template-columns:repeat(2,minmax(0,1fr)); } .metric:nth-child(2) { border-right:0; } table { display:block; overflow-x:auto; } }
  </style>
</head>
<body>
  <header>
    <h1>{{ task.task_name }}</h1>
    <div class="meta">AI E2E Testing Agent · Trace {{ task.trace_id }} · {{ generated_at }}</div>
  </header>
  <main>
    <section class="summary">
      <div class="metric"><span class="muted">状态</span><strong class="{{ status_class }}">{{ task.status }}</strong></div>
      <div class="metric"><span class="muted">执行器</span><strong>{{ task.executor_type }}</strong></div>
      <div class="metric"><span class="muted">通过率</span><strong>{{ pass_rate }}%</strong></div>
      <div class="metric"><span class="muted">耗时</span><strong>{{ duration_ms }} ms</strong></div>
    </section>

    <h2>任务信息</h2>
    <table><tbody>
      <tr><th style="width:160px">测试需求</th><td>{{ task.description }}</td></tr>
      <tr><th>测试目标</th><td>{{ task.scenario.objective|default:"" }}</td></tr>
      <tr><th>目标环境</th><td>{{ task.target_url|default:"移动端设备" }}</td></tr>
      <tr><th>预期结果</th><td>{{ task.expected_result }}</td></tr>
    </tbody></table>

    <h2>执行步骤</h2>
    <table><thead><tr><th style="width:58px">#</th><th>步骤</th><th style="width:110px">动作</th><th style="width:90px">结果</th><th>错误与证据</th></tr></thead><tbody>
    {% for step in steps %}
      <tr>
        <td>{{ step.step }}</td><td>{{ step.step_name }}</td><td>{{ step.action }}</td>
        <td class="{{ step.status_class }}">{{ step.status }}</td>
        <td>{{ step.error }}{% if step.screenshot %}<a href="{{ step.screenshot }}" target="_blank" rel="noopener">查看截图</a><img src="{{ step.screenshot }}" alt="步骤 {{ step.step }} 截图">{% endif %}</td>
      </tr>
    {% empty %}<tr><td colspan="5">尚无执行步骤证据</td></tr>{% endfor %}
    </tbody></table>

    {% if video_captured %}<p class="muted">执行视频已保存，可通过任务证据接口鉴权下载。</p>{% endif %}

    <h2>AI 异常分析</h2>
    {% if analysis %}
    <table><tbody>
      <tr><th style="width:160px">标题</th><td>{{ analysis.title }}</td></tr>
      <tr><th>严重等级</th><td>{{ analysis.severity }}</td></tr>
      <tr><th>原因</th><td>{{ analysis.root_cause }}</td></tr>
      <tr><th>复现步骤</th><td>{% for item in analysis.reproduction_steps %}{{ item }}<br>{% endfor %}</td></tr>
      <tr><th>修复建议</th><td>{{ analysis.fix_suggestion }}</td></tr>
    </tbody></table>
    {% else %}<p class="muted">未发现需要 AI 分析的失败。</p>{% endif %}

    <h2>错误日志</h2>
    <pre>{{ error_logs }}</pre>
  </main>
</body>
</html>'''


class ReportAgent:
    """Generate an escaped, self-contained HTML summary for an E2E task."""

    def __init__(self, *, output_root: str | os.PathLike[str] | None = None):
        media_root = Path(getattr(settings, 'MEDIA_ROOT', Path.cwd() / 'media'))
        self.output_root = Path(output_root or media_root / 'e2e-reports')

    def generate(self, task: Any) -> str:
        logs = list(task.execution_logs.order_by('attempt', 'step', 'id'))
        total = len(logs) or len(task.steps or [])
        passed = len([item for item in logs if item.status == 'PASSED'])
        result = task.result if isinstance(task.result, dict) else {}
        duration_ms = int(result.get('duration_ms') or sum(item.duration_ms for item in logs))
        video_captured = bool(result.get('video') or next((item.video for item in logs if item.video), ''))
        error_logs = '\n\n'.join(filter(None, [
            str(task.error_message or ''),
            *[item.error for item in logs if item.error],
            str(result.get('error') or ''),
            json.dumps(result.get('page_errors') or [], ensure_ascii=False, default=str),
        ]))[:100000]
        rows = [{
            'step': item.step,
            'step_name': item.step_name,
            'action': item.action,
            'status': item.status,
            'status_class': 'passed' if item.status == 'PASSED' else ('failed' if item.status == 'FAILED' else ''),
            'error': item.error,
            'screenshot': image_data_uri(task.id, item.screenshot),
        } for item in logs]
        raw_analysis = task.analysis if isinstance(task.analysis, dict) else {}
        analysis = {
            **raw_analysis,
            'root_cause': raw_analysis.get('root_cause') or raw_analysis.get('reason') or '',
            'reproduction_steps': raw_analysis.get('reproduction_steps') or [],
        }
        context = Context({
            'task': task,
            'steps': rows,
            'analysis': analysis,
            'video_captured': video_captured,
            'error_logs': error_logs or '无错误日志',
            'pass_rate': round((passed / total * 100) if total else 0, 2),
            'duration_ms': duration_ms,
            'status_class': 'passed' if task.status == 'PASSED' else ('failed' if task.status == 'FAILED' else ''),
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }, autoescape=True)
        html = Engine(debug=False).from_string(_TEMPLATE).render(context)
        self.output_root.mkdir(parents=True, exist_ok=True)
        path = self.output_root / f'e2e-task-{int(task.id)}.html'
        temporary = path.with_suffix('.html.tmp')
        temporary.write_text(html, encoding='utf-8')
        temporary.replace(path)
        return str(path)

    def relative_path(self, path: str) -> str:
        media_root = Path(getattr(settings, 'MEDIA_ROOT', self.output_root.parent)).resolve()
        return Path(path).resolve().relative_to(media_root).as_posix()
