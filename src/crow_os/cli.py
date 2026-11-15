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
    sub.add_parser("verify")
    s_ = sub.add_parser("serve", help="Crow OS Home Screen (web UI backed by the kernel)")
    s_.add_argument("--port", type=int, default=8787)
    a = p.parse_args(argv)
    home = Path(a.home)
    if a.cmd == "run":
        kernel = Kernel(home)
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
        try:
            return serve(Kernel(home), a.port)
        except OSError as e:
            print(f"cannot serve on port {a.port}: {e}")
            return 2
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
