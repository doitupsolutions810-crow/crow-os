"""Additional trust-boundary regression tests for the persistent agent registry."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.agents import AgentError
from crow_os.runtime import Kernel
from crow_os.tools import FileTools, ToolError


class AgentTrustTests(unittest.TestCase):
    def setUp(self):
        self.kernel = Kernel(Path(tempfile.mkdtemp()))

    def test_hand_edited_agent_cannot_add_unregistered_tool_grants(self):
        agent = self.kernel.agents.create("Safe Agent", "Read only work.")
        path = f"/system/agents/{agent['id']}.json"
        raw = {
            "id": agent["id"],
            "name": "Safe Agent",
            "instructions": "Read only work.",
            "model": None,
            "tools": ["kernel.signing-key.export"],
            "created": agent["created"],
        }
        self.kernel.fs.write_file(path, json.dumps(raw))
        with self.assertRaises(AgentError):
            self.kernel.agents.get(agent["id"])
        self.assertNotIn(agent["id"], [item["id"] for item in self.kernel.agents.list()])

    def test_agent_with_fs_write_cannot_modify_system_or_agent_registry(self):
        agent = self.kernel.agents.create("Writer", "Write user documents.", tools=["fs.write"])
        cap = self.kernel.broker.issue(
            "test/agent-writer",
            [{"method": "TOOL", "resource": "fs.write"}],
            ttl_seconds=60,
            rate_limit=10,
        )
        file_tools = FileTools(self.kernel.broker, cap, self.kernel.fs)
        payload = json.dumps({
            "id": agent["id"],
            "name": "Privilege Escalation",
            "instructions": "Grant every tool.",
            "tools": ["net.fetch", "net.resolve", "fs.write"],
        })
        for path in (
            "/system/agents/writer.json",
            "/system/README",
            "/system/agents/../VERSION",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ToolError):
                    file_tools.write(path, payload)
        self.assertEqual(self.kernel.agents.get(agent["id"])["name"], "Writer")
        self.assertFalse(self.kernel.fs.exists("/system/VERSION.tmp"))

    def test_registry_accepts_only_tools_in_trusted_live_catalog(self):
        self.kernel.agents.set_extra_tools_provider(lambda: ["connector.calendar.read"])
        allowed = self.kernel.agents.create(
            "Connector Agent",
            "Read calendar events.",
            tools=["connector.calendar.read"],
            extra_tools=["connector.calendar.read"],
        )
        self.assertEqual(self.kernel.agents.get(allowed["id"])["tools"],
                         ["connector.calendar.read"])
        with self.assertRaises(AgentError):
            self.kernel.agents.create(
                "Invalid Connector Agent",
                "Request a made-up tool.",
                tools=["kernel.admin"],
                extra_tools=["kernel.admin"],
            )

    def test_invalid_agent_ids_are_rejected_before_filesystem_resolution(self):
        for value in ("../VERSION", "../../home/user/secret", "Agent", "x/y", "", None, ["x"]):
            with self.subTest(value=value):
                with self.assertRaises(AgentError):
                    self.kernel.agents.get(value)
                with self.assertRaises(AgentError):
                    self.kernel.agents.delete(value)


if __name__ == "__main__":
    unittest.main()
