from __future__ import annotations

import logging

from celery import shared_task
from django.db import close_old_connections, connection

from .models import AgentTask


logger = logging.getLogger(__name__)


@shared_task(name='agent.execute_react_task')
def execute_react_agent_task(task_id: int) -> dict:
    from backend.agent.core.executor import ReActAgentExecutor

    close_old_connections()
    try:
        task = AgentTask.objects.get(pk=task_id)
        result = ReActAgentExecutor(task).run()

        # Celery must not report a terminal state that only exists in its
        # current SQLite connection. Commit, reopen, and return the state that
        # other processes will observe.
        if connection.in_atomic_block:
            raise RuntimeError('Agent worker finished inside an open database transaction')
        connection.commit()
        connection.close()
        persisted = AgentTask.objects.get(pk=task_id)
        if persisted.status != result.status:
            raise RuntimeError(
                'Agent worker database state mismatch: '
                f'expected {result.status}, persisted {persisted.status}'
            )
        return {'task_id': persisted.id, 'status': persisted.status}
    except AgentTask.DoesNotExist:
        logger.warning('Agent task %s no longer exists', task_id)
        return {'task_id': task_id, 'status': 'missing'}
    finally:
        close_old_connections()
