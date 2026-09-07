"""决策引擎：根据任务状态决定下一个 Action（Thought -> Action）。

- build_action_queue：把测试计划步骤转换为可执行 Action 队列
- decide_next_action：按队列顺序返回下一个未执行 Action
- generate_thought：为当前 Action 生成 Thought 文本
"""
from __future__ import annotations

from typing import Any, Optional

from backend.agent.core.planner import build_react_plan


def build_action_queue(requirement: str, plan: Optional[dict] = None) -> list[dict]:
    """把测试计划步骤转换为 Action 队列。"""
    plan = plan or build_react_plan(requirement)
    queue: list[dict] = []
    for step in plan.get("steps") or []:
        tool = str(step.get("tool") or "")
        queue.append({
            "index": len(queue),
            "tool": tool,
            "params": dict(step.get("payload") or {}),
            "action": str(step.get("action") or tool)[:300],
            "type": str(step.get("type") or ""),
        })
    return queue


def decide_next_action(state: dict) -> Optional[dict]:
    """返回下一个未执行 Action；全部完成返回 None。"""
    queue = state.get("queue") or []
    done = set(state.get("done_indices") or [])
    skipped = set(state.get("skipped_indices") or [])
    for action in queue:
        index = int(action.get("index", -1))
        if index in done or index in skipped:
            continue
        return action
    return None


def generate_thought(
    requirement: str,
    action: dict,
    *,
    context: Optional[list[dict]] = None,
) -> str:
    """为当前 Action 生成 Thought 文本。

    ReAct 执行链路必须优先保证工具按时落地，不能因为模型服务慢或不可用阻塞。
    这里使用确定性文案，模型增强应放在非阻塞的分析/报告步骤中。
    """
    tool = str(action.get("tool") or "")
    step_action = str(action.get("action") or "")
    return f"需求「{requirement}」需要推进，下一步调用工具 {tool}：{step_action}"
