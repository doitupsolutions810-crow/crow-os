"""Role-checked workflow.

Fixes the swarm coordinator audit finding: completion is gated by role,
not by event count. A job advances only when the role that owns the
current step reports; duplicates and out-of-role events are rejected.
All state transitions are written to the ledger, so a restart loses
nothing.
"""
from __future__ import annotations

import uuid
from typing import Any

from .ledger import Ledger

CHAIN = ["debugger", "coder", "tester", "governance"]


class WorkflowError(Exception):
    pass


class Workflow:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def _jobs(self) -> dict[str, dict[str, Any]]:
        jobs: dict[str, dict[str, Any]] = {}
        for e in self.ledger.read():
            if e["kind"] == "job.created":
                jobs[e["raw"]["job_id"]] = {
                    "job_id": e["raw"]["job_id"],
                    "objective": e["raw"]["objective"],
                    "step": 0,
                    "status": "running",
                }
            elif e["kind"] == "job.step":
                job = jobs.get(e["raw"]["job_id"])
                if job:
                    job["step"] = e["raw"]["step"] + 1
                    if job["step"] >= len(CHAIN):
                        job["status"] = "completed"
            elif e["kind"] == "job.failed":
                job = jobs.get(e["raw"]["job_id"])
                if job:
                    job["status"] = "failed"
        return jobs

    def create_job(self, objective: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.ledger.append("crow-os", "job.created",
                           {"job_id": job_id, "objective": objective, "chain": CHAIN})
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._jobs().get(job_id)
        if job is None:
            raise WorkflowError(f"unknown job: {job_id}")
        return job

    def expected_role(self, job_id: str) -> str:
        job = self.get(job_id)
        if job["status"] != "running":
            raise WorkflowError(f"job {job_id} is {job['status']}")
        return CHAIN[job["step"]]

    def report(self, job_id: str, role: str, passed: bool, note: str = "") -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] != "running":
            raise WorkflowError(f"job {job_id} is {job['status']}")
        expected = CHAIN[job["step"]]
        if role != expected:
            raise WorkflowError(
                f"step {job['step']} belongs to {expected}, not {role}")
        if not passed:
            self.ledger.append("crow-os", "job.failed",
                               {"job_id": job_id, "role": role, "note": note})
            return self.get(job_id)
        self.ledger.append("crow-os", "job.step",
                           {"job_id": job_id, "step": job["step"],
                            "role": role, "note": note})
        return self.get(job_id)
