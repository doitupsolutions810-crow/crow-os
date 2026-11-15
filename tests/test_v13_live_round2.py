"""Live round 2 findings on the agents' network tool and the agent registry.

- DNS rebinding: net.fetch checked getaddrinfo once, then urllib resolved
  again on connect, so a host that answers public-then-127.0.0.1 reached
  loopback. The connection now checks the address it actually dials.
- 100.64.0.0/10 (CGNAT) and IPv6 forms embedding IPv4 (NAT64, 6to4) passed.
- 12 parallel same-name agent creates gave 11 ids; two shared 'racer-2' and
  one request crashed (shared temp file in CrowFS.write_file).
- Unexpected handler errors dropped the connection instead of a 500.
"""
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os import tools  # noqa: E402
from crow_os.capabilities import Broker  # noqa: E402
from crow_os.runtime import Kernel  # noqa: E402


class Hit(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        Hit.hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"internal secret")

    def log_message(self, *a):
        pass


class Rebinding(unittest.TestCase):
    def test_rebind_to_loopback_is_refused_at_connect(self):
        if not hasattr(tools, "_pinned_create_connection"):
            self.fail("no connect-time address check (DNS rebinding window)")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Hit)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        real = socket.getaddrinfo
        calls = {"n": 0}

        def fake(host, *a, **kw):
            if host == "rebind.test":
                calls["n"] += 1
                ip = "93.184.215.14" if calls["n"] == 1 else "127.0.0.1"
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            return real(host, *a, **kw)

        k = Kernel(Path(tempfile.mkdtemp()))
        cap = k.broker.mint("t", [{"action": "TOOL", "resource": "net.fetch"}]) \
            if hasattr(k.broker, "mint") else None
        runner = tools.ToolRunner(k.broker, cap)
        runner.broker = mock.Mock()  # gate is tested elsewhere; this is the socket path
        env = {k_: v for k_, v in os.environ.items() if "proxy" not in k_.lower()}
        Hit.hits = 0
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("socket.getaddrinfo", fake):
            with self.assertRaises(tools.ToolError):
                runner.fetch(f"http://rebind.test:{port}/")
        srv.shutdown()
        self.assertEqual(Hit.hits, 0, "loopback server was reached")

    def test_blocked_ranges(self):
        for ip in ("100.64.1.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "::ffff:127.0.0.1",
                   "192.0.0.1", "198.18.0.1", "fe80::1%eth0", "169.254.169.254", "0.0.0.0"):
            self.assertTrue(tools._ip_blocked(ip), ip)
        for ip in ("8.8.8.8", "2606:4700:4700::1111"):
            self.assertFalse(tools._ip_blocked(ip), ip)


class Registry(unittest.TestCase):
    def test_parallel_same_name_creates_get_distinct_ids(self):
        k = Kernel(Path(tempfile.mkdtemp()))
        out, errs = [], []

        def mk(i):
            try:
                out.append(k.agents.create("Racer", f"racer {i}")["id"])
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=mk, args=(i,)) for i in range(24)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(set(out)), 24)
        self.assertEqual(len({a["instructions"] for a in k.agents.list()}), 24)

    def test_concurrent_writes_of_one_file_never_crash(self):
        k = Kernel(Path(tempfile.mkdtemp()))
        errs = []

        def w(i):
            try:
                for _ in range(20):
                    k.fs.write_file("/home/same.txt", f"v{i}")
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertFalse([p for p in (k.fs.resolve("/home")).iterdir() if p.name.endswith(".tmp")])


class Attribution(unittest.TestCase):
    def test_kernel_artifact_save_is_attributed_to_kernel(self):
        k = Kernel(Path(tempfile.mkdtemp()))
        k.outputs["j1"] = {"artifact": "hello"}
        k._persist_artifact("j1")
        writes = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"
                  and e["raw"].get("tool") == "fs.write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["raw"].get("by"), "kernel")


class Handler500(unittest.TestCase):
    def test_unexpected_error_is_500_json_not_dropped(self):
        import json
        import urllib.error
        import urllib.request
        from crow_os.server import HomeServer, make_handler
        home = HomeServer(Kernel(Path(tempfile.mkdtemp())))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(home))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with mock.patch.object(home.kernel.agents, "list", side_effect=RuntimeError("boom")):
            try:
                op.open(f"http://127.0.0.1:{httpd.server_address[1]}/api/agents", timeout=5)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
                body = json.loads(e.read())
                self.assertNotIn("boom", json.dumps(body))
            except Exception as e:  # noqa: BLE001
                code = repr(e)
        httpd.shutdown()
        self.assertEqual(code, 500)


if __name__ == "__main__":
    unittest.main()
