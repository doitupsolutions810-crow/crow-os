from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import Ledger
from .runtime import Kernel


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crow")
    p.add_argument("--home", default=str(Path.home() / ".crow-os"))
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--objective", default="ship the kernel")
    r = sub.add_parser("run", help="LLM-backed run (Ollama, capability-gated)")
    r.add_argument("--objective", default="ship the kernel")
    r.add_argument("--model", default=None)
    r.add_argument("--ollama", default=None, help="Ollama base URL")
    r.add_argument("--agent", default=None, help="run a user-created agent by id")
    sub.add_parser("verify")
    o = sub.add_parser("ollama-check", help="verify the local Ollama endpoint and installed model")
    o.add_argument("--model", default=None)
    o.add_argument("--base-url", "--ollama", dest="base_url", default=None)
    o.add_argument("--smoke", action="store_true", help="send one short chat request")
    ai = sub.add_parser("avrone-import", help="ingest one local AVRONE bridge journal as untrusted evidence")
    ai.add_argument("--path", required=True, help="path to an AVRONE bridge/<run-id>.jsonl file")
    f_ = sub.add_parser("fs", help="the OS file system (operator view)")
    fsub = f_.add_subparsers(dest="fs_cmd", required=True)
    x = fsub.add_parser("ls"); x.add_argument("path", nargs="?", default="/")
    x = fsub.add_parser("cat"); x.add_argument("path")
    x = fsub.add_parser("write"); x.add_argument("path"); x.add_argument("text")
    fsub.add_parser("tree")
    g_ = sub.add_parser("agent", help="user-created agents")
    gsub = g_.add_subparsers(dest="agent_cmd", required=True)
    x = gsub.add_parser("create"); x.add_argument("--name", required=True)
    x.add_argument("--instructions", required=True)
    x.add_argument("--model", default=None)
    x.add_argument("--tools", default="", help="comma-separated, e.g. net.fetch,fs.read")
    gsub.add_parser("list")
    s_ = sub.add_parser("serve", help="Crow OS Home Screen (web UI backed by the kernel)")
    s_.add_argument("--port", type=int, default=8787)
    t_ = sub.add_parser("transcript", help="export a job's chat + agent outputs (Markdown)")
    t_.add_argument("job_id")
    a = p.parse_args(argv)
    home = Path(a.home)
    if a.cmd == "ollama-check":
        from .llm import DEFAULT_BASE, DEFAULT_MODEL, LLMUnavailable, OllamaClient
        try:
            client = OllamaClient(base_url=a.base_url or DEFAULT_BASE,
                                  model=a.model or DEFAULT_MODEL)
            health = client.health()
            result = {"reachable": True, "base": client.base_url,
                      "model": client.model, "model_present": health["model_present"],
                      "installed": health["models"]}
            if not health["model_present"]:
                print(json.dumps(result, indent=2))
                return 3  # model not installed; no implicit pulls
            if a.smoke:
                answer = client.chat_stream(
                    "You are a connection diagnostic. Respond with READY.",
                    "Reply READY.")
                result["smoke_ok"] = bool(answer.strip())
                result["smoke_response_chars"] = len(answer)
            print(json.dumps(result, indent=2))
            return 0
        except (ValueError, LLMUnavailable) as e:
            print(json.dumps({"reachable": False, "error": str(e)}))
            return 2
    if a.cmd == "avrone-import":
        from .avrone import AvroneImportError, import_avrone_journal
        try:
            kernel = Kernel(home)
            outcome = import_avrone_journal(kernel, a.path)
            print(json.dumps(outcome, indent=2))
            return 0 if outcome["ledger_ok"] else 1
        except (AvroneImportError, OSError) as e:
            print(json.dumps({"error": str(e)}))
            return 1
    if a.cmd == "agent":
        from .agents import AgentError
        kernel = Kernel(home)
        try:
            if a.agent_cmd == "create":
                tools = [t.strip() for t in a.tools.split(",") if t.strip()]
                agent = kernel.agents.create(a.name, a.instructions,
                                             model=a.model, tools=tools)
                print(json.dumps(agent, indent=2))
            elif a.agent_cmd == "list":
                for ag in kernel.agents.list():
                    print(f"{ag['id']}  {ag['name']}  tools={ag['tools']}")
        except AgentError as e:
            print(f"agent error: {e}")
            return 1
        return 0
    if a.cmd == "transcript":
        kernel = Kernel(home)
        try:
            print(kernel.transcript(a.job_id), end="")
        except KeyError:
            print(f"no such job: {a.job_id}")
            return 1
        return 0
    if a.cmd == "run":
        kernel = Kernel(home)
        if getattr(a, "agent", None):
            job = kernel.run_agent(a.objective, a.agent, model=a.model,
                                   base_url=a.ollama)
        else:
            job = kernel.run_llm(a.objective, model=a.model, base_url=a.ollama)
        print(json.dumps(job, indent=2))
        return 0 if job["status"] == "completed" else 1
    if a.cmd == "demo":
        kernel = Kernel(home)
        job = kernel.run_demo(a.objective)
        print(json.dumps(job, indent=2))
        return 0 if job["status"] == "completed" else 1
    if a.cmd == "serve":
        from .server import serve
        import os
        # Operator opt-in for intranet tooling (SSRF guard stays on by
        # default): CROW_OS_ALLOW_PRIVATE_TOOLS=1 crow serve
        kernel = Kernel(home, allow_private_tools=os.environ.get(
            "CROW_OS_ALLOW_PRIVATE_TOOLS") == "1")
        try:
            return serve(kernel, a.port)
        except OSError as e:
            print(f"cannot serve on port {a.port}: {e}")
            return 2
    if a.cmd == "fs":
        from .fs import CrowFS, FSError
        fs = CrowFS(home)
        try:
            if a.fs_cmd == "ls":
                for e in fs.list_dir(a.path):
                    print(("d " if e["type"] == "dir" else "f ") + e["name"] +
                          ("" if e["type"] == "dir" else f"  ({e['size']}b)"))
            elif a.fs_cmd == "cat":
                print(fs.read_file(a.path), end="")
            elif a.fs_cmd == "write":
                print(fs.write_file(a.path, a.text))
            elif a.fs_cmd == "tree":
                def walk(vp, depth=0):
                    print("  " * depth + (vp if vp != "/" else "/"))
                    for e in fs.list_dir(vp):
                        child = (vp.rstrip("/") + "/" + e["name"])
                        if e["type"] == "dir":
                            walk(child, depth + 1)
                        else:
                            print("  " * (depth + 1) + e["name"])
                walk("/")
        except FSError as e:
            print(f"fs error: {e}")
            return 1
        return 0
    if a.cmd == "verify":
        from .runtime import SECRET_FILE
        secret_file = home / SECRET_FILE
        if secret_file.exists():
            from .signers import HmacSigner
            verdict = Ledger(home / "ledger.jsonl",
                             signer=HmacSigner(secret_file.read_bytes())).verify()
            verdict["signed"] = True
        else:
            verdict = Ledger(home / "ledger.jsonl").verify()
            verdict["signed"] = False
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
