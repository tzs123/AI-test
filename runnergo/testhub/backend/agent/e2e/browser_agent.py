from __future__ import annotations

from typing import Any, Callable

from .browser_executor import BrowserExecutor
from .contracts import BrowserRunRequest


class BrowserAgent:
    """Production entry point for the Playwright Browser Agent."""

    def __init__(
        self,
        *,
        executor_factory: Callable[..., BrowserExecutor] = BrowserExecutor,
    ):
        self.executor_factory = executor_factory

    def execute(
        self,
        *,
        url: str,
        steps: list[dict[str, Any]],
        task_id: str = '',
        trace_id: str = '',
        headless: bool = True,
        record_video: bool = True,
        screenshot_each_step: bool = True,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        request = BrowserRunRequest.from_payload({
            'base_url': url,
            'steps': steps,
            'task_id': task_id,
            'trace_id': trace_id,
            'headless': headless,
            'record_video': record_video,
            'screenshot_each_step': screenshot_each_step,
        })
        return self.executor_factory(on_step=on_step).run(request)
