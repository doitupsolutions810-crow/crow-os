"""Capability-gated LLM worker.

The gate is checked BEFORE the model is called: a step whose capability
does not hold EXECUTE on that step's resource never reaches the LLM.
If the model is unreachable, the step fails closed — the job is marked
failed, never passed with a synthetic result (the localLoop lesson).
Tester and governance stay deterministic: the model proposes, the
kernel disposes.
"""
from __future__ import annotations

from typing import Any

import re

from .capabilities import Broker, CapabilityError
from .ledger import Ledger
from .llm import LLMUnavailable, OllamaClient
from .tools import ToolError, ToolRunner
from .workflow import Workflow

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_DELEGATE_RE = re.compile(r"^delegate to ([a-z0-9][a-z0-9\-]*):\s*(.+)$", re.I)
_FS_RE = re.compile(r"^fs (read|list):\s*(.+)$", re.I)
_CONNECTOR_RE = re.compile(r"^connector:\s*([a-z0-9.\-]+)$", re.I)

PROMPTS = {
    "debugger": ("You are the debugger/planner in a 4-role chain. "
                 "Return a short plan with numbered acceptance criteria."),
    "coder": ("You are the coder in a 4-role chain. "
              "Return only the artifact (code or text) implementing the plan. "
              "Include the plan reference line exactly as given."),
}


class WorkerError(Exception):
    pass


class LLMWorker:
    def __init__(self, broker: Broker, workflow: Workflow, ledger: Ledger,
                 client: OllamaClient, tools: ToolRunner | None = None):
        self.broker = broker
        self.workflow = workflow
        self.ledger = ledger
        self.client = client
        self.tools = tools

    def run(self, objective: str, cap: dict[str, Any],
            outputs: dict | None = None,
            job_id: str | None = None) -> dict[str, Any]:
        if job_id is None:
            job_id = self.workflow.create_job(objective)
        else:
            self.workflow.get(job_id)  # must already exist (pre-created)
        role = "debugger"
        try:
            # debugger (LLM)
            self.broker.authorize(cap, "EXECUTE", "jobs.debugger")
            if outputs is not None:
                outputs.setdefault(job_id, {"plan": "", "artifact": "", "live": ""})
            plan = self.client.chat_stream(
                PROMPTS["debugger"], f"Objective: {objective}\nJob: {job_id}",
                on_token=(lambda t: outputs[job_id].__setitem__(
                    "live", outputs[job_id].get("live", "") + t)) if outputs is not None else None)
            state = self.workflow.report(job_id, "debugger", True,
                                         f"plan chars={len(plan)}")
            if state["status"] == "failed":
                return state

            # coder (LLM) — with network tooling when the objective
            # names a URL: the fetch is capability-gated, SSRF-guarded
            # and written to the ledger before the model ever sees it.
            role = "coder"
            self.broker.authorize(cap, "EXECUTE", "jobs.coder")
            context = ""
            url_match = _URL_RE.search(objective)
            if url_match and self.tools is not None:
                fetched = self.tools.fetch(url_match.group(0))
                self.ledger.append("crow-os/tools", "tool.invoked", {
                    "job_id": job_id, "tool": fetched["tool"],
                    "host": fetched["host"], "status": fetched["status"],
                    "bytes": fetched["bytes"]})
                context = (f"\nFetched context from {fetched['url']} "
                           f"(status {fetched['status']}):\n{fetched['excerpt']}\n")
            if outputs is not None:
                outputs[job_id]["live"] = ""
            artifact = self.client.chat_stream(
                PROMPTS["coder"],
                f"Objective: {objective}\nPlan:\n{plan}\n{context}"
                f"Plan reference line: plan:{job_id}",
                on_token=(lambda t: outputs[job_id].__setitem__(
                    "live", outputs[job_id].get("live", "") + t)) if outputs is not None else None)
            plan_reference = f"plan:{job_id}"
            if plan_reference not in artifact:
                # Small local models sometimes omit the mandatory reference.
                # Give the coder exactly one constrained correction opportunity;
                # the tester still independently enforces the reference.
                self.ledger.append("crow-os", "coder.repair", {
                    "job_id": job_id,
                    "attempt": 1,
                    "reason": "missing plan reference line",
                    "artifact_chars": len(artifact),
                })
                if outputs is not None:
                    outputs[job_id]["live"] = ""
                previous_artifact = artifact
                artifact = self.client.chat_stream(
                    PROMPTS["coder"],
                    f"Objective: {objective}\\nPlan:\\n{plan}\\n{context}"
                    f"Plan reference line: {plan_reference}\\n"
                    "Your previous artifact omitted the required plan reference. "
                    "Return the corrected artifact, preserving its useful content, "
                    f"and include the exact literal line `# {plan_reference}` "
                    "on its own line. Do not invent or alter the reference.\\n"
                    f"Previous artifact:\\n{previous_artifact}",
                    on_token=(lambda t: outputs[job_id].__setitem__(
                        "live", outputs[job_id].get("live", "") + t)) if outputs is not None else None)
            reference_present = plan_reference in artifact
            if outputs is not None:
                outputs.setdefault(job_id, {"plan": "", "artifact": "", "live": ""})
                outputs[job_id]["plan"] = plan
                outputs[job_id]["artifact"] = artifact
            passed = bool(artifact.strip()) and reference_present
            note = (f"artifact chars={len(artifact)}; "
                    f"plan reference line {'present' if reference_present else 'missing'}")
            state = self.workflow.report(job_id, "coder", passed, note)
            if state["status"] == "failed":
                failed = dict(state)
                if not reference_present:
                    failed["error"] = "plan reference line missing after one repair attempt"
                elif not artifact.strip():
                    failed["error"] = "coder returned an empty artifact"
                return failed

            # tester (deterministic: criteria actually checked)
            role = "tester"
            checks = [f"plan:{job_id}" in artifact, len(artifact.strip()) > 0]
            state = self.workflow.report(job_id, "tester", all(checks),
                                         f"criteria {sum(checks)}/{len(checks)}")
            if state["status"] == "failed":
                return state

            # governance (deterministic: ledger must verify)
            role = "governance"
            verdict = self.ledger.verify()
            self.workflow.report(job_id, "governance", verdict["ok"],
                                 f"ledger ok={verdict['ok']}")
        except (CapabilityError, LLMUnavailable, ToolError) as e:
            if self.workflow.get(job_id)["status"] == "running":
                self.workflow.report(job_id, role, False,
                                     f"{type(e).__name__}: {e}")
            job = self.workflow.get(job_id)
            job["error"] = f"{type(e).__name__}: {e}"
            return job
        return self.workflow.get(job_id)


