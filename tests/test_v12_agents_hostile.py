"""Found while driving a live Crow OS server on local Ollama: hostile agent
definitions and non-object JSON bodies crashed handler threads (the client got a
dropped connection, never a 400), and a run's agent id could name any .json in
the user file system."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.agents import AgentError  # noqa: E402
from crow_os.runtime import Kernel  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AgentValidation(unittest.TestCase):
    def setUp(self):
        self.k = Kernel(Path(tempfile.mkdtemp()))
        self.reg = self.k.agents

    def bad(self, **kw):
        base = {"name": "x", "instructions": "y"}
        base.update(kw)
        with self.assertRaises(AgentError):
            self.reg.create(base.pop("name"), base.pop("instructions"), **base)

    def test_user_can_create_named_agent_with_instructions(self):
        a = self.reg.create("Edge Hunter", "Hunt edge cases.\nThen report.",
                            tools=["net.fetch", "net.resolve"])
        self.assertEqual((a["id"], a["name"]), ("edge-hunter", "Edge Hunter"))
        self.assertEqual(self.reg.get("edge-hunter")["instructions"], "Hunt edge cases.\nThen report.")

    def test_avatar_must_be_object(self):
        self.bad(avatar="red")
        self.bad(avatar=[1])

    def test_model_must_be_a_model_name(self):
        self.bad(model=5)
        self.bad(model="qwen\x00")
        self.bad(model="a b")
        self.assertEqual(self.reg.create("m", "y", model="qwen2.5:3b")["model"], "qwen2.5:3b")

    def test_tools_must_be_list(self):
        self.bad(tools={"net.fetch": 1})
        self.bad(tools="net.fetch")

    def test_control_chars_rejected(self):
        self.bad(name="a\x00b")
        self.bad(instructions="ok\x1b[31m")

    def test_agent_id_cannot_escape_agents_dir(self):
        self.k.fs.write_file("/home/notes.json", json.dumps({"name": "evil", "instructions": "x"}))
        for bad in ("../../home/notes", "..", "", None, ["x"], "A", "x/y"):
            with self.assertRaises(AgentError):
                self.reg.get(bad)
            with self.assertRaises(AgentError):
                self.reg.delete(bad)
        self.assertTrue(self.k.fs.exists("/home/notes.json"))

    def test_list_survives_hand_edited_definitions(self):
        self.reg.create("ok", "fine")
        self.k.fs.write_file("/system/agents/nocreated.json", json.dumps({"id": "nocreated", "name": "n", "instructions": "i"}))
        self.k.fs.write_file("/system/agents/listy.json", "[1, 2]")
        names = [a["name"] for a in self.reg.list()]
        self.assertIn("ok", names)
        self.assertIn("n", names)


class NonObjectBodies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from crow_os.server import HomeServer, make_handler
        cls.home = HomeServer(Kernel(Path(tempfile.mkdtemp())))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.home))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def post(self, path, raw):
        r = urllib.request.Request(self.base + path, data=raw, method="POST",
                                   headers={"Content-Type": "application/json"})
        try:
            with OPENER.open(r, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception as e:  # dropped connection = crashed handler
            return repr(e)

    def test_every_post_route_400s_on_non_object(self):
        for path in ("/api/run", "/api/agents", "/api/spawn", "/api/devices", "/api/plugins"):
            for raw in (b"[1,2]", b'"s"', b"null", b"7"):
                self.assertEqual(self.post(path, raw), 400, (path, raw))

    def test_agent_bad_avatar_is_400_not_crash(self):
        self.assertEqual(self.post("/api/agents", json.dumps(
            {"name": "x", "instructions": "y", "avatar": "red"}).encode()), 400)


if __name__ == "__main__":
    unittest.main()
