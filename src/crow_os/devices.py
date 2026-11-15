"""Devices & hosting (v0.10) — the machines Crow OS runs on and can
reach.

The local device profile is computed, never asserted: hostname,
platform, CPU count, and the OS-installed Ollama's live state. Other
devices (a second machine, a phone, a VM) are registered by the user
into the file system (/system/devices.json) so the HUD and the API
can show the whole hosting picture in one place. Registration is a
record, not a connection — Crow OS reports what it can verify (the
local profile, Ollama reachability) and labels the rest as
registered/unverified rather than pretending to be connected.
"""
from __future__ import annotations

import json
import platform
import socket
import time
from typing import Any

from .fs import CrowFS, FSError

DEVICES_FILE = "/system/devices.json"
MAX_DEVICES = 32


class DeviceError(Exception):
    pass


def local_profile(ollama: dict[str, Any] | None = None) -> dict[str, Any]:
    import os
    return {
        "id": "local",
        "name": socket.gethostname() or "local device",
        "kind": "host",
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "cpus": os.cpu_count() or 1,
        "ollama": ollama or {"reachable": False, "installed": []},
        "verified": True,
    }


class DeviceRegistry:
    def __init__(self, fs: CrowFS):
        self.fs = fs

    def _read(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.fs.read_file(DEVICES_FILE))
        except (FSError, json.JSONDecodeError):
            return []

    def register(self, name: str, kind: str = "device",
                 address: str = "") -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise DeviceError("device name is required")
        devices = self._read()
        if len(devices) >= MAX_DEVICES:
            raise DeviceError("device registry is full")
        device = {"id": f"dev-{int(time.time())}-{len(devices)}",
                  "name": name.strip()[:60], "kind": kind[:24] or "device",
                  "address": (address or "")[:120], "verified": False,
                  "registered": time.time()}
        devices.append(device)
        self.fs.write_file(DEVICES_FILE, json.dumps(devices, indent=2))
        return device

    def list(self, ollama: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [local_profile(ollama)] + self._read()
