"""End-to-end HTTP validation for custom agent naming and chat intake."""
from __future__ import annotations

import contextlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.runtime import Kernel
from crow_os.server import HomeServer, make_handler


@contextlib.contextmanager
def local_server():
    tmp = tempfile.TemporaryDirectory()
    kernel = Kernel(Path(tmp.name))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(HomeServer(kernel)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", kernel
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        tmp.cleanup()


def request(base, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class AgentHTTPTests(unittest.TestCase):
    def test_create_name_and_multiline_custom_instructions_are_persistent(self):
        with local_server() as (base, kernel):
            request_body = {"name": "Network Edge Auditor",
                            "instructions": "Review network redirects.\nExplain all edge cases.",
                            "tools": ["net.fetch", "net.resolve"],
                            "avatar": {"shape": "star", "color": "teal"}}
            code, result = request(base, "/api/agents", request_body)
            self.assertEqual(code, 201, result)
            agent = result["agent"]
            self.assertEqual(agent["name"], request_body["name"])
            self.assertEqual(agent["instructions"], request_body["instructions"])
            self.assertEqual(agent["tools"], request_body["tools"])
            self.assertEqual(kernel.agents.get(agent["id"])["avatar"],
                             request_body["avatar"])
            with urllib.request.urlopen(base + "/api/agents", timeout=5) as response:
                data = json.loads(response.read())
            self.assertEqual(data["agents"][0]["id"], agent["id"])

    def test_duplicate_display_names_do_not_replace_instructions(self):
        with local_server() as (base, kernel):
            a, first = request(base, "/api/agents", {
                "name": "Researcher", "instructions": "First policy"})
            b, second = request(base, "/api/agents", {
                "name": "Researcher", "instructions": "Second policy"})
            self.assertEqual((a, b), (201, 201))
            self.assertNotEqual(first["agent"]["id"], second["agent"]["id"])
            self.assertEqual(kernel.agents.get(first["agent"]["id"])["instructions"],
                             "First policy")

    def test_non_object_payloads_are_400_not_500(self):
        with local_server() as (base, kernel):
            for path in ("/api/agents", "/api/run", "/api/spawn", "/api/plugins",
                         "/api/devices"):
                for value in ([], ["bad"], None, "hello", 3):
                    with self.subTest(path=path, value=value):
                        code, response = request(base, path, value)
                        self.assertEqual(code, 400)
                        self.assertIn("error", response)
            self.assertEqual(len(kernel.agents.list()), 0)
            self.assertEqual(len(kernel.workflow.jobs()), 0)

    def test_malformed_tools_avatar_and_model_fail_without_writes(self):
        with local_server() as (base, kernel):
            for key, value in (("tools", "net.fetch"), ("tools", 42),
                               ("tools", [["net.fetch"]]), ("avatar", ["star"]),
                               ("avatar", {"shape": []}), ("model", {"not": "a model"})):
                with self.subTest(key=key, value=value):
                    code, result = request(base, "/api/agents", {
                        "name": "Bad Agent", "instructions": "Test", key: value})
                    self.assertEqual(code, 400)
            self.assertEqual(len(kernel.agents.list()), 0)

    def test_oversized_request_is_rejected_before_processing(self):
        with local_server() as (base, kernel):
            code, result = request(base, "/api/agents", {
                "name": "Oversize", "instructions": "X" * 70000})
            self.assertEqual(code, 400)
            self.assertEqual(kernel.agents.list(), [])

    def test_no_model_call_from_agent_registration(self):
        with local_server() as (base, kernel):
            code, result = request(base, "/api/agents", {
                "name": "No Inference Yet", "instructions": "Run only when asked",
                "tools": ["net.fetch"]})
            self.assertEqual(code, 201)
            self.assertEqual(len(kernel.workflow.jobs()), 0)
            self.assertTrue(kernel.ledger.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
