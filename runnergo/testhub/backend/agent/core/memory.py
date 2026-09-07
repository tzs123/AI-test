from __future__ import annotations

from typing import Any, Dict

from apps.agent.models import AgentMemory, AgentTask
from backend.agent.memory import AgentMemoryStore


class ReActMemory:
    """Project-aware memory facade."""

    def __init__(self) -> None:
        self.store = AgentMemoryStore()

    def remember(self, task: AgentTask, content: str, **kwargs: Any) -> AgentMemory:
        return self.store.remember(task, content, **kwargs)

    def recall_project(self, task: AgentTask, limit: int = 20) -> list[Dict[str, Any]]:
        if not task.project_id:
            return []
        rows = AgentMemory.objects.filter(project=task.project).order_by('-created_at')[:limit]
        return [
            {
                'memory_type': row.memory_type,
                'content': row.content,
                'payload': row.payload,
                'created_at': row.created_at.isoformat(),
            }
            for row in rows
        ]
