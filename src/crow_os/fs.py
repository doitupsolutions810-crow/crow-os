"""CrowFS — the Crow OS file system.

Every OS needs a file system; this is Crow OS's. It lives at
`<home>/fs` and speaks virtual paths ("/home/user/Documents") that
can never name a host file:

- Paths are normalized inside the virtual root; ".." that would
  escape is refused, and every resolved path — symlinks included —
  must stay under the root. A virtual "/etc/passwd" is a path inside
  the sandbox that simply doesn't exist, never the host's file.
- Writes are atomic (temp file in the same directory + os.replace)
  and size-capped; reads are size-capped; writing onto a directory
  or reading one is an error.
- Agent access goes through capability-gated tools (see tools.py:
  TOOL fs.list / fs.read / fs.write, checked before any disk I/O and
  attested in the ledger). The operator CLI uses CrowFS directly.

Standard tree (created on first boot):
  /system            OS files (VERSION, README)
  /home/user/        Documents, Downloads, Pictures, Music
  /apps              installed app bundles
  /var/log, /var/cache, /tmp
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

MAX_FILE_BYTES = 262_144  # 256KB

TREE = [
    "system",
    "home/user/Documents",
    "home/user/Downloads",
    "home/user/Pictures",
    "home/user/Music",
    "apps",
    "var/log",
    "var/cache",
    "tmp",
]


class FSError(Exception):
    pass


class CrowFS:
    def __init__(self, home: Path):
        self.root = (home / "fs")
        self.root.mkdir(parents=True, exist_ok=True)
        self._root_real = self.root.resolve()
        for d in TREE:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        version = self.root / "system" / "VERSION"
        if not version.exists():
            from . import __version__
            version.write_text(f"crow-os {__version__}\n", encoding="utf-8")
        readme = self.root / "system" / "README"
        if not readme.exists():
            readme.write_text(
                "This is the Crow OS file system. Agent access is "
                "capability-gated and attested in the signed ledger.\n",
                encoding="utf-8")

    # -- path safety ------------------------------------------------------
    def resolve(self, vpath: str) -> Path:
        """Map a virtual path to a real path provably inside the root."""
        if not isinstance(vpath, str) or not vpath:
            raise FSError("path must be a non-empty string")
        if "\x00" in vpath:
            raise FSError("NUL byte in path")
        parts: list[str] = []
        for part in vpath.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise FSError(f"path escapes the file system: {vpath}")
                parts.pop()
            else:
                parts.append(part)
        candidate = self.root.joinpath(*parts) if parts else self.root
        real = candidate.resolve()
        if real != self._root_real and self._root_real not in real.parents:
            raise FSError(f"path escapes the file system: {vpath}")
        return real

    def virtual(self, real: Path) -> str:
        rel = real.resolve().relative_to(self._root_real)
        return "/" + "/".join(rel.parts) if rel.parts else "/"

    # -- operations -------------------------------------------------------
    def list_dir(self, vpath: str = "/") -> list[dict]:
        real = self.resolve(vpath)
        if not real.is_dir():
            raise FSError(f"not a directory: {vpath}")
        out = []
        for child in sorted(real.iterdir(), key=lambda p: p.name):
            try:
                st = child.stat()
                out.append({"name": child.name,
                            "type": "dir" if child.is_dir() else "file",
                            "size": st.st_size if child.is_file() else 0})
            except OSError:
                continue
        return out

    def read_file(self, vpath: str) -> str:
        real = self.resolve(vpath)
        if not real.exists():
            raise FSError(f"no such file: {vpath}")
        if real.is_dir():
            raise FSError(f"is a directory: {vpath}")
        if real.stat().st_size > MAX_FILE_BYTES:
            raise FSError(f"file too large to read: {vpath}")
        return real.read_bytes().decode("utf-8", "replace")

    def write_file(self, vpath: str, content: str) -> dict:
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FSError(f"content too large: {len(data)} > {MAX_FILE_BYTES} bytes")
        real = self.resolve(vpath)
        if real.exists() and real.is_dir():
            raise FSError(f"is a directory: {vpath}")
        real.parent.mkdir(parents=True, exist_ok=True)
        # The parent chain was just (re)created — re-verify containment.
        parent_real = real.parent.resolve()
        if parent_real != self._root_real and self._root_real not in parent_real.parents:
            raise FSError(f"path escapes the file system: {vpath}")
        # A unique temp name: the old fixed ".<name>.tmp" made two concurrent
        # writes of one file race on the same temp, and one os.replace failed.
        tmp = real.parent / f".{real.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, real)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"path": self.virtual(real), "bytes": len(data)}

    def create_file(self, vpath: str, content: str) -> dict:
        """Write a NEW file atomically; FileExistsError if the name is taken.
        os.link of a finished temp file never overwrites and never exposes a
        half-written file, so callers can claim a name without a check-then-write race."""
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FSError(f"content too large: {len(data)} > {MAX_FILE_BYTES} bytes")
        real = self.resolve(vpath)
        real.parent.mkdir(parents=True, exist_ok=True)
        parent_real = real.parent.resolve()
        if parent_real != self._root_real and self._root_real not in parent_real.parents:
            raise FSError(f"path escapes the file system: {vpath}")
        tmp = real.parent / f".{real.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_bytes(data)
            os.link(tmp, real)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"path": self.virtual(real), "bytes": len(data)}

    def mkdir(self, vpath: str) -> dict:
        real = self.resolve(vpath)
        real.mkdir(parents=True, exist_ok=True)
        if not real.is_dir():
            raise FSError(f"not a directory: {vpath}")
        return {"path": self.virtual(real)}

    def exists(self, vpath: str) -> bool:
        try:
            return self.resolve(vpath).exists()
        except FSError:
            return False
