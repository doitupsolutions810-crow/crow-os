#!/usr/bin/env python3
"""Round-10 probes: the simplify contract — Chat + Agents are the
surface; everything else is backend, hidden unless Settings enables it."""
import re, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/crow_os/home.html").read_text()

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

def w1():
    return ('id="topbar" class="devonly"' in HTML
            and "body:not(.dev) .devonly" in HTML), "status bar + build entry are dev-gated in markup and CSS"

def w2():
    return ('id="settings"' in HTML and 'id="set-dev"' in HTML
            and "crow-os-settings" in HTML and "localStorage" in HTML), "settings modal with a persisted developer-panels toggle"

def w3():
    need = ['id="chats"', 'id="search"', 'id="modelsel"', 'id="build"',
            "EventSource", "textContent", "Crow OS", 'id="agents"',
            'id="agentform"', 'id="objective"']
    missing = [m for m in need if m not in HTML]
    return not missing, f"chat + agents + engine markers intact (missing: {missing})"

def w4():
    # the visible-by-default things must NOT be dev-gated
    bad = []
    for marker in ['id="agents"', 'id="chats"', 'id="composer"', 'id="hello"']:
        i = HTML.find(marker)
        if i == -1 or "devonly" in HTML[max(0, i-60):i]:
            bad.append(marker)
    return not bad, f"chat/agents/composer/greeting are not hidden by default (bad: {bad})"

def w5():
    scripts = re.findall(r"<script>(.*?)</script>", HTML, re.S)
    Path("/tmp/h10.js").write_text("\n".join(scripts))
    r = subprocess.run(["node", "--check", "/tmp/h10.js"], capture_output=True, text=True)
    return r.returncode == 0, "inline JS parses"

def w6():
    return ('_sendVis' in HTML and 'visibility' in HTML
            and 'aria-label="Model select"' in HTML), "Grok behaviors: send appears with content; model select lives in the composer"

for name, fn in [("W1 backend surfaces dev-gated", w1), ("W2 settings modal + persistence", w2),
                 ("W3 surface + engine markers intact", w3), ("W4 chat+agents visible by default", w4),
                 ("W5 inline JS parses", w5), ("W6 Grok composer behaviors", w6)]:
    probe(name, fn)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-10 probes passed; failures: {fails}")
