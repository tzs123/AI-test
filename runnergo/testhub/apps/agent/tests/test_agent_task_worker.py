import pytest
from django.contrib.auth import get_user_model

from apps.agent import tasks as agent_tasks
from apps.agent.models import AgentTask


@pytest.mark.django_db(transaction=True)
def test_react_worker_returns_state_reloaded_from_database(monkeypatch):
    user = get_user_model().objects.create_user(username='agent_worker_user', password='x')
    task = AgentTask.objects.create(
        task_name='worker persistence',
        user_requirement='verify committed terminal state',
        created_by=user,
        status=AgentTask.STATUS_RUNNING,
    )
    connection_cleanup_calls = []

    def fake_run(executor):
        executor.task.status = AgentTask.STATUS_COMPLETED
        executor.task.progress = 100
        executor.task.save(update_fields=['status', 'progress', 'updated_at'])
        return executor.task

    monkeypatch.setattr(
        'backend.agent.core.executor.ReActAgentExecutor.run',
        fake_run,
    )
    monkeypatch.setattr(
        agent_tasks,
        'close_old_connections',
        lambda: connection_cleanup_calls.append(True),
    )

    result = agent_tasks.execute_react_agent_task.run(task.id)

    task.refresh_from_db()
    assert result == {'task_id': task.id, 'status': AgentTask.STATUS_COMPLETED}
    assert task.status == AgentTask.STATUS_COMPLETED
    assert task.progress == 100
    assert len(connection_cleanup_calls) == 2
