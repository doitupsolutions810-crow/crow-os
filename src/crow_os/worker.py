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
            state = self.workflow.report(job_id, "coder", bool(artifact.strip()),
                                         f"artifact chars={len(artifact)}")
            if outputs is not None:
                outputs.setdefault(job_id, {"plan": "", "artifact": "", "live": ""})
                outputs[job_id]["plan"] = plan
                outputs[job_id]["artifact"] = artifact
            if state["status"] == "failed":
                return state

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
                 spawner=None):
        self.broker = broker
        self.workflow = workflow
        self.ledger = ledger
        self.client = client
        self.tools = tools
        self.spawner = spawner  # callable(parent_job_id, agent_id, objective) -> job

    def run(self, objective: str, agent: dict[str, Any], cap: dict[str, Any],
            outputs: dict | None = None,
            job_id: str | None = None) -> dict[str, Any]:
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
            if outputs is not None:
                outputs.setdefault(job_id, {"plan": "", "artifact": "", "live": ""})
            reply = self.client.chat_stream(
                agent["instructions"],
                f"Objective: {objective}\n{context}",
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
                        child = self.spawner(job_id, m.group(1),
                                             m.group(2).strip())
                        subs.append({"agent": m.group(1),
                                     "job_id": child["job_id"],
                                     "status": child["status"]})
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
