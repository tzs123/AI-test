"""SQLite concurrency helpers for APP automation workers."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from django.conf import settings
from django.db import OperationalError, close_old_connections, connection


logger = logging.getLogger(__name__)
T = TypeVar('T')


def configure_sqlite_connection(sender, connection, **kwargs) -> None:
    """Reduce reader/writer contention for the local multi-process setup."""
    if connection.vendor != 'sqlite':
        return
    timeout_seconds = int(
        connection.settings_dict.get('OPTIONS', {}).get('timeout', 30)
    )
    with connection.cursor() as cursor:
        cursor.execute(f'PRAGMA busy_timeout = {max(timeout_seconds, 1) * 1000}')
        # In-memory test databases cannot switch to WAL and simply keep their
        # native journal mode. File-backed local deployments use WAL.
        cursor.execute('PRAGMA journal_mode = WAL')
        cursor.execute('PRAGMA synchronous = NORMAL')


def is_database_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and 'database is locked' in str(exc).lower()


def retry_database_write(
    operation: Callable[[], T],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
) -> T:
    """Retry only transient SQLite lock conflicts outside atomic blocks."""
    configured_attempts = int(getattr(settings, 'SQLITE_LOCK_RETRY_ATTEMPTS', 3))
    configured_delay = float(getattr(settings, 'SQLITE_LOCK_RETRY_BASE_DELAY', 0.1))
    attempts = max(1, min(attempts or configured_attempts, 5))
    base_delay = max(0.0, min(base_delay if base_delay is not None else configured_delay, 1.0))

    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            can_retry = (
                connection.vendor == 'sqlite'
                and is_database_locked_error(exc)
                and not connection.in_atomic_block
                and attempt + 1 < attempts
            )
            if not can_retry:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                'SQLite 写锁冲突，%.2f 秒后重试（%s/%s）',
                delay,
                attempt + 2,
                attempts,
            )
            close_old_connections()
            time.sleep(delay)

    raise AssertionError('unreachable')
