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


def _ip_blocked(ip_text: str) -> bool:
    # Scoped addresses are meaningful only on a host's local interfaces.
    if "%" in ip_text:
        return True
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # Translation/tunnel addresses must not smuggle a non-public IPv4
        # destination past the IPv6 classification.
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is not None and _ip_blocked(str(embedded)):
            return True
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            if _ip_blocked(str(ipaddress.IPv4Address(int(ip) & 0xffffffff))):
                return True
        if ip in ipaddress.ip_network("64:ff9b:1::/48") or ip.teredo:
            return True
    return False


def _pinned_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                              source_address=None, *, allow_private=False):
    """Resolve once at connect time, validate all answers, dial numeric tuples.

    No second hostname resolution occurs between the check and socket.connect.
    This is per connection rather than a global socket monkeypatch, so other
    threads and explicitly opted-in private tools retain their own policy.
    """
    host, port = address
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve {host}: {e}") from e
    if not infos:
        raise ToolError(f"cannot resolve {host}: no addresses")
    if not allow_private:
        for _, _, _, _, sockaddr in infos:
            if _ip_blocked(sockaddr[0]):
                raise ToolError(f"{host} resolves to a non-public address ({sockaddr[0]}); refused")
    last_error = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last_error = e
            if sock is not None:
                sock.close()
    raise last_error or OSError("no usable addresses")


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self._allow_private = allow_private

    def http_open(self, req):
        def connection(*args, **kwargs):
            conn = http.client.HTTPConnection(*args, **kwargs)
            conn._create_connection = lambda *a, **kw: _pinned_create_connection(
                *a, **kw, allow_private=self._allow_private)
            return conn
        return self.do_open(connection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allow_private: bool):
        super().__init__()
        self._allow_private = allow_private

    def https_open(self, req):
        def connection(*args, **kwargs):
            conn = http.client.HTTPSConnection(*args, **kwargs)
            conn._create_connection = lambda *a, **kw: _pinned_create_connection(
                *a, **kw, allow_private=self._allow_private)
            return conn
        # Keep the original hostname for TLS SNI and certificate validation.
        return self.do_open(connection, req, context=self._context)


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
        opener = urllib.request.build_opener(
            # Proxy endpoints would hide the actual target address from this
            # connection guard. Network tools always connect directly.
            urllib.request.ProxyHandler({}),
            _GuardedHTTPHandler(self.allow_private),
            _GuardedHTTPSHandler(self.allow_private),
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
