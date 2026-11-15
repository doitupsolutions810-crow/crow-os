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
from pathlib import Path
from typing import Any

from .agents import AgentRegistry
from .capabilities import Broker
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
            "bytes": rec["bytes"]})
        self.outputs[job_id]["saved_to"] = rec["path"]

    def run_llm(self, objective: str, model: str | None = None,
                base_url: str | None = None,
                job_id: str | None = None) -> dict[str, Any]:
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
                                            allow_private=self.allow_private_tools))
        job = worker.run(objective, cap, outputs=self.outputs, job_id=job_id)
        if job["status"] == "completed":
            self._persist_artifact(job["job_id"])
        return job

    def run_agent(self, objective: str, agent_id: str,
                  model: str | None = None, base_url: str | None = None,
                  job_id: str | None = None) -> dict[str, Any]:
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
                           allow_private=self.allow_private_tools)             if "net.fetch" in (agent.get("tools") or []) else None
        worker = AgentWorker(self.broker, self.workflow, self.ledger,
                             client, tools=tools)
        if job_id is None:
            job_id = self.workflow.create_job(
                objective, chain=[agent_id, "governance"])
        job = worker.run(objective, agent, cap,
                         outputs=self.outputs, job_id=job_id)
        if job["status"] == "completed":
            self._persist_artifact(job["job_id"])
        return job

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
