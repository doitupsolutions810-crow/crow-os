"""Real loopback HTTP contract and persisted custom-agent regression checks.
No model provider is stubbed or invoked by these checks.
"""
import json
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

from crow_os.agents import AgentError
from crow_os.runtime import Kernel
from crow_os.server import HomeServer, make_handler


class WorkspaceAgents(unittest.TestCase):
    def test_http_custom_instructions_and_grants_survive_reload(self):
        with tempfile.TemporaryDirectory() as td:
            kernel = Kernel(Path(td))
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(HomeServer(kernel)))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            instructions = "Hunt edge cases.\nRetain evidence and stop on failed acceptance gates."
            payload = {"name": "Workspace Edge Hunter", "instructions": instructions,
                       "model": "qwen2.5:3b", "tools": ["net.fetch"]}
            try:
                request = urllib.request.Request(base + "/api/agents", data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with opener.open(request, timeout=5) as response:
                    self.assertEqual(response.status, 201)
                    created = json.load(response)["agent"]
                with opener.open(base + "/api/agents", timeout=5) as response:
                    roster = json.load(response)["agents"]
                self.assertEqual(len(roster), 1)
                self.assertEqual(roster[0]["instructions"], instructions)
                reloaded = Kernel(Path(td)).agents.get(created["id"])
                self.assertEqual(reloaded["name"], payload["name"])
                self.assertEqual(reloaded["instructions"], instructions)
                self.assertEqual(reloaded["tools"], payload["tools"])
                self.assertEqual(reloaded["model"], payload["model"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)

    def test_malformed_avatar_members_fail_without_partial_agent(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Kernel(Path(td)).agents
            for avatar in ({"color": []}, {"shape": {}}, {"color": 3}, {"shape": False}):
                with self.subTest(avatar=avatar), self.assertRaises(AgentError):
                    registry.create("Invalid avatar", "Inspect evidence", avatar=avatar)
            self.assertEqual(registry.list(), [])

    def test_failed_reason_survives_ledger_reload(self):
        with tempfile.TemporaryDirectory() as td:
            kernel = Kernel(Path(td))
            job_id = kernel.workflow.create_job("Inspect a failed verification")
            kernel.workflow.report(job_id, "debugger", False, "Provider unavailable; verification blocked")
            restored = Kernel(Path(td)).workflow.get(job_id)
            self.assertEqual(restored["status"], "failed")
            self.assertEqual(restored["failed_role"], "debugger")
            self.assertIn("Provider unavailable", restored["error"])

    def test_runtime_uses_captured_custom_agent_after_delete(self):
        with tempfile.TemporaryDirectory() as td:
            kernel = Kernel(Path(td))
            agent = kernel.agents.create("Captured Agent", "Retain these exact instructions")
            home = HomeServer(kernel)
            pending = []
            class DeferredThread:
                def __init__(self, target, args=(), **kwargs):
                    self.target, self.args = target, args
                def start(self):
                    pending.append(self)
            with mock.patch("crow_os.server.threading.Thread", DeferredThread), mock.patch.object(kernel, "run_agent", return_value={"status": "failed"}) as run:
                home.start_run("Inspect exact captured configuration", "llm", agent_id=agent["id"])
                kernel.agents.delete(agent["id"])
                pending[0].target(*pending[0].args)
                self.assertEqual(run.call_args.kwargs["agent"], agent)
