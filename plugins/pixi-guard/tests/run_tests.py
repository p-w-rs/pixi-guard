#!/usr/bin/env python3
"""Tests for pixi-guard. Pure stdlib; run with:
    pixi run test
Hermetic: the real hooks/guard.sh runs with PIXI_GUARD_HOME and
PIXI_GUARD_WRAP_DIR in a temp dir and PIXI_GUARD_PIXI pointing at a fake pixi
(or at nothing, to play a machine without pixi).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks" / "guard.sh"
TMP = Path(tempfile.mkdtemp(prefix="pixi-guard-test-"))
FAKE_PIXI = TMP / "bin" / "pixi"
FAKE_PIXI.parent.mkdir()
FAKE_PIXI.write_text("#!/bin/sh\nexit 0\n")
FAKE_PIXI.chmod(0o755)
PLAIN = TMP / "plain"  # a folder with no project markers
PLAIN.mkdir()

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def run(mode, payload, pixi=True, **env):
    e = dict(os.environ)
    e.pop("PIXI_GUARD_TOOLS", None)
    e.update(PIXI_GUARD_HOME=str(TMP / "state"), PIXI_GUARD_WRAP_DIR=str(TMP / "wrap"),
             PIXI_GUARD_PIXI=str(FAKE_PIXI) if pixi else str(TMP / "no-such-pixi"), HOME=str(TMP))
    e.update(env)
    r = subprocess.run(["sh", str(GUARD), mode], input=json.dumps(payload), capture_output=True,
                       text=True, timeout=30, env=e)
    out = r.stdout.strip()
    return (json.loads(out) if out else None), r.stderr


def pre(cmd, cwd=PLAIN, sid="s1", **kw):
    out, err = run("pre", {"session_id": sid, "transcript_path": "/x", "cwd": str(cwd),
                           "hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": cmd, "description": "d"}}, **kw)
    return out, err


def decision(cmd, **kw):
    out, err = pre(cmd, **kw)
    if err:
        return "stderr:" + err
    return out["hookSpecificOutput"]["permissionDecision"] if out else "pass"


def reason(cmd, **kw):
    out, _ = pre(cmd, **kw)
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else ""


print("scanner: refused")
for cmd in [
    "python x.py", "python3 -m venv .venv", "python3.12 -c 1", "pip install requests",
    "pip3 install -r requirements.txt", "uv sync", "uvx ruff check", "easy_install foo",
    "poetry install", "conda install numpy", "pipx install black", "pytest -q", "ipython",
    "jupyter lab", "./run.py", "scripts/build.py --fast", "/usr/bin/python3 x",
    "/usr/bin/env python3 -c 1", "env -i PATH=/bin python x", "sudo -u bob pip install x",
    "timeout 5 pytest", "nice -n 5 python x", "time python x", "nohup python x &",
    "echo hi && pip3 install x", "make || python setup.py install", "a; b; uv pip install x",
    "cat x | python -", "echo $(python -V)", "echo \"$(python -V)\"", "echo `pip --version`",
    "bash -c 'python x'", "bash -lc \"uv sync\"", "sh -c 'cd a && pip install .'",
    "eval python x", "ls | xargs -I{} python {}", "find . -name '*.py' -exec black {} +",
    "{ python x; }", "(cd sub && python x)", "if true; then python x; fi",
    "for f in a b; do python $f; done", "source .venv/bin/activate", ". venv/bin/activate",
    "brew install python@3.12", "brew install uv", "apt-get install -y python3-pip",
    "bash <<EOF\npip install x\nEOF", "python3 - <<'EOF'\nprint(1)\nEOF",
    "cd /tmp\npython x", "X=1 Y=2 python x", "echo ok # fine\npip install x",
]:
    check(f"deny  {cmd!r}", decision(cmd) == "deny", decision(cmd))

print("scanner: allowed")
for cmd in [
    "pixi run python x.py", "pixi run pytest", "pixi add --pypi requests", "pixi exec python -V",
    "cd a && pixi run python x", "/opt/local/bin/pixi run python x", "git commit -m 'pip install x'",
    "git commit -m \"fix; python thing | uv\"", "echo python", "echo \"python x\"", "grep -r pip .",
    "which python3", "command -v python3", "type -p pip", "ls *.py", "cat setup.py",
    "chmod +x run.py", "rg 'import numpy' src", "cat > notes.md <<EOF\npip install x\npython y\nEOF",
    "git commit -m \"$(cat <<'EOF'\nUse pip install, don't run python\nEOF\n)\"",
    "cat <<-EOF\n\tpip install x\n\tEOF\necho done", "ruff check .", "node build.js",
    "echo ${PYTHON:-python}", "npm run python-docs", "brew install jq", "pixi run bash -c 'pip list'",
    "",
]:
    check(f"pass  {cmd!r}", decision(cmd) == "pass", decision(cmd))

print("escalation")
check("PIXI_GUARD=ask prefix asks", decision("PIXI_GUARD=ask python x") == "ask")
check("ask on a later segment", decision("cd a && PIXI_GUARD=ask pip install x") == "ask")
check("quoted PIXI_GUARD=ask is not an override",
      decision("echo 'PIXI_GUARD=ask' && python x") == "deny")
check("ask reason names the program", "`pip`" in reason("PIXI_GUARD=ask pip install x"))
check("deny reason explains the override", "PIXI_GUARD=ask <command>" in reason("python x"))

print("tool list override")
check("PIXI_GUARD_TOOLS narrows the list", decision("pytest", PIXI_GUARD_TOOLS="python") == "pass")
check("PIXI_GUARD_TOOLS still catches listed", decision("python x", PIXI_GUARD_TOOLS="python") == "deny")

print("project kinds")
pixi_proj = TMP / "pixiproj"
(pixi_proj / "src" / "deep").mkdir(parents=True)
(pixi_proj / "pixi.toml").write_text("[workspace]\n")
r = reason("python x", cwd=pixi_proj / "src" / "deep")
check("pixi workspace found from a subfolder", str(pixi_proj) in r and "pixi workspace" in r, r)
check("python suggests pixi run", "pixi run python" in r, r)
r = reason("pip install x", cwd=pixi_proj)
check("pip suggests pixi add, not pixi run pip", "pixi add" in r and "pixi run pip" not in r, r)

pyproj_pixi = TMP / "pyproj-pixi"
pyproj_pixi.mkdir()
(pyproj_pixi / "pyproject.toml").write_text("[project]\nname='a'\n\n[tool.pixi.workspace]\n")
check("pyproject with [tool.pixi] is pixi", "pixi workspace" in reason("python x", cwd=pyproj_pixi))

py_proj = TMP / "pyproj"
(py_proj / "pkg").mkdir(parents=True)
(py_proj / "pyproject.toml").write_text("[project]\nname='a'\n")
r = reason("python x", cwd=py_proj / "pkg")
check("non-pixi python project", "not on pixi" in r and "pixi:project" in r and str(py_proj) in r, r)
wrap = r.split("put the wrapper at ")[1].split(" ")[0].rstrip(".")
check("wrapper path is under the wrap dir", wrap.startswith(str(TMP / "wrap")) and wrap.endswith("/pixi.toml"), wrap)

def wrap_path(d):
    e = dict(os.environ, PIXI_GUARD_WRAP_DIR=str(TMP / "wrap"), HOME=str(TMP))
    return subprocess.run(["sh", str(GUARD), "wrap-path", str(d)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=e).stdout.strip()


check("wrap-path from a subfolder matches the refusal", wrap_path(py_proj / "pkg") == wrap, wrap_path(py_proj / "pkg"))
link = TMP / "link-to-pyproj"
link.symlink_to(py_proj)
check("wrap-path resolves symlinks", wrap_path(link / "pkg") == wrap)
r = reason("python x", cwd=link / "pkg")
check("refusal through a symlinked cwd names the same wrapper", wrap in r, r)
check("wrap-path for a plain folder", wrap_path(PLAIN).endswith("/pixi.toml"))
Path(wrap).parent.mkdir(parents=True)
Path(wrap).write_text("[workspace]\n")
r = reason("python x", cwd=py_proj / "pkg")
check("wrapped project points at its manifest", "--manifest-path '" + wrap + "'" in r, r)

req_proj = TMP / "reqproj"
req_proj.mkdir()
(req_proj / "requirements.txt").write_text("requests\n")
check("requirements.txt marks a python project", "not on pixi" in reason("python x", cwd=req_proj))
check("plain folder: one-off advice", "pixi exec" in reason("python x"))

print("pixi missing")
r1 = reason("python x", pixi=False, sid="m1")
check("first refusal tells the user", "not installed" in r1 and "tell the user" in r1, r1)
r2 = reason("python x", pixi=False, sid="m1")
check("later refusals are short", "has been told" in r2, r2)
check("other sessions are told again", "tell the user" in reason("python x", pixi=False, sid="m2"))
check("pixi missing: still allows non-python", decision("ls", pixi=False) == "pass")

print("session start")
out, _ = run("session", {"session_id": "s1", "hook_event_name": "SessionStart", "source": "startup"})
ctx = out["hookSpecificOutput"]["additionalContext"]
check("policy context", "pixi" in ctx and "pixi:project" in ctx)
check("no missing notice when pixi exists", "NOT installed" not in ctx)
out, _ = run("session", {"session_id": "s1"}, pixi=False)
check("missing notice without pixi", "NOT installed" in out["hookSpecificOutput"]["additionalContext"])

print("robustness")
out, _ = run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Bash",
                     "tool_input": {"command": "python \"x\\ty\" é \\u00e9"}})
check("escapes in payload still parse to a deny", out and out["hookSpecificOutput"]["permissionDecision"] == "deny")
r = reason('python "a\\b"', cwd=PLAIN)
check("reason is valid JSON with quotes/backslashes", "pixi-guard" in r)
out, err = run("pre", {"tool_name": "Bash"})
check("no command: no output", out is None and not err)
r = subprocess.run(["sh", str(GUARD), "pre"], input="not json", capture_output=True, text=True)
check("garbage input fails open", r.stdout == "" and r.returncode == 0)
big = "echo " + "a" * 50000 + " && python x"
check("long command still scanned", decision(big) == "deny")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
