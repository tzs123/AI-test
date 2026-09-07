import socket
import unittest
from unittest.mock import Mock, patch

from docker.adb_proxy import AdbProxyServer


class AdbProxyRecoveryTests(unittest.TestCase):
    def test_open_upstream_recovers_host_adb_then_retries(self):
        recovered_socket = Mock(spec=socket.socket)
        server = AdbProxyServer.__new__(AdbProxyServer)
        server.upstream_host = 'host.docker.internal'
        server.upstream_port = 5037
        server.recover_upstream = Mock(return_value=True)

        with patch(
            'docker.adb_proxy.socket.create_connection',
            side_effect=[ConnectionRefusedError('offline'), recovered_socket],
        ), patch('docker.adb_proxy.time.sleep'):
            result = server.open_upstream()

        self.assertIs(result, recovered_socket)
        server.recover_upstream.assert_called_once()

    def test_open_upstream_does_not_recover_without_recovery_url(self):
        server = AdbProxyServer.__new__(AdbProxyServer)
        server.upstream_host = 'remote-adb.example.com'
        server.upstream_port = 5037
        server.recover_upstream = Mock(return_value=False)

        with patch(
            'docker.adb_proxy.socket.create_connection',
            side_effect=ConnectionRefusedError('offline'),
        ):
            with self.assertRaises(ConnectionRefusedError):
                server.open_upstream()

        server.recover_upstream.assert_called_once()


if __name__ == '__main__':
    unittest.main()
