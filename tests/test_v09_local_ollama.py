"""v0.9: the OS talks to a real local Ollama correctly.

Regressions, each found by an edge probe:
- an HTTP(S)_PROXY in the environment routed 127.0.0.1 calls to the proxy, so a
  running local Ollama looked "unreachable" (4 suite tests failed on any proxied box)
- the built-in default qwen2.5-coder:32b was used even when not pulled, so auto
  mode failed every job on a box that has other local models
- Ollama's own error text ("model 'x' not found", mid-stream errors) was hidden
  behind "unreachable: HTTP Error 404"
- a non-object JSON line in the stream raised AttributeError, not LLMUnavailable
- OLLAMA_HOST in Ollama's own "host:port" / "0.0.0.0" form made an invalid URL
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os import llm
from crow_os.llm import LLMUnavailable, OllamaClient


class Stub(BaseHTTPRequestHandler):
    tags = ["qwen2.5:3b", "nomic-embed-text:latest"]
    chat_status = 200
    chat_body = None  # bytes; default: a normal answer
    stream_lines = None  # list[bytes] for stream=True
    seen_models: list = []

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, json.dumps({"models": [{"name": n} for n in type(self).tags]}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen_models.append(body.get("model"))
        if body.get("model") not in type(self).tags:
            self._send(404, json.dumps({"error": f"model '{body.get('model')}' not found"}).encode())
            return
        if body.get("stream") and type(self).stream_lines is not None:
            self._send(200, b"".join(type(self).stream_lines), "application/x-ndjson")
            return
        out = type(self).chat_body or json.dumps({"message": {"content": "hello"}, "done": True}).encode()
        self._send(type(self).chat_status, out)

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        Stub.tags = ["qwen2.5:3b", "nomic-embed-text:latest"]
        Stub.chat_status, Stub.chat_body, Stub.stream_lines = 200, None, None
        Stub.seen_models = []
        self.srv = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()


class ProxyBypass(Base):
    def test_loopback_ignores_dead_proxy(self):
        env = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
               "no_proxy": "", "NO_PROXY": ""}
        with mock.patch.dict(os.environ, env):
            c = OllamaClient(base_url=self.url, model="qwen2.5:3b", timeout=5)
            self.assertTrue(c.health()["model_present"])
            self.assertEqual(c.chat("s", "u"), "hello")

    def test_is_loopback(self):
        for u in ("http://127.0.0.1:11434", "http://localhost:1", "http://[::1]:1", "http://127.8.0.1"):
            self.assertTrue(llm.is_loopback(u), u)
        for u in ("http://10.0.0.5:11434", "http://ollama.example.com", "http://192.168.1.2"):
            self.assertFalse(llm.is_loopback(u), u)


class ErrorText(Base):
    def test_missing_model_says_not_found(self):
        c = OllamaClient(base_url=self.url, model="nope:1b", timeout=5)
        with self.assertRaises(LLMUnavailable) as cm:
            c.chat("s", "u")
        self.assertIn("not found", str(cm.exception))
        with self.assertRaises(LLMUnavailable) as cm:
            c.chat_stream("s", "u")
        self.assertIn("not found", str(cm.exception))

    def test_stream_error_line(self):
        Stub.stream_lines = [json.dumps({"message": {"content": "par"}}).encode() + b"\n",
                             json.dumps({"error": "out of memory"}).encode() + b"\n"]
        c = OllamaClient(base_url=self.url, model="qwen2.5:3b", timeout=5)
        with self.assertRaises(LLMUnavailable) as cm:
            c.chat_stream("s", "u")
        self.assertIn("out of memory", str(cm.exception))

    def test_stream_non_object_line(self):
        for bad in (b"[1,2]\n", b"\"text\"\n", b"{\"message\": \"str\"}\n", b"not json\n"):
            Stub.stream_lines = [bad]
            c = OllamaClient(base_url=self.url, model="qwen2.5:3b", timeout=5)
            with self.assertRaises(LLMUnavailable):  # never AttributeError/TypeError
                c.chat_stream("s", "u")

    def test_stream_stops_at_done(self):
        Stub.stream_lines = [json.dumps({"message": {"content": "a"}}).encode() + b"\n",
                             json.dumps({"message": {"content": "b"}, "done": True}).encode() + b"\n",
                             b"trailing garbage\n"]
        c = OllamaClient(base_url=self.url, model="qwen2.5:3b", timeout=5)
        toks = []
        self.assertEqual(c.chat_stream("s", "u", toks.append), "ab")
        self.assertEqual(toks, ["a", "b"])

    def test_nonstream_odd_bodies(self):
        c = OllamaClient(base_url=self.url, model="qwen2.5:3b", timeout=5)
        for body in (b"[]", b"null", b"{\"message\": [1]}", b"{\"error\": \"boom\"}"):
            Stub.chat_body = body
            with self.assertRaises(LLMUnavailable):
                c.chat("s", "u")

    def test_tags_odd_shapes(self):
        for tags in ([None, 5, {"name": 3}],):
            Stub.tags = tags
            h = OllamaClient(base_url=self.url, model="x").health()
            self.assertEqual(h["models"], [])


class ModelChoice(Base):
    def test_same_model_latest(self):
        self.assertTrue(llm.same_model("qwen2.5", "qwen2.5:latest"))
        self.assertFalse(llm.same_model("qwen2.5", "qwen2.5:3b"))

    def test_pick_prefers_family_and_skips_embeddings(self):
        self.assertEqual(llm.pick_local_model(["nomic-embed-text:latest", "zzz:1b", "llama3.2:3b", "qwen2.5:3b"]),
                         "qwen2.5:3b")
        self.assertEqual(llm.pick_local_model(["nomic-embed-text:latest"]), None)
        self.assertEqual(llm.pick_local_model([]), None)
        self.assertEqual(llm.pick_local_model(["a:1", "qwen2.5-coder:32b"], "qwen2.5-coder:32b"), "qwen2.5-coder:32b")

    def test_resolve_default_uses_installed(self):
        with mock.patch.object(llm, "MODEL_FROM_ENV", False):
            self.assertEqual(llm.resolve_model(None, self.url), "qwen2.5:3b")
            self.assertEqual(llm.resolve_model("  ", self.url), "qwen2.5:3b")

    def test_explicit_model_is_honored(self):
        self.assertEqual(llm.resolve_model("other:7b", self.url), "other:7b")
        with mock.patch.object(llm, "MODEL_FROM_ENV", True):
            self.assertEqual(llm.resolve_model(None, self.url), llm.DEFAULT_MODEL)

    def test_kernel_llm_run_on_installed_model(self):
        from crow_os.runtime import Kernel
        kernel = Kernel(Path(tempfile.mkdtemp()))
        with mock.patch.object(llm, "MODEL_FROM_ENV", False):
            job = kernel.run_llm("say hi", base_url=self.url)
        self.assertIn("qwen2.5:3b", Stub.seen_models)
        self.assertNotIn(llm.DEFAULT_MODEL, Stub.seen_models)
        self.assertIn(job["status"], ("completed", "failed"))
        self.assertNotIn("not found", job.get("error") or "")


class CoderRepair(Base):
    """A 3B local model dropped the plan reference line, so every live job failed at
    the tester with no error text. The coder is now asked once more; the check stays."""

    def _run(self, lines_per_call):
        calls = {"n": 0}

        class S(Stub):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                i = min(calls["n"], len(lines_per_call) - 1)
                calls["n"] += 1
                self._send(200, json.dumps({"message": {"content": lines_per_call[i]}, "done": True}).encode() + b"\n")

        self.srv.RequestHandlerClass = S
        from crow_os.runtime import Kernel
        k = Kernel(Path(tempfile.mkdtemp()))
        job = k.run_llm("add two numbers", model="qwen2.5:3b", base_url=self.url)
        return job, calls["n"], k

    def test_repair_recovers_missing_reference(self):
        job, n, k = self._run(["1. plan", "def add(a,b): return a+b", "{REF}\ndef add(a,b): return a+b"])
        self.assertEqual(n, 3)
        self.assertEqual(job["status"], "failed")  # "{REF}" is not the real reference
        self.assertIn("plan reference line", job.get("error", ""))
        kinds = [json.loads(l).get("kind") or json.loads(l).get("type") for l in
                 (k.home / "ledger.jsonl").read_text().splitlines()]
        self.assertIn("coder.repair", json.dumps(kinds))

    def test_repair_success(self):
        from crow_os import worker
        real = worker.LLMWorker.run
        seen = {}

        def run(self_, objective, cap, outputs=None, job_id=None):
            jid = self_.workflow.create_job(objective) if job_id is None else job_id
            seen["ref"] = f"plan:{jid}"
            return real(self_, objective, cap, outputs=outputs, job_id=jid)

        replies = ["1. plan", "def add(a,b): return a+b"]

        class S(Stub):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                r = replies.pop(0) if replies else f"# {seen['ref']}\ndef add(a,b): return a+b"
                self._send(200, json.dumps({"message": {"content": r}, "done": True}).encode() + b"\n")

        self.srv.RequestHandlerClass = S
        from crow_os.runtime import Kernel
        with mock.patch.object(worker.LLMWorker, "run", run):
            job = Kernel(Path(tempfile.mkdtemp())).run_llm("add", model="qwen2.5:3b", base_url=self.url)
        self.assertEqual(job["status"], "completed", job)


class AutoMode(Base):
    def _home(self):
        from crow_os.runtime import Kernel
        from crow_os.server import HomeServer
        return HomeServer(Kernel(Path(tempfile.mkdtemp())))

    def test_reachable_with_nothing_pulled_runs_demo(self):
        Stub.tags = ["nomic-embed-text:latest"]
        home = self._home()
        with mock.patch("crow_os.server.OllamaClient", lambda *a, **k: OllamaClient(base_url=self.url)), \
                mock.patch("crow_os.server.MODEL_FROM_ENV", False):
            info = home.model_info()
            self.assertTrue(info["reachable"])
            self.assertFalse(info["active_installed"])
            r = home.start_run("hello", "auto")
        self.assertEqual(r["mode"], "demo")

    def test_active_is_installed_model(self):
        home = self._home()
        with mock.patch("crow_os.server.OllamaClient", lambda *a, **k: OllamaClient(base_url=self.url)), \
                mock.patch("crow_os.server.MODEL_FROM_ENV", False):
            info = home.model_info()
        self.assertEqual(info["active"], "qwen2.5:3b")
        self.assertTrue(info["active_installed"])


class HostEnv(unittest.TestCase):
    def test_ollama_host_forms(self):
        f = llm._base_from_env
        self.assertEqual(f(None), "http://127.0.0.1:11434")
        self.assertEqual(f("127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(f("0.0.0.0"), "http://127.0.0.1:11434")
        self.assertEqual(f("0.0.0.0:8080"), "http://127.0.0.1:8080")
        self.assertEqual(f("https://ollama.example.com"), "https://ollama.example.com:11434")
        self.assertEqual(f("http://[::1]:11434"), "http://[::1]:11434")
        self.assertEqual(f('"localhost:1"'), "http://localhost:1")
        self.assertEqual(f("host:notaport"), "http://host:11434")


@unittest.skipUnless(os.environ.get("CROW_OS_LIVE_OLLAMA"), "set CROW_OS_LIVE_OLLAMA=1 with Ollama running")
class LiveOllama(unittest.TestCase):
    def test_live_chat(self):
        model = llm.resolve_model(None)
        c = OllamaClient(model=model, timeout=300)
        self.assertTrue(c.health()["model_present"], model)
        self.assertTrue(c.chat_stream("Reply with one word.", "Say: ready").strip())


if __name__ == "__main__":
    unittest.main()
