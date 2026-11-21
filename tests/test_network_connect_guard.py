"""Offline regression tests for connect-time network destination checks."""
import socket
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from crow_os import tools


class ConnectGuardTests(unittest.TestCase):
    def test_all_answers_checked_before_any_socket_is_created(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80)),
        ]
        with patch.object(socket, 'getaddrinfo', return_value=addresses), patch.object(socket, 'socket') as create:
            with self.assertRaises(tools.ToolError):
                tools._pinned_create_connection(('mixed.test', 80))
            create.assert_not_called()

    def test_connect_dials_checked_numeric_tuple_without_reresolving(self):
        address = ('8.8.8.8', 443)
        sock = Mock()
        with patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]) as resolve, patch.object(socket, 'socket', return_value=sock):
            self.assertIs(tools._pinned_create_connection(('public.test', 443), timeout=2), sock)
            resolve.assert_called_once_with('public.test', 443, 0, socket.SOCK_STREAM)
            sock.connect.assert_called_once_with(address)
            sock.settimeout.assert_called_once_with(2)

    def test_failed_socket_closed_before_fallback(self):
        first, second = Mock(), Mock()
        first.connect.side_effect = ConnectionRefusedError('refused')
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in ['8.8.8.8', '1.1.1.1']]
        with patch.object(socket, 'getaddrinfo', return_value=addresses), patch.object(socket, 'socket', side_effect=[first, second]):
            self.assertIs(tools._pinned_create_connection(('public.test', 80)), second)
            first.close.assert_called_once()
            second.connect.assert_called_once_with(('1.1.1.1', 80))

    def test_empty_answers_are_refused(self):
        with patch.object(socket, 'getaddrinfo', return_value=[]):
            with self.assertRaises(tools.ToolError):
                tools._pinned_create_connection(('empty.test', 80))

    def test_ipv4_translation_cannot_hide_private_destinations(self):
        for ip in ['64:ff9b::a00:1', '2002:c0a8:101::1', '::ffff:10.0.0.1', '100.64.0.1', '2606:4700::1111%eth0']:
            self.assertTrue(tools._ip_blocked(ip), ip)
        for ip in ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111']:
            self.assertFalse(tools._ip_blocked(ip), ip)

    def test_explicit_private_optin_works_and_ignores_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'local opt-in response')
            def log_message(self, *args):
                pass
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        runner = tools.ToolRunner(Mock(), {}, allow_private=True)
        try:
            with patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': ''}):
                result = runner.fetch(f'http://127.0.0.1:{srv.server_address[1]}/')
            self.assertEqual(result['excerpt'], 'local opt-in response')
            self.assertEqual(result['status'], 200)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
