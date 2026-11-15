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

from .capabilities import Broker, CapabilityError
from .ledger import Ledger
from .llm import LLMUnavailable, OllamaClient
from .workflow import Workflow

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
                 client: OllamaClient):
        self.broker = broker
        self.workflow = workflow
        self.ledger = ledger
        self.client = client

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

            # coder (LLM)
            role = "coder"
            self.broker.authorize(cap, "EXECUTE", "jobs.coder")
            if outputs is not None:
                outputs[job_id]["live"] = ""
            artifact = self.client.chat_stream(
                PROMPTS["coder"],
                f"Objective: {objective}\nPlan:\n{plan}\n"
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
        except (CapabilityError, LLMUnavailable) as e:
            if self.workflow.get(job_id)["status"] == "running":
                self.workflow.report(job_id, role, False,
                                     f"{type(e).__name__}: {e}")
            job = self.workflow.get(job_id)
            job["error"] = f"{type(e).__name__}: {e}"
            return job
        return self.workflow.get(job_id)
