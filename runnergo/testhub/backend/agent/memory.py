from __future__ import annotations

from typing import Any, Dict

from apps.agent.models import AgentMemory, AgentStep, AgentTask


class AgentMemoryStore:
    """Agent 持久化记忆。"""

    def remember(
        self,
        task: AgentTask,
        content: str,
        *,
        step: AgentStep | None = None,
        memory_type: str = 'event',
        role: str = 'agent',
        payload: Dict[str, Any] | None = None,
    ) -> AgentMemory:
        return AgentMemory.objects.create(
            task=task,
            project=task.project,
            step=step,
            memory_type=memory_type,
            role=role,
            content=content[:2000],
            payload=payload or {},
        )