class AgentWorker:
    """Runs one user-created agent (v0.8).

    The agent's custom instructions are its system prompt — data for
    the model, never authority: the step still needs an EXECUTE
    capability for jobs.<agent_id>, tools fire only if the agent was
    granted them, and the chain still ends with deterministic
    governance verifying the ledger.
    """

    def __init__(self, broker: Broker, workflow: Workflow, ledger: Ledger,
                 client: OllamaClient, tools: ToolRunner | None = None,
                 spawner=None, file_tools=None, connector_call=None):
        self.broker = broker
        self.workflow = workflow
        self.ledger = ledger
        self.client = client
        self.tools = tools
        self.spawner = spawner  # callable(parent_job_id, agent_id, objective) -> job
        self.file_tools = file_tools
        self.connector_call = connector_call

    def run(self, objective: str, agent: dict[str, Any], cap: dict[str, Any],
            outputs: dict | None = None,
            job_id: str | None = None,
            memory: str = "") -> dict[str, Any]:
        agent_id = agent["id"]
        if job_id is None:
            job_id = self.workflow.create_job(
                objective, chain=[agent_id, "governance"])
        else:
            self.workflow.get(job_id)  # must already exist (pre-created)
        role = agent_id
        try:
            self.broker.authorize(cap, "EXECUTE", f"jobs.{agent_id}")
            context = ""
            url_match = _URL_RE.search(objective)
            if url_match and self.tools is not None and \
                    "net.fetch" in (agent.get("tools") or []):
                fetched = self.tools.fetch(url_match.group(0))
                self.ledger.append("crow-os/tools", "tool.invoked", {
                    "job_id": job_id, "tool": fetched["tool"],
                    "host": fetched["host"], "status": fetched["status"],
                    "bytes": fetched["bytes"]})
                context = (f"\nFetched context from {fetched['url']} "
                           f"(status {fetched['status']}):\n{fetched['excerpt']}\n")
            # File directives: "fs read: <path>" / "fs list: <path>"
            # lines pull CrowFS content into the agent's context —
            # only with the matching grant, gated before any disk
            # I/O, attested, and fail-soft like delegation.
            if self.file_tools is not None:
                done = 0
                for line in objective.splitlines():
                    m = _FS_RE.match(line.strip())
                    if not m or done >= 3:
                        continue
                    op, vpath = m.group(1).lower(), m.group(2).strip()
                    if f"fs.{op}" not in (agent.get("tools") or []):
                        continue
                    done += 1
                    try:
                        if op == "read":
                            r = self.file_tools.read(vpath)
                            self.ledger.append("crow-os/tools", "tool.invoked", {
                                "job_id": job_id, "tool": "fs.read",
                                "path": vpath, "bytes": len(r["content"].encode())})
                            context += (f"\nFile {vpath}:\n"
                                        f"{r['content'][:4000]}\n")
                        else:
                            r = self.file_tools.list(vpath)
                            names = [e["name"] for e in r["entries"]]
                            self.ledger.append("crow-os/tools", "tool.invoked", {
                                "job_id": job_id, "tool": "fs.list",
                                "path": vpath, "entries": len(names)})
                            context += f"\nListing {vpath}: {', '.join(names)}\n"
                    except Exception as e:  # noqa: BLE001 - fail soft
                        context += f"\n(fs {op} {vpath} failed: {e})\n"
            # Connector directives: "connector: <tool>" lines run a
            # granted connector tool — gated inside the runner,
            # attested, excerpt into context, fail-soft.
            if self.connector_call is not None:
                for line in objective.splitlines():
                    m = _CONNECTOR_RE.match(line.strip())
                    if not m:
                        continue
                    tool = m.group(1).strip().lower()
                    if tool not in (agent.get("tools") or []):
                        continue
                    try:
                        r = self.connector_call(tool)
                        self.ledger.append("crow-os/tools", "tool.invoked", {
                            "job_id": job_id, "tool": tool,
                            "result": {k: v for k, v in r.items() if k != "tool"}})
                        if "repos" in r:
                            context += (f"\n{tool} ({r.get('count', 0)}): "
                                        f"{', '.join(r['repos'][:40])}\n")
                        else:
                            context += f"\n{tool}: {r}\n"
                    except Exception as e:  # noqa: BLE001 - fail soft
                        context += f"\n({tool} failed: {e})\n"
            if outputs is not None:
                outputs.setdefault(job_id, {"plan": "", "artifact": "", "live": ""})
            memory_block = (f"\nYour memory from earlier runs:\n"
                            f"{memory[-2000:]}\n") if memory else ""
            reply = self.client.chat_stream(
                agent["instructions"],
                f"Objective: {objective}\n{memory_block}{context}",
                on_token=(lambda t: outputs[job_id].__setitem__(
                    "live", outputs[job_id].get("live", "") + t)) if outputs is not None else None)
            state = self.workflow.report(job_id, agent_id, bool(reply.strip()),
                                         f"response chars={len(reply)}")
            if outputs is not None:
                outputs[job_id]["artifact"] = reply
                outputs[job_id]["agent"] = agent["name"]
            if state["status"] == "failed":
                return state
            # Delegation: "delegate to <agent-id>: <objective>" lines in
            # the objective spawn sub-agents — only with the agent.spawn
            # grant, only through the kernel's spawner (which attests
            # agent.spawned and enforces the depth cap).
            if self.spawner is not None and \
                    "agent.spawn" in (agent.get("tools") or []):
                subs = []
                for line in objective.splitlines():
                    m = _DELEGATE_RE.match(line.strip())
                    if m and len(subs) < 3:
                        # A bad delegation (unknown agent, reserved
                        # role, depth cap) must not kill the parent
                        # job: record the refusal and carry on to
                        # governance.
                        try:
                            child = self.spawner(job_id, m.group(1),
                                                 m.group(2).strip())
                            subs.append({"agent": m.group(1),
                                         "job_id": child["job_id"],
                                         "status": child["status"]})
                        except Exception as e:  # noqa: BLE001 - fail soft
                            subs.append({"agent": m.group(1),
                                         "error": f"{type(e).__name__}: {e}"})
                if subs and outputs is not None:
                    outputs[job_id]["subagents"] = subs
            role = "governance"
            verdict = self.ledger.verify()
            self.workflow.report(job_id, "governance", verdict["ok"],
                                 f"ledger ok={verdict['ok']}")
        except (CapabilityError, LLMUnavailable, ToolError) as e:
            if self.workflow.get(job_id)["status"] == "running":
                self.workflow.report(job_id, role, False,
                                     f"{type(e).__name__}: {e}")
            job = self.workflow.get(job_id)
            job["error"] = f"{type(e).__name__}: {e}"
            return job
        return self.workflow.get(job_id)
