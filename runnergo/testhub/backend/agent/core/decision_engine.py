from __future__ import annotations

from apps.agent.models import AgentStep


class DecisionEngine:
    """Selects the next action from current observations and step state."""

    def next_step(self, steps: list[AgentStep]) -> AgentStep | None:
        retryable = [
            step for step in steps
            if step.status == AgentStep.STATUS_FAILED and (step.output_payload or {}).get('retryable')
        ]
        if retryable:
            return retryable[0]
        for step in steps:
            if step.status in {AgentStep.STATUS_PENDING, AgentStep.STATUS_RUNNING}:
                return step
        return None

    def thought_for(self, step: AgentStep) -> str:
        tool = step.tool_name or 'unknown'
        return f'当前任务需要执行“{step.action}”，下一步调用工具 {tool} 获取可观测结果。'
