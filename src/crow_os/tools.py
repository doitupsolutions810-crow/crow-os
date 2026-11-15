"""Network tooling for agents (v0.6) — capability-gated, SSRF-guarded.

The most dangerous sentence in an agent OS is "the model can browse".
So the network tools here are built like everything else in Crow OS:

- No capability, no network: every call authorizes `TOOL <name>`
  through the broker BEFORE any socket is opened.
- SSRF guard on every hop: scheme must be http/https, the host's
  resolved IPs must all be public (loopback, RFC1918, link-local,
  metadata and benchmarking ranges are refused), and redirects are
  re-validated hop by hop. (The same guard the daemon's recon tools
  apply — learned from the audit.)
- Bounded: 10s timeout, 64KB read cap, excerpt returned to the model
  is capped again. Failures are ToolError — callers fail closed.
- Attested: every invocation is written to the ledger by the caller
  (tool.invoked) with tool, host, status and byte count — never the
  fetched content itself.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .capabilities import Broker

MAX_BYTES = 65_536
EXCERPT_CHARS = 4_000
TIMEOUT = 10.0

TOOLS = ["net.fetch", "net.resolve", "fs.list", "fs.read", "fs.write",
         "agent.spawn"]


class ToolError(Exception):
    pass


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_blocked(ip_text: str) -> bool:
    """Only globally routable unicast addresses pass. is_private alone let
    100.64.0.0/10 (carrier-grade NAT) through, and IPv6 forms that embed an
    IPv4 address (::ffff:a.b.c.d, 64:ff9b::a.b.c.d NAT64, 2002::/16 6to4)
    are judged by the IPv4 address they reach."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return True
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return (not ip.is_global or ip.is_multicast or ip.is_private
            or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_unspecified)


def _resolve_checked(host: str, allow_private: bool) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve {host}: {e}") from e
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise ToolError(f"cannot resolve {host}: no addresses")
    if not allow_private:
        blocked = [ip for ip in ips if _ip_blocked(ip)]
        if blocked:
            raise ToolError(
                f"{host} resolves to a non-public address ({blocked[0]}); refused")
    return ips


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname
        if host:
            _resolve_checked(host, self._allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pinned_create_connection(allow_private: bool):
    """socket.create_connection that checks the address it actually connects
    to. Checking getaddrinfo once and then letting urllib resolve again left a
    DNS-rebinding window (public on check, 127.0.0.1 on connect)."""
    def create(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
               source_address=None, *a, **kw):
        host, port = address
        last = None
        for ip in _resolve_checked(host, allow_private):
            try:
                return socket.create_connection((ip, port), timeout, source_address)
            except OSError as e:
                last = e
        raise last or OSError(f"cannot connect to {host}")
    return create


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self._mk = _pinned_create_connection(allow_private)

    def http_open(self, req):
        mk = self._mk

        class Conn(http.client.HTTPConnection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._create_connection = mk
        return self.do_open(Conn, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self._mk = _pinned_create_connection(allow_private)

    def https_open(self, req):
        mk = self._mk

        class Conn(http.client.HTTPSConnection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._create_connection = mk
        return self.do_open(Conn, req, context=self._context)


class ToolRunner:
    """Executes network tools under a capability. One runner per run."""

    def __init__(self, broker: Broker, cap: dict[str, Any], *,
                 allow_private: bool = False, timeout: float = TIMEOUT):
        self.broker = broker
        self.cap = cap
        self.allow_private = allow_private
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    # -- net.resolve ----------------------------------------------------
    def resolve(self, host: str) -> dict[str, Any]:
        self.broker.authorize(self.cap, "TOOL", "net.resolve")
        ips = _resolve_checked(host, self.allow_private)
        result = {"tool": "net.resolve", "host": host, "ips": ips}
        self.calls.append(result)
        return result

    # -- net.fetch ------------------------------------------------------
    def fetch(self, url: str) -> dict[str, Any]:
        # The gate is checked BEFORE any network activity, always.
        self.broker.authorize(self.cap, "TOOL", "net.fetch")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"scheme not allowed: {parsed.scheme or '(none)'}")
        host = parsed.hostname
        if not host:
            raise ToolError("URL has no host")
        _resolve_checked(host, self.allow_private)
        if urllib.request.getproxies() and not urllib.request.proxy_bypass(host):
            # Egress goes through the configured proxy, which does its own DNS;
            # the hop-by-hop check above still applies.
            opener = urllib.request.build_opener(
                _GuardedRedirectHandler(self.allow_private))
        else:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _PinnedHTTPHandler(self.allow_private),
                _PinnedHTTPSHandler(self.allow_private),
                _GuardedRedirectHandler(self.allow_private))
        req = urllib.request.Request(
            url, headers={"User-Agent": "CrowOS/0.6 net.fetch"})
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                chunks, total = [], 0
                while total <= MAX_BYTES:
                    chunk = resp.read(min(8192, MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                final_url = resp.geturl()
        except ToolError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ToolError(f"fetch failed for {host}: {e}") from e
        body = b"".join(chunks)[:MAX_BYTES]
        result = {
            "tool": "net.fetch", "host": host, "url": url,
            "final_url": final_url, "status": status,
            "bytes": total, "truncated": total > MAX_BYTES,
            "excerpt": body.decode("utf-8", "replace")[:EXCERPT_CHARS],
        }
        self.calls.append(result)
        return result


class FileTools:
    """Capability-gated access to the OS file system (CrowFS).

    Same discipline as the network tools: the broker authorizes
    `TOOL fs.<op>` BEFORE any disk I/O, and every call is recorded
    for the caller to attest in the ledger.
    """

    def __init__(self, broker: Broker, cap: dict[str, Any], fs):
        self.broker = broker
        self.cap = cap
        self.fs = fs
        self.calls: list[dict[str, Any]] = []

    def list(self, vpath: str = "/") -> dict[str, Any]:
        self.broker.authorize(self.cap, "TOOL", "fs.list")
        result = {"tool": "fs.list", "path": vpath,
                  "entries": self.fs.list_dir(vpath)}
        self.calls.append({"tool": "fs.list", "path": vpath})
        return result

    def read(self, vpath: str) -> dict[str, Any]:
        self.broker.authorize(self.cap, "TOOL", "fs.read")
        content = self.fs.read_file(vpath)
        self.calls.append({"tool": "fs.read", "path": vpath,
                           "bytes": len(content.encode("utf-8"))})
        return {"tool": "fs.read", "path": vpath, "content": content}

    def write(self, vpath: str, content: str) -> dict[str, Any]:
        self.broker.authorize(self.cap, "TOOL", "fs.write")
        result = self.fs.write_file(vpath, content)
        record = {"tool": "fs.write", "path": result["path"],
                  "bytes": result["bytes"]}
        self.calls.append(record)
        return record
