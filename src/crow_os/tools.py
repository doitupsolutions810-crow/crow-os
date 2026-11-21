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
import urllib.parse
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
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True

    # Do not rely only on is_private: Python correctly reports CGNAT
    # (100.64/10) as neither private nor globally routable.
    if not ip.is_global:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None and _ip_blocked(str(mapped)):
            return True
        six_to_four = ip.sixtofour
        if six_to_four is not None and _ip_blocked(str(six_to_four)):
            return True
        # Well-known NAT64 embeds an IPv4 destination in the low 32 bits.
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xffffffff)
            if _ip_blocked(str(embedded)):
                return True
        # Local-use translation and Teredo are not accepted as public origins.
        if ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return True
        if ip in ipaddress.ip_network("2001::/32"):
            return True
    return False


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


def _pinned_create_connection(
    host: str,
    port: int,
    addresses: list[str],
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect to a previously validated literal IP without resolving host again."""
    errors: list[OSError] = []
    for address_text in addresses:
        try:
            address = ipaddress.ip_address(address_text.split("%", 1)[0])
        except ValueError as exc:
            raise ToolError("validated address became invalid") from exc
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        if isinstance(timeout, (int, float)):
            sock.settimeout(float(timeout))
        try:
            if source_address:
                sock.bind(source_address)
            sockaddr = ((str(address), port, 0, 0) if address.version == 6
                        else (str(address), port))
            # Passing a numeric sockaddr directly is the security boundary:
            # socket.connect does not call getaddrinfo for the hostname.
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise ToolError(f"{host} has no validated address to connect to")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, addresses: list[str], **kwargs):
        self._pinned_addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = _pinned_create_connection(
            self.host, self.port, self._pinned_addresses,
            timeout=self.timeout, source_address=self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: list[str], **kwargs):
        self._pinned_addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        raw = _pinned_create_connection(
            self.host, self.port, self._pinned_addresses,
            timeout=self.timeout, source_address=self.source_address)
        if self._tunnel_host:
            self.sock = raw
            self._tunnel()
            raw = self.sock
        self.sock = self._context.wrap_socket(
            raw, server_hostname=self._tunnel_host or self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pins: dict[str, list[str]]):
        super().__init__()
        self._pins = pins

    def http_open(self, request):
        host = urllib.parse.urlsplit(request.full_url).hostname
        addresses = self._pins.get(host or "")
        if not addresses:
            raise ToolError("refusing an HTTP connection without a validated address")
        return self.do_open(
            lambda target, **kwargs: _PinnedHTTPConnection(target, addresses, **kwargs),
            request)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pins: dict[str, list[str]]):
        super().__init__()
        self._pins = pins

    def https_open(self, request):
        host = urllib.parse.urlsplit(request.full_url).hostname
        addresses = self._pins.get(host or "")
        if not addresses:
            raise ToolError("refusing an HTTPS connection without a validated address")
        return self.do_open(
            lambda target, **kwargs: _PinnedHTTPSConnection(target, addresses, **kwargs),
            request, context=self._context, check_hostname=self._check_hostname)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool, pins: dict[str, list[str]]):
        super().__init__()
        self._allow_private = allow_private
        self._pins = pins

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("redirect target is not an HTTP(S) URL with a host")
        self._pins[parsed.hostname] = _resolve_checked(
            parsed.hostname, self._allow_private)
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
        pins = {host: _resolve_checked(host, self.allow_private)}
        # Disable ambient proxies and dial the validated address directly.
        # Redirects add a freshly validated pin before the next connection.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PinnedHTTPHandler(pins),
            _PinnedHTTPSHandler(pins),
            _GuardedRedirectHandler(self.allow_private, pins))
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
