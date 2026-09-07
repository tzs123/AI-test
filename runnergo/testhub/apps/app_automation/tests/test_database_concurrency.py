from unittest.mock import MagicMock, call, patch

from django.db import OperationalError
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.app_automation.database import (
    configure_sqlite_connection,
    retry_database_write,
)


class SQLiteConnectionConfigurationTests(SimpleTestCase):
    def test_sqlite_connection_enables_busy_timeout_wal_and_normal_sync(self):
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        fake_connection = MagicMock(
            vendor='sqlite',
            settings_dict={'OPTIONS': {'timeout': 30}},
        )
        fake_connection.cursor.return_value = context

        configure_sqlite_connection(None, fake_connection)

        self.assertEqual(cursor.execute.call_args_list, [
            call('PRAGMA busy_timeout = 30000'),
            call('PRAGMA journal_mode = WAL'),
            call('PRAGMA synchronous = NORMAL'),
        ])


class SQLiteWriteRetryTests(TransactionTestCase):
    @override_settings(SQLITE_LOCK_RETRY_ATTEMPTS=3, SQLITE_LOCK_RETRY_BASE_DELAY=0.01)
    @patch('apps.app_automation.database.close_old_connections')
    @patch('apps.app_automation.database.time.sleep')
    def test_transient_database_lock_is_retried_with_bounded_backoff(
        self,
        sleep_mock,
        close_connections_mock,
    ):
        operation = MagicMock(side_effect=[
            OperationalError('database is locked'),
            OperationalError('database is locked'),
            'saved',
        ])

        result = retry_database_write(operation)

        self.assertEqual(result, 'saved')
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.01), call(0.02)])
        self.assertEqual(close_connections_mock.call_count, 2)

    @patch('apps.app_automation.database.time.sleep')
    def test_non_lock_operational_error_is_not_retried(self, sleep_mock):
        operation = MagicMock(side_effect=OperationalError('disk I/O error'))

        with self.assertRaisesMessage(OperationalError, 'disk I/O error'):
            retry_database_write(operation)

        operation.assert_called_once_with()
        sleep_mock.assert_not_called()
