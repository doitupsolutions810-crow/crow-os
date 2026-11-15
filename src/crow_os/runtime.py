"""Crow OS kernel: ledger + capabilities + workflow, one honest demo run.

The demo workers do real checks instead of rubber-stamping:
- debugger: turns the objective into a plan with acceptance criteria
- coder: produces an artifact that must reference the plan
- tester: actually runs the criteria against the artifact
- governance: verifies the ledger chain and the capability before
  allowing completion
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .agents import AgentRegistry
from .capabilities import Broker
from .devices import DeviceRegistry
from .plugins import PluginRegistry
from .fs import CrowFS
from .ledger import Ledger
from .signers import HmacSigner
from .llm import DEFAULT_BASE, DEFAULT_MODEL, OllamaClient, resolve_model  # noqa: F401
from .workflow import Workflow
from .worker import LLMWorker

SECRET_FILE = "broker.secret"


class Kernel:
    def __init__(self, home: Path, *, allow_private_tools: bool = False):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        secret_path = self.home / SECRET_FILE
        if not secret_path.exists():
            secret_path.write_bytes(hashlib.sha256(
                f"crow-os:{self.home.resolve()}".encode()).digest())
        secret = secret_path.read_bytes()
        self.ledger = Ledger(self.home / "ledger.jsonl", signer=HmacSigner(secret))
        if not self.ledger.path.exists():
            # A kernel home always has its ledger: an absent file then
            # means external deletion, which verify() rightly treats as
            # an error, while a fresh home reads as an empty, valid chain.
            self.ledger.path.touch()
        self.broker = Broker(secret, state_path=self.home / "broker_state.json")
        self.workflow = Workflow(self.ledger)
        self.outputs: dict[str, dict[str, str]] = {}  # job_id -> plan/artifact (for the Home Screen code view)
        self.fs = CrowFS(home)  # the OS file system (<home>/fs)
        self.agents = AgentRegistry(self.fs)  # user-created agents
        self.devices = DeviceRegistry(self.fs)  # hosting devices
        self.plugins = PluginRegistry(self.fs)  # connect-apps catalog
        self._spawn_depth: dict[str, int] = {}  # root job_id -> depth
        # Operator's explicit choice: may agent tools reach non-public
        # addresses (intranet)? Default NO — the SSRF guard refuses
        # loopback/RFC1918/link-local/metadata destinations.
        self.allow_private_tools = allow_private_tools

    def _persist_artifact(self, job_id: str) -> None:
        """Save a completed job's artifact into the OS file system,
        through the capability-gated fs tool, attested in the ledger."""
        artifact = (self.outputs.get(job_id) or {}).get("artifact", "")
        if not artifact:
            return
        from .tools import FileTools
        cap = self.broker.issue(
            "crow-os/kernel",
            [{"method": "TOOL", "resource": "fs.write"}],
            ttl_seconds=60, rate_limit=4)
        tools = FileTools(self.broker, cap, self.fs)
        try:
            rec = tools.write(f"/home/user/Documents/{job_id}.md", artifact)
        except Exception:
            return  # best-effort; the job already completed
        self.ledger.append("crow-os/tools", "tool.invoked", {
            "job_id": job_id, "tool": rec["tool"], "path": rec["path"],
            "bytes": rec["bytes"],
            # The kernel saved the artifact under its own short-lived cap. Without
            # this, an agent with no fs.write grant looked like it had written.
            "by": "kernel"})
        self.outputs[job_id]["saved_to"] = rec["path"]

    def run_llm(self, objective: str, model: str | None = None,
                base_url: str | None = None,
                job_id: str | None = None,
                tools_enabled: bool = True) -> dict[str, Any]:
        """Full chain with an LLM behind the capability gate (v0.2)."""
        base = base_url or DEFAULT_BASE
        client = OllamaClient(base_url=base, model=resolve_model(model, base))
        cap = self.broker.issue(
            "crow-os/kernel",
            [{"method": "EXECUTE", "resource": "jobs.debugger"},
             {"method": "EXECUTE", "resource": "jobs.coder"},
             {"method": "TOOL", "resource": "net.fetch"},
             {"method": "TOOL", "resource": "net.resolve"}],
            ttl_seconds=900, rate_limit=10)
        from .tools import ToolRunner
        worker = LLMWorker(self.broker, self.workflow, self.ledger, client,
                           tools=ToolRunner(self.broker, cap,
                                            allow_private=self.allow_private_tools)
                           if tools_enabled else None)
        job = worker.run(objective, cap, outputs=self.outputs, job_id=job_id)
        if job["status"] == "completed":
            self._persist_artifact(job["job_id"])
        return job

    MAX_SPAWN_DEPTH = 2

    def spawn_subagent(self, parent_job_id: str, agent_id: str,
                       objective: str, base_url: str | None = None,
                       model: str | None = None) -> dict[str, Any]:
        """Spawn a sub-agent job from a parent job. The spawn itself is
        attested (agent.spawned links parent -> child in the signed
        ledger) and depth-capped, so delegation trees stay shallow
        and auditable instead of recursing forever."""
        root = parent_job_id
        depth = self._spawn_depth.get(parent_job_id, 0) + 1
        if depth > self.MAX_SPAWN_DEPTH:
            raise ValueError(
                f"spawn depth cap ({self.MAX_SPAWN_DEPTH}) reached")
        self.agents.get(agent_id)  # validate BEFORE attesting the spawn
        child_id = self.workflow.create_job(
            objective, chain=[agent_id, "governance"])
        self._spawn_depth[child_id] = depth
        self.ledger.append("crow-os/kernel", "agent.spawned", {
            "parent_job_id": parent_job_id, "child_job_id": child_id,
            "agent": agent_id, "depth": depth})
        return self.run_agent(objective, agent_id, model=model,
                              base_url=base_url, job_id=child_id)

    def run_agent(self, objective: str, agent_id: str,
                  model: str | None = None, base_url: str | None = None,
                  job_id: str | None = None,
                  tools_enabled: bool = True) -> dict[str, Any]:
        """Run one user-created agent: its custom instructions are the
        system prompt; it receives capabilities only for its granted
        tools; governance still has the last word."""
        from .worker import AgentWorker
        agent = self.agents.get(agent_id)
        rights = [{"method": "EXECUTE", "resource": f"jobs.{agent_id}"}]
        rights += [{"method": "TOOL", "resource": t}
                   for t in agent.get("tools") or []]
        cap = self.broker.issue("crow-os/kernel", rights,
                                ttl_seconds=900, rate_limit=20)
        base = base_url or DEFAULT_BASE
        client = OllamaClient(base_url=base, model=resolve_model(model or agent.get("model"), base))
        from .tools import ToolRunner
        tools = ToolRunner(self.broker, cap,
                           allow_private=self.allow_private_tools) \
            if tools_enabled and "net.fetch" in (agent.get("tools") or []) else None
        spawner = None
        if tools_enabled and "agent.spawn" in (agent.get("tools") or []):
            spawner = lambda parent_id, sub_id, sub_obj: \
                self.spawn_subagent(parent_id, sub_id, sub_obj,
                                    base_url=base_url, model=model)
        from .tools import FileTools
        file_tools = FileTools(self.broker, cap, self.fs) \
            if tools_enabled and any(t.startswith("fs.")
                                     for t in agent.get("tools") or []) else None
        worker = AgentWorker(self.broker, self.workflow, self.ledger,
                             client, tools=tools, spawner=spawner,
                             file_tools=file_tools)
        if job_id is None:
            job_id = self.workflow.create_job(
                objective, chain=[agent_id, "governance"])
        job = worker.run(objective, agent, cap,
                         outputs=self.outputs, job_id=job_id)
        if job["status"] == "completed":
            self._persist_artifact(job["job_id"])
        return job

    def transcript(self, job_id: str) -> str:
        """Export one job as a Markdown transcript: the chat (the
        user's objective), every agent output, tool invocations,
        sub-agent links, and the governance verdict — assembled from
        the workflow record, the outputs sink, and the signed ledger,
        so what you export is what was attested."""
        from .workflow import WorkflowError
        try:
            job = self.workflow.get(job_id)
        except WorkflowError as e:
            raise KeyError(job_id) from e
        out = dict(self.outputs.get(job_id) or {})
        if not out.get("artifact"):
            # Outputs are in-memory; the artifact itself is persisted
            # to Documents at completion, so a transcript after a
            # restart still carries the agent's output.
            try:
                out["artifact"] = self.fs.read_file(
                    f"/home/user/Documents/{job_id}.md")
                out["saved_to"] = f"/home/user/Documents/{job_id}.md"
            except Exception:  # noqa: BLE001 - no artifact persisted
                pass
        entries = [e for e in self.ledger.read()
                   if e["raw"].get("job_id") == job_id
                   or e["raw"].get("parent_job_id") == job_id
                   or e["raw"].get("child_job_id") == job_id]
        lines = [f"# Chat transcript — job {job_id}", "",
                 f"- Status: **{job['status']}**",
                 f"- Chain: {' → '.join(job.get('chain') or [])}",
                 f"- Ledger entries: {len(entries)}", ""]
        if out.get("agent"):
            lines.append(f"**Agent:** {out['agent']}")
        lines += ["## 🧑 User", "", job["objective"], ""]
        if out.get("plan"):
            lines += ["## 🐛 Debugger — plan", "", out["plan"], ""]
        if out.get("artifact"):
            who = out.get("agent") or "Coder"
            lines += [f"## 🤖 {who} — output", "", out["artifact"], ""]
        for e in entries:
            raw = e["raw"]
            if e["kind"] == "tool.invoked":
                lines.append(f"- 🔧 `{raw.get('tool')}` "
                             f"{raw.get('host') or raw.get('path') or ''} "
                             f"→ {raw.get('status', 'ok')}")
            elif e["kind"] == "agent.spawned":
                if raw.get("child_job_id") == job_id:
                    lines.append(f"- ⤵ spawned by parent job "
                                 f"{raw.get('parent_job_id')} (depth {raw.get('depth')})")
                else:
                    lines.append(f"- ⤵ spawned sub-agent **{raw.get('agent')}** "
                                 f"(job {raw.get('child_job_id')}, depth {raw.get('depth')})")
        if out.get("subagents"):
            lines += ["", "## ⤵ Sub-agents"]
            for sub in out["subagents"]:
                if "error" in sub:
                    lines.append(f"- **{sub['agent']}** — refused: {sub['error']}")
                else:
                    lines.append(f"- **{sub['agent']}** — job {sub['job_id']}: {sub['status']}")
        steps = [e for e in entries if e["kind"] in ("job.step", "job.failed")]
        if steps:
            lines += ["", "## Steps"]
            for e in steps:
                raw = e["raw"]
                lines.append(f"- {raw.get('role')}: {raw.get('note')}")
        if out.get("saved_to"):
            lines += ["", f"Artifact saved to `{out['saved_to']}`"]
        if job["status"] == "running":
            lines += ["", "_⏳ This run is still in progress — the transcript "
                          "is partial._"]
        return "\n".join(lines) + "\n"

    def computer_info(self) -> dict[str, Any]:
        """The Bot Computer page: the one computer the agents share —
        real disk numbers, the size of the Crow OS home, and the
        ledger depth. (Rebuilding state is non-destructive here: the
        workflow is always rebuilt from the signed ledger.)"""
        import shutil
        total, _used, free = shutil.disk_usage(self.home)
        home_bytes = 0
        for dirpath, _dirs, names in os.walk(self.home):
            for n in names:
                try:
                    home_bytes += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
        return {"disk_total": total, "disk_free": free,
                "home_bytes": home_bytes, "home": str(self.home),
                "ledger": self.ledger.verify()["entries"]}

    def run_demo(self, objective: str,
                 job_id: str | None = None) -> dict[str, Any]:
        cap = self.broker.issue(
            "crow-os/kernel",
            [{"method": "EXECUTE", "resource": "jobs.run"}],
            ttl_seconds=600, rate_limit=10)
        if job_id is None:
            job_id = self.workflow.create_job(objective)
        else:
            self.workflow.get(job_id)  # must already exist (pre-created)

        # debugger
        self.broker.authorize(cap, "EXECUTE", "jobs.run")
        plan = {"job": job_id, "objective": objective,
                "criteria": ["artifact references plan", "artifact non-empty"]}
        self.workflow.report(job_id, "debugger", True, f"plan:{job_id}")

        # coder
        artifact = f"# artifact for {objective}\nplan:{job_id}\n"
        ok = f"plan:{job_id}" in artifact and len(artifact) > 0
        self.workflow.report(job_id, "coder", ok, "artifact written")

        # tester: run the criteria for real
        results = [f"plan:{job_id}" in artifact, len(artifact) > 0]
        self.workflow.report(job_id, "tester", all(results),
                             f"criteria {sum(results)}/{len(results)}")

        self.outputs[job_id] = {"plan": json.dumps(plan, indent=2), "artifact": artifact}

        # governance: the trust layer must verify before completion
        verdict = self.ledger.verify()
        self.workflow.report(job_id, "governance", verdict["ok"],
                             f"ledger ok={verdict['ok']} entries={verdict['entries']}")
        job = self.workflow.get(job_id)
        if job["status"] == "completed":
            self._persist_artifact(job_id)
        return job
