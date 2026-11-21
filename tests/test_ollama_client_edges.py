"""Offline provider-boundary checks using an actual loopback HTTP server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from crow_os import llm


class ResponseHandler(BaseHTTPRequestHandler):
    payload = b'{}'
    content_type = 'application/json'
    def do_GET(self):
        self._response()
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self._response()
    def _response(self):
        body = type(self).payload
        self.send_response(200)
        self.send_header('Content-Type', type(self).content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass


class ProviderBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), ResponseHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
    def setUp(self):
        ResponseHandler.payload = b'{}'
        ResponseHandler.content_type = 'application/json'
        self.client = llm.OllamaClient(base_url=self.base, model='test:1b')

    def test_truncated_native_stream_does_not_return_partial_answer(self):
        ResponseHandler.content_type = 'application/x-ndjson'
        ResponseHandler.payload = b'{"message":{"content":"partial"},"done":false}\n'
        tokens = []
        with self.assertRaisesRegex(llm.LLMUnavailable, 'before completion'):
            self.client.chat_stream('system', 'user', tokens.append)
        self.assertEqual(tokens, ['partial'])

    def test_explicit_incomplete_json_is_not_legacy_completion(self):
        ResponseHandler.payload = b'{"message":{"content":"partial"},"done":false}'
        for operation in (self.client.chat, self.client.chat_stream):
            with self.subTest(operation=operation.__name__), self.assertRaises(llm.LLMUnavailable):
                operation('system', 'user')

    def test_non_boolean_completion_marker_is_refused(self):
        for value in ('true', 1, None):
            ResponseHandler.payload = json.dumps({'message': {'content': 'answer'}, 'done': value}).encode()
            for operation in (self.client.chat, self.client.chat_stream):
                with self.subTest(value=value, operation=operation.__name__), self.assertRaises(llm.LLMUnavailable):
                    operation('system', 'user')

    def test_finite_legacy_json_response_remains_supported(self):
        ResponseHandler.payload = b'{"message":{"content":"legacy response"}}'
        self.assertEqual(self.client.chat_stream('system', 'user'), 'legacy response')

    def test_malformed_tag_collection_cannot_claim_model_is_installed(self):
        for value in ({'models': 'test:1b'}, {'models': None}, {'models': [None, 2, {'name': []}]}):
            ResponseHandler.payload = json.dumps(value).encode()
            self.assertEqual(self.client.health()['models'], [])
            self.assertFalse(self.client.health()['model_present'])
        ResponseHandler.payload = b'[]'
        with self.assertRaises(llm.LLMUnavailable):
            self.client.health()

    def test_response_size_limit_applies_to_json_and_stream_frames(self):
        ResponseHandler.payload = json.dumps({'message': {'content': 'a' * 100}, 'done': True}).encode()
        with patch.object(llm, 'MAX_RESPONSE_BYTES', 64):
            for operation in (self.client.chat, self.client.chat_stream):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(llm.LLMUnavailable, 'size limit'):
                    operation('system', 'user')


class ConfigurationBoundary(unittest.TestCase):
    def test_credentials_non_http_and_non_origin_endpoints_are_refused(self):
        for base in ('http://user:secret@example.com', 'file:///tmp/model', 'https://example.com/base',
                     'http://localhost:11434?key=secret', 'http://localhost:11434#part'):
            with self.subTest(base=base), self.assertRaises(llm.LLMUnavailable) as error:
                llm.OllamaClient(base_url=base)
            self.assertNotIn('secret', str(error.exception))

    def test_remote_provider_keeps_environment_transport(self):
        with patch.object(llm.urllib.request, 'build_opener') as build:
            llm.OllamaClient(base_url='https://example.com:11434')
            build.assert_called_once_with()

    def test_explicit_model_never_probes_or_replaces_it(self):
        with patch.object(llm, 'OllamaClient') as client:
            self.assertEqual(llm.resolve_model('requested:7b'), 'requested:7b')
            client.assert_not_called()

    def test_health_failure_retains_configuration_without_claiming_inference(self):
        with patch.object(llm, 'MODEL_FROM_ENV', False), patch.object(llm, 'OllamaClient') as client:
            client.return_value.health.side_effect = llm.LLMUnavailable('offline')
            self.assertEqual(llm.resolve_model(None), llm.DEFAULT_MODEL)
            client.return_value.chat.assert_not_called()

    def test_registry_port_is_distinct_from_model_tag(self):
        self.assertTrue(llm.same_model('registry.example:5000/team/model', 'registry.example:5000/team/model:latest'))
        self.assertFalse(llm.same_model('', ''))
        self.assertEqual(llm._base_from_env('  '), 'http://127.0.0.1:11434')
        self.assertIsNone(llm.pick_local_model(['nomic-embed-text:latest'], 'nomic-embed-text:latest'))
