"""Local Ollama transport hardening, with no model downloads or Internet access."""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.llm import (LLMUnavailable, MAX_FRAME_BYTES, MAX_REPLY_BYTES,
                         OllamaClient)


class FakeOllama(BaseHTTPRequestHandler):
    tags = {"models": [{"name": "test:latest"}]}
    chat_bytes = b'{"message":{"content":"hello"}}'
    chat_status = 200
    captured = []

    def do_GET(self):
        body = self.tags if isinstance(self.tags, bytes) else json.dumps(self.tags).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        body = type(self).chat_bytes
        self.send_response(type(self).chat_status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def server(*, frames=None, chat=None, tags=None, status=200):
    class Handler(FakeOllama):
        pass
    Handler.chat_bytes = (b"\n".join(json.dumps(f).encode() for f in frames) + b"\n") if frames is not None else (json.dumps(chat).encode() if chat is not None else FakeOllama.chat_bytes)
    Handler.tags = tags if tags is not None else FakeOllama.tags
    Handler.chat_status = status
    Handler.captured = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield OllamaClient(base_url=f"http://127.0.0.1:{httpd.server_port}", model="test:latest"), Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


class OllamaHardeningTests(unittest.TestCase):
    def test_normal_stream_and_callback(self):
        with server(frames=[{"message": {"content": "hello"}, "done": False},
                            {"message": {"content": " world"}, "done": False},
                            {"done": True, "done_reason": "stop"}]) as (client, handler):
            seen = []
            self.assertEqual(client.chat_stream("system", "user", seen.append), "hello world")
            self.assertEqual(seen, ["hello", " world"])
            self.assertTrue(handler.captured[0]["stream"])
            self.assertEqual(client.calls, 1)

    def test_legacy_one_object_stub_compatible(self):
        with server(chat={"message": {"content": "older fixture"}}) as (client, _):
            self.assertEqual(client.chat_stream("s", "u"), "older fixture")

    def test_partial_stream_fails_closed(self):
        with server(frames=[{"message": {"content": "partial"}, "done": False}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "terminal"):
                c.chat_stream("s", "u")

    def test_trailing_stream_error_overrides_partial_content(self):
        with server(frames=[{"message": {"content": "partial"}, "done": False},
                            {"error": "model unloaded"}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "error"):
                c.chat_stream("s", "u")

    def test_truncated_termination_fails(self):
        with server(frames=[{"message": {"content": "partial"}, "done": True, "done_reason": "length"}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "completion"):
                c.chat_stream("s", "u")

    def test_terminal_frame_ignores_trailing_data_for_v09_compatibility(self):
        with server(frames=[{"message": {"content": "a"}, "done": True},
                            {"message": {"content": "b"}, "done": True}]) as (c, _):
            self.assertEqual(c.chat_stream("s", "u"), "a")

    def test_non_dict_stream_fails(self):
        with server(frames=[["bad"]]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "object"):
                c.chat_stream("s", "u")

    def test_nonboolean_done_fails(self):
        with server(frames=[{"message": {"content": "hello"}, "done": "false"}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "boolean"):
                c.chat_stream("s", "u")

    def test_null_content_fails(self):
        with server(frames=[{"message": {"content": None}, "done": True}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "non-text"):
                c.chat_stream("s", "u")

    def test_oversized_frame_fails(self):
        with server(frames=[{"message": {"content": "x" * (MAX_FRAME_BYTES + 5)}, "done": True}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "byte budget"):
                c.chat_stream("s", "u")

    def test_incomplete_multiframe_legacy_fails(self):
        with server(frames=[{"message": {"content": "a"}}, {"message": {"content": "b"}}]) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "terminal"):
                c.chat_stream("s", "u")

    def test_callback_failure_fails(self):
        with server(frames=[{"message": {"content": "a"}, "done": True}]) as (c, _):
            def fail(_token):
                raise RuntimeError("failure writing display")
            with self.assertRaisesRegex(LLMUnavailable, "consumer"):
                c.chat_stream("s", "u", fail)

    def test_tags_and_missing_model(self):
        with server(tags={"models": [{"name": "small:latest"}, {"model": "other"}]}) as (c, _):
            self.assertFalse(c.health()["model_present"])
            self.assertIn("small:latest", c.health()["models"])

    def test_malformed_tags_are_empty_for_v09_compatibility(self):
        with server(tags={"models": {"oops": True}}) as (c, _):
            self.assertFalse(c.health()["model_present"])
            self.assertEqual(c.health()["models"], [])

    def test_nonstream_chat(self):
        with server(chat={"message": {"content": "done"}, "done": True}) as (c, handler):
            self.assertEqual(c.chat("s", "u"), "done")
            self.assertFalse(handler.captured[0]["stream"])

    def test_nonstream_false_done_fails(self):
        with server(chat={"message": {"content": "partial"}, "done": False}) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "finish"):
                c.chat("s", "u")

    def test_nonstream_oversized_reply(self):
        with server(chat={"message": {"content": "a" * (MAX_REPLY_BYTES + 1)}}) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "byte budget"):
                c.chat("s", "u")

    def test_http_error_fails(self):
        with server(status=500) as (c, _):
            with self.assertRaisesRegex(LLMUnavailable, "HTTP 500"):
                c.chat("s", "u")

    def test_bare_ollama_host_normalized(self):
        self.assertEqual(OllamaClient(base_url="127.0.0.1:11434").base_url,
                         "http://127.0.0.1:11434")

    def test_invalid_endpoints(self):
        for url in ("file:///etc/passwd", "http://u:p@127.0.0.1:11434",
                    "http://127.0.0.1:11434/api", "http://x/?token=secret", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaClient(base_url=url)

    def test_bad_timeout(self):
        for t in (0, float("nan"), float("inf"), -1, True):
            with self.subTest(timeout=t), self.assertRaises(ValueError):
                OllamaClient(timeout=t)

    def test_large_prompt_fails_before_network(self):
        with server() as (c, handler):
            with self.assertRaisesRegex(LLMUnavailable, "byte budget"):
                c.chat("s", "x" * (2 * 1024 * 1024 + 1))
            self.assertEqual(handler.captured, [])


if __name__ == "__main__":
    unittest.main()
