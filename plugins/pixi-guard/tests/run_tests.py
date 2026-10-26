#!/usr/bin/env python3
"""Tests for pixi-guard. Pure stdlib; run with:
    pixi run test            # this machine's sh and awk
    pixi run test-matrix     # again under every awk/sh found (gawk, gawk --posix, mawk, busybox, dash)
Hermetic: the real hooks/guard.sh runs with HOME, PIXI_GUARD_HOME and
PIXI_GUARD_WRAP_DIR in a temp dir and PIXI_GUARD_PIXI pointing at a fake pixi
(or at nothing, to play a machine without pixi). No network.
"""
import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
GUARD = ROOT / "hooks" / "guard.sh"
PIXI_SH = ROOT / "hooks" / "pixi.sh"
# (no "bash" or "zsh" in its path: the shell prefix tests take a path that holds one for that shell)
for _ in range(20):
    TMP = Path(tempfile.mkdtemp(prefix="pixi-guard-test-")).resolve()
    if "bash" not in str(TMP) and "zsh" not in str(TMP):
        break
    TMP.rmdir()
else:
    sys.exit(f"pixi-guard tests: the temp folder's path holds bash or zsh, which the shell prefix tests read: {TMP}")
FAKE_PIXI = TMP / "bin" / "pixi"
FAKE_PIXI.parent.mkdir()
FAKE_PIXI.write_text('#!/bin/sh\necho "fake pixi $*"\n')
FAKE_PIXI.chmod(0o755)
PLAIN = TMP / "plain"  # a folder with no project markers
PLAIN.mkdir()


def pixi_bin(d):
    """Is PATH entry d a pixi environment's bin (the hook lets tools that resolve there through)?"""
    p = Path(d)
    home = os.environ.get("PIXI_HOME")
    return (p.name == "bin" and (p.parent.parent.name == "envs" and p.parent.parent.parent.name == ".pixi"
                                 or p.parent.name == ".pixi" or (p.parent / "conda-meta" / "pixi").exists()
                                 or (home and p.parent == Path(home))))


# PATH without pixi environments (`pixi run test` puts this repo's own first), so that python, pytest, ...
# resolve outside pixi unless a test puts a pixi bin folder on it.
BASE_PATH = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not pixi_bin(d))
# PATH without any pixi on it either, for the "find pixi off PATH" tests.
NOPIXI_PATH = os.pathsep.join(d for d in BASE_PATH.split(os.pathsep) if not os.access(os.path.join(d, "pixi"), os.X_OK))
SCRUB = ("PIXI_GUARD_TOOLS", "PIXI_GUARD_PIXI", "PIXI_GUARD_HOME", "PIXI_GUARD_WRAP_DIR", "PIXI_GUARD_WATCHDOG", "PIXI_GUARD_JOB_SH", "PIXI_GUARD_SHELL_DIRS", "PIXI_HOME",
         "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "XDG_STATE_HOME", "XDG_DATA_HOME",
         # read by the hook: Claude Code's shell (bash 3.x reads $(...) its own way) and its prefix, the active envs
         "SHELL", "CLAUDE_CODE_SHELL", "CLAUDE_CODE_SHELL_PREFIX", "VIRTUAL_ENV", "CONDA_PREFIX")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        # (on one line, so the matrix, which keeps only the FAIL lines, shows all of it)
        d = str(detail)
        print(f"  FAIL {name} {d if chr(10) not in d else repr(d)}")


CPU = 4  # seconds of CPU one hook call may take in the budget checks (about 1 s for 256KB; the watchdog is at 7 s)


def timed(fn, *a, **kw):
    """fn's result, the CPU time of the processes it ran (the hook's) and its wall time."""
    r0, t0 = resource.getrusage(resource.RUSAGE_CHILDREN), time.time()
    out = fn(*a, **kw)
    r1, wall = resource.getrusage(resource.RUSAGE_CHILDREN), time.time() - t0
    return out, (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime), wall


def budgeted(fn, *a, cpu=CPU, wall=None, **kw):
    """timed() for a budget check: over cpu (or wall) seconds, fn runs once more and the smaller
    times are kept. CPU time is steadier than wall time under machine load, but a busy machine
    may run the hook on slower cores; quadratic code is far over budget (or stopped by the 7 s
    watchdog) both times."""
    out, c, w = timed(fn, *a, **kw)
    if c >= cpu or (wall is not None and w >= wall):
        out, c2, w2 = timed(fn, *a, **kw)
        c, w = min(c, c2), min(w, w2)
    return out, c, w


def remeasured(fn, *a, wall, **kw):
    """Runs of fn (which returns its wall time third), first first, for a check whose upper wall-time
    bound only says the machine was quick enough: over wall seconds, fn runs once more, like budgeted().
    Load slows both runs alike, and a scan nothing stops is just as slow again. The check holds every
    run to what it proves, takes the smaller wall time for the upper bound, and lower bounds (what shows
    something happened) from the first run alone."""
    rs = [fn(*a, **kw)]
    if rs[0][2] >= wall:
        rs.append(fn(*a, **kw))
    return rs


def env(pixi=True, **kw):
    e = {k: v for k, v in os.environ.items() if k not in SCRUB}
    e.update(PIXI_GUARD_HOME=str(TMP / "state"), PIXI_GUARD_WRAP_DIR=str(TMP / "wrap"), HOME=str(TMP), SHELL="/bin/zsh",
             PATH=BASE_PATH)
    if pixi is not None:
        e["PIXI_GUARD_PIXI"] = str(FAKE_PIXI) if pixi else str(TMP / "no-such-pixi")
    for k, v in kw.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def run(mode, payload, pixi=True, raw=None, **kw):
    data = raw if raw is not None else json.dumps(payload)
    try:
        r = subprocess.run(["sh", str(GUARD), mode], input=data, capture_output=True,
                           text=True, timeout=60, env=env(pixi, **kw))
    except subprocess.TimeoutExpired:
        return None, "timed out"  # (Claude Code would have let the command run)
    out = r.stdout.strip()
    try:
        return (json.loads(out) if out else None), r.stderr
    except ValueError:
        return {"invalid JSON": out}, r.stderr


def pre(cmd, cwd=PLAIN, sid="s1", tool="Bash", **kw):
    return run("pre", {"session_id": sid, "transcript_path": "/x", "cwd": str(cwd),
                       "hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": {"command": cmd, "description": "d"}}, **kw)


def verdict(out, err):
    if err:
        return "stderr:" + err
    if out and "invalid JSON" in out:
        return "invalid JSON: " + out["invalid JSON"]
    return out["hookSpecificOutput"]["permissionDecision"] if out else "pass"


def decision(cmd, **kw):
    return verdict(*pre(cmd, **kw))


def why(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "") if out else ""


def reason(cmd, **kw):
    return why(pre(cmd, **kw)[0])


# checkpoint's job tool (plugin check, MCP server jobs): input {name, command | argv, cwd, redo_hint, task}.
JOB = "mcp__plugin_check_jobs__job_start"


def job(command=None, argv=None, cwd=None, session_cwd=PLAIN, sid="s1", **kw):
    ti = {"name": "train", "redo_hint": "rerun; resumes from checkpoints/"}
    if command is not None:
        ti["command"] = command
    if argv is not None:
        ti["argv"] = argv
    if cwd is not None:
        ti["cwd"] = str(cwd)
    ti["task"] = "train-model"
    return run("pre", {"session_id": sid, "transcript_path": "/x", "cwd": str(session_cwd),
                       "hook_event_name": "PreToolUse", "tool_name": JOB, "tool_input": ti}, **kw)


def jdecision(**kw):
    return verdict(*job(**kw))


def jreason(**kw):
    return why(job(**kw)[0])


def hit(cmd, **kw):
    m = re.search(r"`([^`]*)` would run", reason(cmd, **kw))
    return m.group(1) if m else None


def expect(want, cmds, tag="", **kw):
    for cmd in cmds:
        got = decision(cmd, **kw)
        check(f"{want:5} {tag}{cmd!r}", got == want, got)


def session(pixi=True, **kw):
    out, err = run("session", {"session_id": "s1", "hook_event_name": "SessionStart", "source": "startup"},
                   pixi=pixi, **kw)
    return out or {}, err


def wrap_path(d, **kw):
    e = env(True, **kw)
    return subprocess.run(["sh", str(GUARD), "wrap-path", str(d)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=e).stdout.strip()


def matrix():
    """Run this file again under each awk / sh found, through PATH shims."""
    shims = TMP / "shims"
    variants = [("default", {})]

    def which(*names):
        for n in names:
            p = shutil.which(n)
            if p:
                return p
        return None

    gawk, mawk, bbox, dash = which("gawk"), which("mawk"), which("busybox"), which("dash")
    bwk = which("original-awk", "bwk-awk", "one-true-awk", "nawk")
    if gawk:
        variants += [("gawk", {"awk": [gawk]}), ("gawk --posix", {"awk": [gawk, "--posix"]})]
    if mawk:
        variants.append(("mawk", {"awk": [mawk]}))
    if bbox:
        variants.append(("busybox awk", {"awk": [bbox, "awk"]}))
    if bwk:
        variants.append(("bwk awk", {"awk": [bwk]}))
    if dash:
        variants.append(("dash", {"sh": [dash]}))
        if gawk:
            variants.append(("dash + gawk --posix", {"sh": [dash], "awk": [gawk, "--posix"]}))
    bad = []
    for name, tools in variants:
        d = shims / re.sub(r"[^a-z0-9]+", "-", name)
        d.mkdir(parents=True)
        for tool, argv in tools.items():
            f = d / tool
            f.write_text("#!/bin/sh\nexec " + " ".join(f"'{a}'" for a in argv) + ' "$@"\n')
            f.chmod(0o755)
        e = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}", PIXI_GUARD_VARIANT=name)
        print(f"=== {name}")
        r = subprocess.run([sys.executable, __file__], env=e, capture_output=True, text=True)
        lines = r.stdout.strip().splitlines() or ["(no output)"]
        fails = [l for l in lines if l.startswith("  FAIL")]
        print("\n".join(lines[:1] + fails + lines[-1:]))
        if r.returncode:
            bad.append(name)
    print(f"\nmatrix: {len(variants) - len(bad)}/{len(variants)} variants green" + (f"; failed: {bad}" if bad else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if bad else 0


if "--matrix" in sys.argv:
    sys.exit(matrix())

if os.environ.get("PIXI_GUARD_VARIANT"):
    print(f"variant {os.environ['PIXI_GUARD_VARIANT']}: sh={shutil.which('sh')} awk={shutil.which('awk')}")


print("scanner: refused")
expect("deny", [
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
    "cd /tmp\npython x", "X=1 Y=2 python x", "echo ok # fine\npip install x", "pypy3 x.py",
])

print("review probes: refused")
expect("deny", [
    'PYTHONPATH="$PWD/src" python -m pkg', "PYTHONPATH='src' pytest -q", 'FOO="a b" pip install x',
    "ls *.txt | xargs -I {} python {}", "xargs -I % python % < list", "xargs -d '\\n' python < list",
    "env -u PYTHONPATH python x", "env -C sub python x", "env -S 'python3 -u' x",
    "timeout -s KILL 5 python x", "timeout -k 5 10 pytest", "sudo -iu bob python x",
    "command -p python3 x", "exec -a foo python x", "bash -o pipefail -c 'python x | tee log'",
    "bash --rcfile /dev/null -c 'pip install x'", 'read a b <<< "$line"\npython x',
    "cat <<< hi\npip install x", "2>/dev/null python x", ">out.log python x",
    "(( n = 1 << 3 ))\npython x", "git commit -m \"$(cat <<'EOF'\nDon't break it\nEOF\n)\" && python x",
    "watch -n 5 python x", "coproc python x", "su -c 'python x' me", "nice -n5 python x",
    "sudo --preserve-env=PATH python x", "echo hi |& python -", "python3.12 x", "./.venv/bin/python x",
    "VIRTUAL_ENV=.venv .venv/bin/pip install x", "exec python x", "nohup python x > log 2>&1 &",
    "time -p python x", "builtin command python x", "\\python x", "p'y'thon x", "{ python x; }",
    "f(){ python x; }; f", "case $x in a) python x;; esac", "diff <(pip freeze) req.txt",
    "sudo -E env PATH=$PATH python x", "true && \\\npython x", "echo $((2*(3+4))) && python x",
    "cat > f.py <<'EOF'\nprint(\"it's\")\nEOF\npython f.py", "uv run x.py", "ipython3",
    "python -c \"print(')')\"; pip list",
])

print("review probes: allowed")
expect("pass", [
    "git commit -m \"$(cat <<'EOF'\nDon't use pip (it's slow)\n- run `pip install -e .` instead\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\nSteps: 1) replace `pip install -e .` with `pixi install`\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\nFixed it :) now \"pixi\" works\npytest passes\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nRemove pip; it's gone\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nSwitch CI to pixi (it's faster than `pip install`)\nEOF\n)\"",
    "brew info python", "brew list --versions python@3.12", "brew --prefix python@3.12",
    "apt-get install -y pipewire", "apt install uvcdynctrl", "brew uses --installed python",
    "brew install pixi", "ls /opt/python3/bin", "cd python && ls", "mkdir -p src/python",
    "which python", "type python3", "command -v pip", "echo pip", "grep -rn \"import pip\" .",
    "cat > x.py <<'EOF'\nimport os\nEOF\npixi run python x.py", "eval \"$(pixi shell-hook)\"",
    "curl -fsSL https://pixi.sh/install.sh | sh", "pixi run -e dev pytest", "vim app.py",
    "open notebook.py", "sed -i '' 's/python/pixi/' file", "export PYTHONPATH=src",
    "docker run img python x", "ssh host python x", "rg -- -exec pip", "echo \"it's\" && echo `echo ok`",
    "git log --grep='pip'", "echo hi # python x", "printf '%s\\n' \"a)b\" \"pip\"", "tr -d '()' < f",
    "man python3", "brew install --cask docker", "brew install pinentry-mac",
])

print("scanner: allowed")
expect("pass", [
    "pixi run python x.py", "pixi run pytest", "pixi add --pypi requests", "pixi exec python -V",
    "cd a && pixi run python x", "/opt/local/bin/pixi run python x", "git commit -m 'pip install x'",
    "git commit -m \"fix; python thing | uv\"", "echo python", "echo \"python x\"", "grep -r pip .",
    "which python3", "command -v python3", "type -p pip", "ls *.py", "cat setup.py",
    "chmod +x run.py", "rg 'import numpy' src", "cat > notes.md <<EOF\npip install x\npython y\nEOF",
    "git commit -m \"$(cat <<'EOF'\nUse pip install, don't run python\nEOF\n)\"",
    "cat <<-EOF\n\tpip install x\n\tEOF\necho done", "ruff check .", "node build.js",
    "echo ${PYTHON:-python}", "npm run python-docs", "brew install jq", "pixi run bash -c 'pip list'",
    "",
])

print("monitor and terminal tools")
TERMINAL = "mcp__terminal__run_in_terminal"  # the desktop app's terminal tool; its input key is also "command"
check("Monitor command is checked like Bash", decision("python3 -c 1", tool="Monitor") == "deny")
check("Monitor near miss passes", decision("tail -f log | grep --line-buffered python", tool="Monitor") == "pass")
check("terminal tool command is checked like Bash", decision("uvicorn app:app --reload; python manage.py runserver", tool=TERMINAL) == "deny")
check("terminal tool: pixi run passes", decision("pixi run python manage.py runserver", tool=TERMINAL) == "pass")
out, err = run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Monitor",
                       "tool_input": {"ws": {"url": "wss://x"}, "description": "d", "timeout_ms": 1000}})
check("Monitor without a command: no output", out is None and not err, (out, err))
hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
names = [n.strip() for n in re.split(r"[|,]", hooks["PreToolUse"][0]["matcher"])]
# the gateway's tools (plugin gate, MCP server gw): job_start {command | argv, cwd, host, ...}, terminal_send {id, seq, text, ...}
GWJOB, GWSEND = "mcp__plugin_gate_gw__job_start", "mcp__plugin_gate_gw__terminal_send"
check("PreToolUse matcher lists Bash, Monitor, the terminal tool and checkpoint's job_start",
      {"Bash", "Monitor", TERMINAL, JOB} <= set(names) and re.fullmatch(r"[A-Za-z0-9_|]+", hooks["PreToolUse"][0]["matcher"]), names)
check("PreToolUse matcher lists the gateway's job_start and terminal_send", {GWJOB, GWSEND} <= set(names), names)

print("hooks.json: exec form")
for event, mode in (("SessionStart", "session"), ("PreToolUse", "pre")):
    h = hooks[event][0]["hooks"][0]
    args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)) for a in h.get("args", [])]
    check(f"{event} runs sh with args", h["command"] == "sh" and args[1:] == [mode] and Path(args[0]).is_file(), h)
    payload = {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Bash", "tool_input": {"command": "pip list"}}
    r = subprocess.run([h["command"], *args], input=json.dumps(payload), capture_output=True, text=True,
                       env=env(True), cwd=str(TMP))
    check(f"{event} hook runs as Claude Code would", r.returncode == 0 and r.stdout.strip().startswith("{"), r)

print("checkpoint's job_start: its command, else its argv as /bin/sh gets it (checkpoint runs shlex.join(argv) with sh -c)")
# A /bin/sh that is not bash (like dash, it has no --version), so a job reads as a Bash command with zsh as Claude
# Code's shell (the default here); the real /bin/sh is bash 3.2 on macOS (see the bash 3.2 and 5.2 sections).
NOBASH = TMP / "nobash" / "sh"
NOBASH.parent.mkdir()
NOBASH.write_text("#!/bin/sh\necho 'sh: 0: Illegal option --' >&2\nexit 2\n")
NOBASH.chmod(0o755)
JSH = {"PIXI_GUARD_JOB_SH": str(NOBASH)}
JOB_PAIRS = [
    # (command, the same as argv, decision)
    ("python train.py", ["python", "train.py"], "deny"),
    ("pixi run python train.py", ["pixi", "run", "python", "train.py"], "pass"),
    ("python3 -u train.py --epochs 10", ["python3", "-u", "train.py", "--epochs", "10"], "deny"),
    ("pixi run -e cuda python3 -u train.py", ["pixi", "run", "-e", "cuda", "python3", "-u", "train.py"], "pass"),
    ("pytest -q tests", ["pytest", "-q", "tests"], "deny"),
    ("pixi run pytest -q tests", ["pixi", "run", "pytest", "-q", "tests"], "pass"),
    (".venv/bin/python train.py", [".venv/bin/python", "train.py"], "deny"),
    ("/usr/bin/python3 train.py", ["/usr/bin/python3", "train.py"], "deny"),
    ("env python train.py", ["env", "python", "train.py"], "deny"),
    ("env CUDA_VISIBLE_DEVICES=0 pixi run python train.py", ["env", "CUDA_VISIBLE_DEVICES=0", "pixi", "run", "python", "train.py"], "pass"),
    ("timeout 3600 python train.py", ["timeout", "3600", "python", "train.py"], "deny"),
    ("timeout 3600 pixi run python train.py", ["timeout", "3600", "pixi", "run", "python", "train.py"], "pass"),
    ("sudo -n pip install torch", ["sudo", "-n", "pip", "install", "torch"], "deny"),
    ("nice -n 10 uv run train.py", ["nice", "-n", "10", "uv", "run", "train.py"], "deny"),
    # a bare NAME=VALUE first is an assignment there too (shlex.join leaves it unquoted)
    ("OMP_NUM_THREADS=4 python train.py", ["OMP_NUM_THREADS=4", "python", "train.py"], "deny"),
    ("OMP_NUM_THREADS=4 pixi run python train.py", ["OMP_NUM_THREADS=4", "pixi", "run", "python", "train.py"], "pass"),
    # code handed to a shell is read as code
    ("sh -c 'python train.py'", ["sh", "-c", "python train.py"], "deny"),
    ("bash -lc 'cd sub && pip install -e .'", ["bash", "-lc", "cd sub && pip install -e ."], "deny"),
    ("bash -c 'pixi run python train.py'", ["bash", "-c", "pixi run python train.py"], "pass"),
    # jobs that run no Python
    ("make -j8 all", ["make", "-j8", "all"], "pass"),
    ("cargo build --release", ["cargo", "build", "--release"], "pass"),
    ("rsync -a data/ backup:data/", ["rsync", "-a", "data/", "backup:data/"], "pass"),
    ("./build.sh --all", ["./build.sh", "--all"], "pass"),
]
for command, argv, want in JOB_PAIRS:
    got = jdecision(command=command)
    check(f"{want:5} job command {command!r}", got == want, got)
    got = jdecision(argv=argv)
    check(f"{want:5} job argv {argv!r}", got == want, got)
# Each argv element is one word, quoted: shell syntax in it is text to /bin/sh. The same as a command runs.
JOB_QUOTED = [
    (["make", "&&", "python", "x"], "make && python x"),
    (["true", "||", "pip", "install", "x"], "true || pip install x"),
    (["echo", "$(pip list)"], "echo $(pip list)"),
    (["echo", "`pytest`"], "echo `pytest`"),
    (["echo", "done;", "python", "x"], "echo done; python x"),
    (["echo", "a\npython x"], "echo a\npython x"),
    (["echo", "it's", "|", "python"], "echo it\\'s | python"),
    (["p'y'thon", "x"], "p'y'thon x"),
]
JSTUBS = "pip() { echo RAN >&2; }; python() { echo RAN >&2; }; pytest() { echo RAN >&2; }\n"
for argv, command in JOB_QUOTED:
    got = jdecision(argv=argv)
    check(f"pass  job argv {argv!r}: one word each", got == "pass", got)
    got = jdecision(command=command)
    check(f"deny  job command {command!r}", got == "deny", got)
    if argv[0] in ("echo", "true", "p'y'thon"):  # (harmless to run) /bin/sh, as checkpoint runs it, runs no Python either
        r = subprocess.run(["/bin/sh", "-c", JSTUBS + shlex.join(argv)], capture_output=True, text=True, timeout=10,
                           cwd=str(PLAIN), stdin=subprocess.DEVNULL)
        check(f"/bin/sh runs nothing in shlex.join({argv!r})", "RAN" not in r.stderr, r.stderr)
# argv reads exactly as Python's shlex.join(argv) does as a command.
for argv in [a for _, a, _ in JOB_PAIRS] + [a for a, _ in JOB_QUOTED] + [
    ["python", "it's here.py"], ["echo", ""], ["", "python"], ["echo", "a b", "c\td", "e\nf"],
    ["python", "-c", "print('hi')"], ["echo", "\\", "\\'", "'", "''", "\"", "$", "${x:-python}", "~", "*", "?", "[a]", "{a,b}", "#", "!"],
    ["env", "A=b c", "python"], ["A=1", "B=x y", "python"], ["PIXI_GUARD=ask", "pip", "list"], ["x=1", "pip"],
    ["é", "python"], ["pythön"], ["sudo", "-u", "o'brien", "pip", "list"], ["sh", "-c", "echo 'it'\"'\"'s'; python x"],
    ["bash", "-c", "git commit -m \"$(cat <<'EOF'\nDon't use pip (it's slow): `pip list`\nEOF\n)\""], ["a@b%c+d=e:f,g.h/i-j_K9", "pip"],
]:
    want = decision(shlex.join(argv))
    got = jdecision(argv=argv, **JSH)
    check(f"job argv {argv!r} reads as the command shlex.join makes of it ({want})", got == want, got)
print("checkpoint's job_start: PIXI_GUARD=ask, refusals, no command")
check("job: PIXI_GUARD=ask in front of the command asks", jdecision(command="PIXI_GUARD=ask python train.py") == "ask")
check("job: PIXI_GUARD=ask as the first argv element asks", jdecision(argv=["PIXI_GUARD=ask", "python", "train.py"]) == "ask")
check("job: PIXI_GUARD=ask before a job that runs no Python: no prompt", jdecision(argv=["PIXI_GUARD=ask", "make"]) == "pass")
check("job: PIXI_GUARD=asked is not an override", jdecision(argv=["PIXI_GUARD=asked", "python", "x"]) == "deny")
check("job: a quoted PIXI_GUARD=ask is not an override", jdecision(argv=["sh", "-c", "echo 'PIXI_GUARD=ask' && python x"]) == "deny")
jws, jpy = TMP / "wsj", TMP / "pyj"  # a pixi workspace, and a Python project not on pixi
jws.mkdir()
(jws / "pixi.toml").write_text("[workspace]\n")
jpy.mkdir()
(jpy / "pyproject.toml").write_text("[project]\nname='p'\n")
r = jreason(argv=["python", "train.py"], session_cwd=jws)
check("job refusal in a pixi workspace: the pixi way, as the job's command (argv: pixi, run first)",
      "make the job's command `pixi run python ...`" in r and '"pixi", "run" first' in r and "run it as" not in r, r)
check("job refusal: PIXI_GUARD=ask in front of the job's command (argv: its first element)",
      "`PIXI_GUARD=ask` in front of its command" in r and "first element" in r and "PIXI_GUARD=ask <command>" not in r, r)
r = jreason(command="python train.py", session_cwd=jws)
check("job refusal for a command: the pixi way, as the job's command", "make the job's command `pixi run python ...`" in r, r)
r = jreason(argv=["pip", "install", "torch"], session_cwd=jws)
check("job refusal for pip: pixi add, not pixi run pip", "pixi add" in r and "pixi run pip" not in r, r)
r = jreason(argv=["pytest", "-q"], session_cwd=jpy)
check("job refusal in a Python project not on pixi: the skill, then the job's command",
      "pixi-guard:project" in r and "Then make the job's command `pixi run pytest ...`" in r, r)
r = jreason(argv=["pip", "install", "torch"], session_cwd=jpy)
check("... for pip: the skill, then pixi add", "pixi-guard:project" in r and "Then manage packages with `pixi add" in r and "pixi run pip" not in r, r)
r = jreason(argv=["python", "train.py"])
check("job refusal with no workspace: pixi exec as the job's command",
      "No pixi workspace here" in r and "The job's command is then that pixi command, such as `pixi exec --spec python python ...`" in r, r)
r = jreason(argv=["python", "x"], pixi=False, sid="jobm1")
check("job refusal without pixi: PIXI_GUARD=ask in front of the job's command",
      "tell the user" in r and "`PIXI_GUARD=ask` in front of its command" in r, r)
check("job ask: names the job", "this job runs `python`" in jreason(argv=["PIXI_GUARD=ask", "python", "x"]))
for d in (jws, jpy, PLAIN):
    r = reason("python train.py", cwd=d)
    check(f"Bash refusals say nothing of jobs ({d.name})", "job" not in r.replace(str(TMP), "") and "PIXI_GUARD=ask <command>" in r, r)
check("Bash refusal in a workspace: run it as pixi run", "run it as `pixi run python ...`" in reason("python train.py", cwd=jws))
check("Bash ask: names the command", "this command runs `python`" in reason("PIXI_GUARD=ask python x"))
for label, kw in (("neither command nor argv", {}), ("an empty command and argv", {"command": "", "argv": []}),
                  ("an empty argv", {"argv": []})):
    out, err = job(**kw)
    check(f"job with {label}: no output", out is None and not err, (out, err))
check("job with an empty command: its argv is checked (checkpoint runs it then)", jdecision(command="", argv=["python", "x"]) == "deny")
check("job argv: the elements before a non-string are checked", jdecision(argv=["python", None]) == "deny")
check("job argv: a non-string first ends it (checkpoint can't run that)", jdecision(argv=[None, "python"]) == "pass")
raw = ('{"session_id":"s1","cwd":' + json.dumps(str(PLAIN)) + ',"tool_name":"' + JOB + '","tool_input":{"name":"t","argv":'
       '["\\u0070ython","\\/tmp\\/x \\"y\\"","a\\\\b\\tc"],"redo_hint":"r"}}')
out, err = run("pre", None, raw=raw)
check("job argv: JSON escapes decode (\\u0070ython is python)", verdict(out, err) == "deny" and "`python`" in why(out), (out, err))
raw = ('{"session_id":"s1","cwd":' + json.dumps(str(PLAIN)) + ',"tool_name":"' + JOB + '","tool_input":{"name":"t","argv":'
       '["echo","\\u0027; python x","\\"$(pip list)\\""],"redo_hint":"r"}}')
out, err = run("pre", None, raw=raw)
check("job argv: a decoded quote stays inside its word", verdict(out, err) == "pass", (out, err))
h = hooks["PreToolUse"][0]["hooks"][0]
r = subprocess.run([h["command"], *[a.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)) for a in h["args"]]],
                   input=json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": JOB,
                                     "tool_input": {"name": "t", "argv": ["pip", "install", "x"], "redo_hint": "r"}}),
                   capture_output=True, text=True, env=env(True), cwd=str(TMP))
check("job_start through hooks.json as Claude Code runs it: deny", r.returncode == 0 and '"deny"' in r.stdout, r)

print("checkpoint's job_start: the job's cwd (a relative one from the session's)")
jobs_dir = TMP / "jobs"
pixi_x, plain_x = jobs_dir / "pixibang", jobs_dir / "plainbang"
for d, line in ((pixi_x, "#!/usr/bin/env -S pixi exec --spec numpy -- python"), (plain_x, "#!/usr/bin/env python3")):
    d.mkdir(parents=True)
    (d / "x.py").write_text(line + "\nprint(1)\n")
    (d / "x.py").chmod(0o755)
for label, kw, want in (
    ("a pixi #! line in the job's cwd (argv)", dict(argv=["./x.py"], cwd=pixi_x), "pass"),
    ("a pixi #! line in the job's cwd (command)", dict(command="./x.py --fast", cwd=pixi_x), "pass"),
    ("a python #! line in the job's cwd", dict(argv=["./x.py"], cwd=plain_x), "deny"),
    ("the job's cwd counts, not the session's (python #! line in the job's)", dict(argv=["./x.py"], cwd=plain_x, session_cwd=pixi_x), "deny"),
    ("the job's cwd counts, not the session's (pixi #! line in the job's)", dict(argv=["./x.py"], cwd=pixi_x, session_cwd=plain_x), "pass"),
    ("a relative cwd, in the session's folder (pixi #! line)", dict(argv=["./x.py"], cwd="pixibang", session_cwd=jobs_dir), "pass"),
    ("a relative cwd, in the session's folder (python #! line)", dict(argv=["./x.py"], cwd="plainbang", session_cwd=jobs_dir), "deny"),
    ("no cwd: the session's (pixi #! line)", dict(argv=["./x.py"], session_cwd=pixi_x), "pass"),
    ("no cwd: the session's (python #! line)", dict(argv=["./x.py"], session_cwd=plain_x), "deny"),
    ("a cwd that does not exist: the session's (checkpoint refuses the job)", dict(argv=["./x.py"], cwd=jobs_dir / "nope", session_cwd=pixi_x), "pass"),
):
    got = jdecision(**kw)
    check(f"{want:5} job ./x.py, {label}", got == want, got)
out, err = run("pre", {"session_id": "s1", "cwd": str(plain_x), "tool_name": "Bash", "tool_input": {"command": "./x.py", "cwd": str(pixi_x)}})
check("Bash: a cwd in its tool_input is not the command's folder (only a job's is)", verdict(out, err) == "deny", (out, err))
jproj = jobs_dir / "proj"
jproj.mkdir()
(jproj / "pixi.toml").write_text("[workspace]\n")
r = jreason(argv=["python", "train.py"], cwd=jproj)
check("job refusal: names the pixi workspace of the job's cwd", f"{jproj} is a pixi workspace" in r, r)
check("job refusal: not the session's folder", f"{jproj} is a pixi workspace" not in jreason(argv=["python", "train.py"], session_cwd=jproj, cwd=PLAIN))

print("budget checks: a run over budget is measured once more, keeping the smaller time (machine load slows runs)")
BURN = "import sys, time\nt = time.process_time()\nwhile time.process_time() - t < float(sys.argv[1]): pass\n"


def runs(*plan):
    """A function whose i-th call spends plan[i] = (CPU seconds in a child, seconds asleep) and returns i."""
    calls = []

    def fn():
        c, s = plan[len(calls)]
        if c:
            subprocess.run([sys.executable, "-c", BURN, str(c)], check=True)
        time.sleep(s)
        calls.append(c)
        return len(calls)
    return fn


n, c, _ = budgeted(runs((0.05, 0), (0.05, 0)), cpu=1)
check(f"under budget: measured once ({c:.2f}s CPU)", n == 1 and c < 1, (n, c))
n, c, _ = budgeted(runs((0.6, 0), (0.05, 0)), cpu=0.4)
check(f"over budget, then under: measured twice, the smaller time kept ({c:.2f}s CPU)", n == 2 and 0.05 <= c < 0.4, (n, c))
n, c, _ = budgeted(runs((0.6, 0), (0.6, 0)), cpu=0.4)
check(f"over budget twice: still over ({c:.2f}s CPU)", n == 2 and c >= 0.6, (n, c))
n, _, w = budgeted(runs((0, 1), (0, 0)), wall=0.8)
check(f"over a wall-time budget, then under: measured twice, the smaller time kept ({w:.2f}s)", n == 2 and w < 0.8, (n, w))
rs = remeasured(timed, runs((0, 0), (0, 0)), wall=0.3)
check("under an upper wall-time bound: run once", [t[0] for t in rs] == [1], rs)
rs = remeasured(timed, runs((0, 0.5), (0, 0)), wall=0.3)
check(f"over it, then under: run twice, both results kept, first first ({rs[0][2]:.2f}s, {rs[-1][2]:.2f}s)",
      [t[0] for t in rs] == [1, 2] and rs[0][2] >= 0.3 > rs[1][2], rs)
rs = remeasured(timed, runs((0, 0.5), (0, 0.5)), wall=0.3)
check("over it twice: still over", [t[0] for t in rs] == [1, 2] and min(t[2] for t in rs) >= 0.3, rs)

print("checkpoint's job_start: big argv lists stay linear; a list too long to read is refused")
for label, payload, want, big in (
    ("80k words", dict(argv=["python"] + ["ab"] * 80000), "deny", False),
    ("20k words with apostrophes", dict(argv=["echo"] + ["a'b c"] * 20000), "pass", False),
    ("one 300KB word", dict(argv=["echo", "a" * 300000]), "deny", True),
    ("100k words, 300KB", dict(argv=["make"] + ["ab"] * 100000), "deny", True),
    ("PIXI_GUARD=ask, 300KB", dict(argv=["PIXI_GUARD=ask", "make"] + ["ab"] * 100000), "ask", True),
    ("1.8MB of \\u escapes", '["echo","' + "\\u0061" * 300000 + '"]', "deny", True),
    ("1.7MB of blanks in the list (cut before its ])", '["ls",' + " " * 1700000 + '"python"]', "deny", True),
    ("0.9MB after the list's ]", '["ls"],"x":"' + "a" * 900000 + '"', "pass", False),
):
    if isinstance(payload, str):
        data = ('{"session_id":"s1","cwd":' + json.dumps(str(PLAIN)) + ',"tool_name":"' + JOB
                + '","tool_input":{"name":"t","argv":' + payload + ',"redo_hint":"r"}}')
        call = lambda: run("pre", None, raw=data)  # noqa: E731
    else:
        call = lambda: job(**payload)  # noqa: E731
    (out, err), cpu, _ = budgeted(call)
    got, r = verdict(out, err), why(out)
    check(f"job argv, {label}: {want}{' (too large)' if big else ''}, {cpu:.2f}s CPU",
          got == want and (("too large" in r) == big or want == "pass") and cpu < CPU, (got, r[:80]))

print("assignments")
expect("deny", ["X+=1 python x", "A='x y' B=\"$(pwd)\" pip list"])
expect("pass", ["'PYTHONPATH=x' python", "PYTHONPATH=\"src\"python x", "FOO=python", "X=\"python x\"", "pkgs=(python pip); echo \"${pkgs[@]}\""])

print("escalation")
check("PIXI_GUARD=ask prefix asks", decision("PIXI_GUARD=ask python x") == "ask")
check("PIXI_GUARD=\"ask\" asks", decision('PIXI_GUARD="ask" python x') == "ask")
check("PIXI_GUARD='ask' asks", decision("PIXI_GUARD='ask' pip list") == "ask")
check("ask on a later segment", decision("cd a && PIXI_GUARD=ask pip install x") == "ask")
check("quoted PIXI_GUARD=ask is not an override", decision("echo 'PIXI_GUARD=ask' && python x") == "deny")
check("PIXI_GUARD=asked is not an override", decision("PIXI_GUARD=asked python x") == "deny")
check("ask reason names the program", "`pip`" in reason("PIXI_GUARD=ask pip install x"))
check("deny reason explains the override", "PIXI_GUARD=ask <command>" in reason("python x"))

print("heredocs in $(...)")
msg = "Don't use pip (it's slow)\n- run `pip install -e .` instead\nSteps: 1) ok\n$(not run)"
expect("pass", [
    f"git commit -m \"$(cat <<'EOF'\n{msg}\nEOF\n)\"",
    f"git commit -m \"$(cat <<'EOF'\n{msg}\nEOF)\"",
    f"gh pr create --body \"$(cat <<-'EOF'\n\t{msg}\n\tEOF\n)\" --title t",
    "x=$(cat <<'EOF'\nit's (fine\nEOF\n); echo \"$x\"",
    "echo \"$(cat <<'A' <<'B'\nit's\nA\n(\nB\n)\"",
    "echo $(echo hi # it's a comment\n) && ls",
])
expect("deny", [
    f"git commit -m \"$(cat <<'EOF'\n{msg}\nEOF\n)\" && python x",
    f"git commit -m \"$(cat <<'EOF'\n{msg}\nEOF)\" && pip list",
    "x=$(cat <<'EOF'\nit's (fine\nEOF\n); python y",
    "echo \"$(cat <<'EOF'\n)\nEOF\n)\" && pip list",
    "echo $(echo hi # it's a comment\n) && python x",
])

print("heredoc bodies")
expect("deny", [
    "cat <<EOF\n$(pip freeze)\nEOF", "cat > r.txt <<EOF\n`pip --version`\nEOF",
    "bash -s -- arg <<EOF\npython x\nEOF", "sh <<'EOF'\npip install x\nEOF",
    "cat <<A; cat <<B\nx\nA\n$(python y)\nB", "cat <<EOF\nfine\nEOF\npython x",
])
expect("pass", [
    "cat <<'EOF'\n`pip install x` and $(python y)\nEOF", "cat <<\"EOF\"\n$(python y)\nEOF",
    "cat <<\\EOF\n$(python y)\nEOF", "cat <<EOF\nuse pip install x\nEOF", "cat <<EOF\n\\$(python y)\nEOF",
    "cat <<EOF\n$(( 1 + 2 )) and ${HOME}\nEOF", "bash script.sh <<EOF\npython x\nEOF",
    "echo \"$(bash <<'X'\necho hi\nX\n)\" \"$(<<'Y'\npip install x\nY\n)\"",
])

print("here-strings and arithmetic")
expect("deny", ["python3 <<< 'print(1)'", "echo $(( 1 << 3 ))\npython x", "for ((i = 0; i < 3; i++)); do python x; done",
                "((echo hi) ); python x", "echo $(( $(python -c 1) + 1 ))", "((n = $(pip list | wc -l)))"])
expect("pass", ["echo $(( python + 1 ))", "(( pip = 1 ))", "echo \"$(( pip * 2 ))\"", "for ((python = 0; python < 3; python++)); do :; done",
                "let \"x = 1 << 2\"", "echo $(( 1 << 3 ))\nls", "(( n = 1 << 3 ))\nls",
                "x=$(( (1 + 2) * 3 ))", "cat <<< \"$(echo python)\""])

print("wrapper options")
expect("deny", [
    "xargs -n1 -P4 python", "xargs -0 -I{} python {}", "xargs --max-args 1 python", "xargs -i python {}",
    "env -S python3 x", "env --split-string='pip list'", "env -iS 'pip install' x", "env - python x",
    "env --chdir sub python x", "env -- python x", "sudo -u bob -g staff python x", "sudo -ubob python x",
    "sudo -- python x", "sudo -Eu bob python x", "sudo -nHu bob pip list", "doas -u bob python x", "timeout --signal KILL 5 python x", "gtimeout -k5 10 pytest",
    "nice -5 python x", "nice --adjustment 5 python x", "ionice -c 3 python x", "stdbuf -o L python x",
    "stdbuf -oL python x", "caffeinate -t 60 python x", "arch -x86_64 python x", "arch -arch x86_64 python x",
    "exec -c python x", "command -- python x", "time -f %e python x", "busybox sh -c 'python x'",
    "bash -euo pipefail -c 'python x'", "bash -O extglob -c 'python x'", "bash +o posix -c 'python x'",
    "bash --init-file f -c 'python x'", "zsh -o errexit -c 'pip list'", "sh -c -- 'python x'",
    "bash -c -x 'python x'", "fish -c 'python x'", "fish --command='pip list'", "fish -C 'pip list'",
    "watch 'pip list | head'", "watch -n 2 -- python x", "su - bob -c 'python x'", "su --command='pip list' bob",
    "runuser -u bob -- sh -c 'python x'", "sudo sh -c 'pip install x'",
    "runuser -u bob python x", "runuser -u bob -g staff pip list", "runuser --user=bob python x",
    "runuser --user bob -- python x", "runuser -ubob python x", "runuser -l bob -c 'python x'",
    "su -s /bin/bash -c 'pip list' bob", "su -lc 'python x' bob",
])
expect("pass", [
    "timeout 5 grep python log", "xargs grep python", "env | grep python", "nice -n 5 make",
    "command -pv python", "exec -a python make", "sudo -e setup.py", "sudo -l python", "xargs -I python echo hi",
    "watch -n 5 ls", "su -c 'pixi run python x' me", "bash script.sh", "bash -x deploy.sh",
    "sh -c 'pixi run pytest'", "env -u PYTHONPATH make", "stdbuf -oL make", "time -p make", "env -S echo 'a;python x'",
    "env -S echo \"it's;python x\"", "runuser -u bob ls", "runuser -l bob -c 'ls'", "su -s /bin/bash -c 'ls' bob",
    "su - python",
])

print("redirections")
expect("deny", ["2>&1 >/dev/null python x", "{fd}>log python x", "</dev/null pip list", "&>/dev/null python x",
                "exec 3>&1; python x", "python x 2>/dev/null", ">>log 2>&1 pip install x"])
expect("pass", ["echo 2>/dev/null python", "ls >python.log", "cat <python.txt", "exec 3>&1"])

print("package managers")
expect("deny", [
    "brew install python", "brew reinstall python@3.12", "brew upgrade pyenv", "brew install --cask miniconda",
    "sudo apt install python3", "apt-get -y install python3-venv", "apt -o Debug=1 install python3",
    "dnf install -y python3.11", "yum install python3-pip", "pacman -S python", "pacman -Syu python-pip",
    "zypper in python311", "apk add py3-pip", "nix-env -iA nixpkgs.python3", "port install py312-numpy",
    "winget install -e --id Python.Python.3.12", "choco install python", "scoop install python",
    "brew install pyenv-virtualenv", "brew install homebrew/core/python@3.11", "apt install python3=3.11.2-1",
])
expect("pass", [
    "apt list --installed python3", "apt search python3", "apt-cache show python3", "dnf info python3",
    "pacman -Ss python", "pacman -Si python", "pacman -Qi python", "port search python", "brew upgrade",
    "brew install pipe-viewer", "apt install pipewire-audio", "brew install uvwasi", "apt install condor",
    "brew install pyright-langserver-not-py", "nix-env -q python3", "brew search python",
])

print("venv and conda console scripts")
expect("deny", ["./.venv/bin/uvicorn app:app", "venv/bin/flask run", ".venv/bin/gunicorn -w 4 app",
                "~/miniconda3/bin/flask run", "/opt/miniforge3/envs/x/bin/streamlit run a",
                "~/.virtualenvs/proj/bin/celery worker", "/home/me/proj/.venv311/bin/alembic upgrade head"])
expect("pass", ["ls .venv/bin/", "cat venv/bin/activate", "rm -r .venv", "ls ~/miniconda3/bin"])

print("find / fd / git")
expect("deny", ["find . -execdir python {} \\;", "find . -ok pip show {} \\;", "gfind . -exec pytest {} +",
                "find . -exec sh -c 'python \"$1\"' _ {} \\;", "fd -e py -x black {}", "fd . -X pytest",
                "fd --exec-batch python {}", "fd -x sh -c 'pip install {}'", "git rebase -i --exec 'python x' main",
                "git rebase -x 'pytest -q' main", "git rebase --exec='pip list' main", "git bisect run python t.py"])
expect("pass", ["rg -- -exec pip", "grep -e -exec python", "find . -exec echo python {} \\;", "find . -name '*.py'",
                "fd -e py", "git rebase -i main", "git rebase --exec 'pixi run pytest' main", "git log -x python",
                "git bisect start"])

print("other syntax")
expect("deny", ["echo ${X:-$(python -V)}", "$'python' x", "$\"python\" x", "echo \"`pip list`\"",
                "a=(python pip); python x", "x=$(python -V)", "if [[ -f x ]]; then pip list; fi",
                "while read l; do python \"$l\"; done < f", "! python x", "until python x; do :; done"])
expect("pass", ["echo $'it\\'s' python", "echo '$(python x)'", "echo \\$PATH python", "echo $#", "echo ${#x} python",
                "echo {python,pip}", "printf '%s\\n' \"$(echo ')')\""])

print("$'...' inside $(...)")
expect("pass", ["echo $(printf $'it\\'s') pip", "echo \"$(printf $'it\\'s') pip\"", "a=($'it\\'s' \"b)c\"); echo ok",
                "x=$(printf $'a)b'); echo \"$x\""])
expect("deny", ["x=$(printf $'it\\'s'); python x", "echo $(printf $'it\\'s'; pip list)", "a=($'it\\'s' $(pip list))",
                "echo ${x:-$'it\\'s'}; python y"])

print("heredocs piped into a shell")
expect("deny", ["cat <<EOF | bash\npip install x\nEOF", "cat <<'EOF' | sh\npython x\nEOF",
                "cat <<EOF |& sudo bash -s\npip list\nEOF", "cat <<EOF | grep -v '^#' | bash\npip list\nEOF",
                "cat <<'A' | cat - <<'B' | bash\nls\nA\npython x\nB", "cat <<'EOF' | bash -s -- a\npython x\nEOF"])
expect("pass", ["cat <<EOF | grep pip\npip install x\nEOF", "cat <<'EOF' | tee f.txt\npython x\nEOF",
                "cat <<EOF || bash x.sh\npip install x\nEOF", "cat <<EOF; bash x.sh\npip install x\nEOF",
                "cat <<EOF | bash x.sh\npip install x\nEOF", "cat <<EOF && bash\npip install x\nEOF",
                "cat <<EOF || bash\npip install x\nEOF"])

print("here-strings into a shell")
expect("deny", ["bash <<< 'pip install x'", "sh <<< \"python x\"", "bash -s <<< 'pip list'", "<<< 'pip list' bash",
                "sudo bash <<< 'python x'"])
expect("pass", ["bash <<< 'echo hi'", "cat <<< 'pip install x'", "bash script.sh <<< 'pip'", "grep -c pip <<< \"$x\""])

print("${...} quoting")
expect("deny", ["echo ${x:-\"}\"}; python y", "echo \"${x:-'}'}\"; python y", "echo \"${x:-'$(python x)'}\"",
                "echo ${x:-\"$(python x)\"}", "echo ${x:-`pip list`}"])
expect("pass", ["echo ${x:-'$(python x)'}", "echo ${x:-\"}\"} pip", "echo \"${x:-'}'}\" pip", "echo ${x//\\}/python}"])

print("$[...], nested backticks, brace expansion")
expect("deny", ["echo $[1<<2]\npython x", "echo $(echo $[1<<2]\npython x)", "echo `echo \\`python x\\``",
                "echo `echo \\$(pip list)`", "{python,pip} x", "py{thon,} x", "{python,}3 -c 1"])
expect("pass", ["echo $[1<<2]\nls", "echo `echo \\\\$HOME` pip", "echo `printf 'a\\\\nb'` pip", "echo {python,pip}",
                "ls {a,b}.txt", "a=('$(python x)' b); echo \"${a[@]}\""])
expect("deny", ["a=(\"$(python x)\" b)"])

print("case statements")
expect("pass", [
    "echo $(case $x in a) echo 1;; esac) pip", "case $x in pip|uv) echo hi;; python) ls;; esac",
    "case \"$t\" in\n  pip) echo a ;;\n  (uv|poetry) echo b ;;\n  *) echo c ;;\nesac",
    "x=$(case $t in pip) echo 1;; (uv) echo 2;; *.@(py|sh)) echo 3;; esac); echo \"$x\"",
    "echo \"$(case $t in pip) echo 1;; esac)\" pip", "case $x in a) echo;;& b) echo;& pip) echo;; esac",
    "case $x in\n# a comment with pip)\npip) echo;;\nesac", "case $x in\na) echo;;\nesac",
])
expect("deny", [
    "case $x in a) python x;; esac", "case $x in pip) echo;; esac; python x", "case $x in a) echo;; esac\npip list",
    "echo $(case $x in a) python x;; esac)", "echo $(case $x in a) echo 1;; esac) && pip list",
    "x=$(case $t in pip) echo 1;; esac); python y", "case $(python -V) in a) echo;; esac",
    "case $x in $(pip list)) echo;; esac", "case $x in a) bash <<EOF;;\npython x\nEOF\nesac",
    "case $x in a) echo;; b) pip install x;; esac", "case x in a) ls\nesac\npython x", "echo case x in a) ; python x",
    "if true; then case $x in a) python y;; esac; fi", "echo $(echo case in a) && pip list",
    "case $x in a) echo;; esac&&python x",
])
# An escaped character is part of a word: \(case is no keyword and \(# no comment, so a ")" after them ends the
# $(...) (the real shells agree, see below).
esc_deny = ["x=\"$( \\(case x in x) \" ; pip list", "x=\"$( \\(#)\" ; pip list", "x=\"$(echo a\\;case x in x) \" ; pip list",
            "x=$(echo \\(x # )\npip list\n)"]
esc_pass = ["x=\"$( \\(case x in x) \" ; ls", "x=\"$( \\(#)\" ; ls", "x=\"$(echo \\\\(case x in x) echo ;; esac)\" ; ls"]
expect("deny", esc_deny, "escaped: ")
expect("pass", esc_pass, "escaped: ")

print("a case pattern with no \")\" is a syntax error, not a command")
expect("pass", ["case $t in pip", "case $t in\npip", "echo $(case x in pip)"])
expect("deny", ["case $t in pip) ;; esac; pip list", "case $t in pip) ;; esac\npython3 x", "case $t in a) python3 x;; pip"])

print("[[ ... ]]")
expect("pass", ["[[ $x =~ ^(pip|uv)$ ]] && echo y", "if [[ $t =~ (python|pip)[0-9]* ]]; then echo y; fi",
                "[[ $x =~ (a|b) ]] || [[ $y =~ (pip|uv) ]]", "! [[ $x =~ (pip) ]] && ls"])
expect("deny", ["[[ $x =~ (a|b) ]] && python x", "[[ $(python -V) == x ]]", "[[ a < b ]] && pip list",
                "[[ x ]] || python x", "[[ x ]] | python", "[[ x ]]\npython x", "echo [[ | python x"])

print("[[ ... ]] ends at an operator")
expect("deny", ["[[ -n x ]]|python3 z", "[[ -n x ]]>/dev/null; echo | python3 z", "[[ -n x ]]</dev/null; echo | python3 z",
                "([[ -n x ]]); (python3 z)", "echo ok; [[ -n x ]]|python3 -c \"print(777)\"", "[[ -n x ]]||python3 z",
                "case x in [[) echo;; esac; python3 z", "case x in [[) echo;; *) python3 z;; esac",
                "case x in [[ | a) echo;; esac | python3 z",
                # where [[ is not a keyword (dash): an unmatched ")", or a ; & newline, ends it too
                "([[ -n x ) || (python3 z)", "[[ -n x]]; echo | python3 z", "[[ -n $[ ]]\necho 'python3 z' | sh"])
expect("pass", ["[[ -n x ]]>/dev/null; cat <<E\npip\nE", "[[ $x =~ ^(pip|uv)$ ]]&&echo y", "case x in [[) echo pip;; esac",
                "[[ ( -n x ) && ( -z y ) ]] && echo pip", "[[ -n x && $y =~ (pip|uv) ]] && echo ok"])

print("${...}: where bash, zsh and dash end it")
expect("deny", [
    # bash: single quotes count in "${...}"
    "x=; echo \"${x:-'}\"'}\"; python3 z", "x=; echo \"${x:-'\"'}\"; python3 z",
    # zsh and dash: they do not
    "x=; echo \"${x:-'}\"; python3 z", "x=$(echo \"${x:-'}\"; python3 z)", "x=; echo \"${x:-$'}'}\"; python3 z",
    # bash and dash: a bare { does not nest; zsh: it does
    "echo ${x:-{}; python3 y", "echo ${x:-{} #}; python3 z",
    # in $(...): ${...} is skipped whole (bash 5, zsh, dash)
    "x=$(echo \"${x:-\")'\"}\"; python3 z; echo \"'\")", "x=$(echo ${x:-) #}; python3 z)",
])
expect("pass", ["echo \"${VAR:-'none'}\" pip", "echo ${x:-{}} pip", "echo \"${DATA:-{}}\" | jq . && echo pip",
                "x=\"$(echo \"${HOME:-'none'}\")\" && echo pip", "x=$(echo \"${x:-\")\"}\") && echo pip",
                "x=$(echo ${x:-$(pwd)} ${x#*/}) && echo pip"])

print("heredoc delimiters over 256 characters")
for q in ("", "'"):
    d = "D" * 300
    r = reason(f"cat <<{q}{d}{q}\nx\n{d}\npython3 x")
    check(f"300-character {'quoted ' if q else ''}delimiter is too complex", "too large" in r, r)
d = "D" * 256
expect("pass", [f"cat <<{d}\npip install x\n{d}\nls"])
expect("deny", [f"cat <<{d}\nx\n{d}\npython3 x"])

print("code piped into a shell")
expect("deny", [
    "cat <<EOF |\npython3 z\nEOF\nbash", "cat <<EOF |\n\npip list\nEOF\n\nbash", "cat <<EOF | # c\npip list\nEOF\nbash",
    "(cat <<EOF) | bash\npython3 z\nEOF", "(cat <<EOF) 2>/dev/null | bash\npip list\nEOF", "{ cat <<EOF; } | bash\npython3 z\nEOF",
    "(cat <<EOF; echo) | sh\npip list\nEOF", "cat <<EOF | (bash)\npython3 z\nEOF", "cat <<EOF | { bash; }\npython3 z\nEOF",
    "cat <<EOF | (cd /tmp && bash)\npip list\nEOF",
    "printf 'pip install x\\n' | sh", "echo 'python3 z' | bash", "printf '%s\\n' 'pip list' | sh", "echo -e 'ls\\npip list' | sh",
    "(echo pip list) | bash", "echo pip list | (bash)", "{ echo pip; } | sh", "echo pip | sudo sh -s",
])
expect("pass", [
    "cat <<EOF |\npip install x\nEOF\ngrep pip", "(cat <<EOF; bash x.sh) | grep x\npip\nEOF", "{ cat <<EOF; } > f\npip\nEOF\nbash x.sh",
    "(cat <<EOF)\npip\nEOF\necho | bash", "cat <<EOF &&\npip list\nEOF\nbash x", "echo pip | grep pip",
    "echo 'pip install x' | bash script.sh", "echo pip; bash x.sh", "(echo pip) | grep x", "printf -v x 'pip list' | sh",
])

print("brace expansion: every word")
expect("deny", ["{,python3} x", "{,python3,} x", "{a,python3} x"])
expect("pass", ["echo {,python3} x", "ls {,a}"])

print("$[ without its ], for dash; extglob in case patterns")
expect("deny", ["echo $[ ; python3 x", "echo $(echo $[) ; python3 x", "{ echo $[; } | cat;echo `echo ]]`&`python3 z`",
                "case x in @($(python3 z))) ;; esac", "case x in @(a|`pip list`)) ;; esac"])
expect("pass", ["echo $[ 1 + 2 ] pip", "case $t in @(pip|uv)) echo y;; esac", "case $t in @('$(pip)')) echo y;; esac"])

print("$((...)) that is $( (...) ), and arrays with operators")
expect("deny", ["cat <<E\n$((cat <<F) | sh\npython3 z\nF\n)\nE", "a=(<(python3 z))", "a=(x <(pip list) y); echo ok",
                "x=(a && b\npython3 z\n)"])
expect("pass", ["cat <<E\n$(( 1 + 2 ))\nE", "a=('a;b' \"c|d\" e\\;f); echo \"${a[@]}\" pip"])

print("{ and }: only whole words in command position open or close a group")
expect("deny", [
    # "}#" is a plain word in every shell: the "#" does not start a comment
    "echo }#; python3 z", "printf '%s\\n' a }#b; pip list", "echo a; }#; python3 z", "for i in }#; do python3 z; done",
    "case }# in *) python3 z;; esac", "echo \"$(echo }#; python3 z)\"", "cat <<E\n$(echo }#; python3 z)\nE",
    # a group after time, coproc, function NAME
    "time { python3 z; }", "time -p { python3 z; }", "function f { python3 z; }; f", "coproc N { python3 z; }",
    "! { python3 z; }", "x || { pip list; }", "{ cat <<E; } | bash\npython3 z\nE",
    # zsh: a "}" before a blank or an operator closes an open group anywhere
    "{ cat <<E; echo }|bash\npython3 z\nE", "{ cat <<E; echo a}|bash\npython3 z\nE", "{ cat <<E; >/dev/null }|bash\npython3 z\nE",
])
expect("pass", ["echo }", "echo }#x", "echo { a }", "echo } python3 z", "echo { python3 z", "echo {a,b} } pip",
                "find . -exec echo {} \\;", "{fd}>log echo python", "{ echo a; } >/dev/null; echo pip"])

print("heredoc delimiters bash and zsh read as other words")
for d in ("$'EOF'", "$'E\\x4fF'", '$"EOF"', "$(echo)", "$((1))", "a$(echo)b", "${x:-a b}", "$[1 ]", "`a b`",
          "a=(b)", "@(a)"):
    r = reason(f"cat <<{d}\nhi\nEOF\n{d}\npython3 z")
    check(f"<<{d}: too complex", "too large" in r, r)
check("$'...' delimiter inside $(...): too complex", "too large" in reason("x=$(cat <<$'EOF'\nhi\nEOF\n); python3 z"))
expect("pass", ["cat <<$EOF\nhi\n$EOF\nls", "cat <<'$(x)'\npip\n$(x)\nls", "cat <<E$x\npip\nE$x\nls"])

print("shells reading /dev/stdin or <(...), and source of them")
expect("deny", [
    "bash /dev/stdin <<'E'\npython3 z\nE", "bash /dev/fd/0 <<'E'\npython3 z\nE", "cat <<'E' | bash /dev/stdin\npython3 z\nE",
    "echo \"python3 z\" | sh /dev/stdin", "sh /proc/self/fd/0 <<< 'pip list'", "bash -- /dev/stdin <<'E'\npip list\nE",
    ". /dev/stdin <<'E'\npython3 z\nE", "source /dev/stdin <<'E'\npython3 z\nE",
    "bash <(echo 'python3 z')", "bash <(cat <<'E'\npython3 z\nE\n)", ". <(printf 'pip list\\n')", "source <(echo 'python3 z')",
    "bash < <(echo python3 z)", "sh -s < <(printf 'pip list')", "bash <(echo ls; (echo 'pip list'))",
    "cat <<< 'python3 z' | bash", "(cat <<< 'pip list') | sh",
])
expect("pass", [
    "bash /dev/stdin.sh <<'E'\npython3 z\nE", "bash script.sh <(echo 'python3 z')", "diff <(echo python3 z) f",
    "source <(pixi shell-hook)", "cat <<< 'python3 z' | grep x", "bash \"$(echo x.sh)\" <<'E'\npython3 z\nE",
    "cat <(echo pip) | grep p", "source ~/.bashrc", "bash < /dev/null", "bash < <(echo ls)",
])

print("literal code handed to a shell through $(...)")
expect("deny", ["bash -c \"$(cat <<'E'\npython3 z\nE\n)\"", "eval \"$(cat <<'E'\npython3 z\nE\n)\"",
                "bash <<<\"$(cat <<'E'\npython3 z\nE\n)\"", "sh -c \"$(echo 'pip list')\"", "su -c \"$(echo pip list)\" me"])
expect("pass", ["eval \"$(pixi shell-hook)\"", "bash -c \"$(cat script.sh)\"", "eval \"$(ssh-agent -s)\"",
                "git commit -m \"$(cat <<'E'\npython3 z\nE\n)\"", "bash -c \"$(printf %s ls)\"", "echo \"$(echo pip)\" | grep p"])

print("arrays: a ( or an operator in them, after which bash goes on at the next line")
expect("deny", [
    "a=( (a) \"\npython3 z\n\"", "fia=( cat[[ x =~ (a) ]] \\'\"\\'\npython3 z cat", "a=( ; ) \"\npython3 z\n\"",
    "a=( ; x ) <<E\npython3 z\nE", "a=( ; x ) echo \\\npython3 z", "a=( (a) x\npython3 z",
])
expect("pass", ["a=(1 2 \"(x)\")", "pkgs=(\n  pip # (the installer); x\n  uv\n); echo \"${pkgs[@]}\"", "a=(<(echo a; b)\n)\necho pip",
                "a=(x)\necho pip"])

print("zsh readings")
expect("deny", ["{python3 z}", "{python3 z;}", "=python3 z", "repeat 1 python3 z", "if [[ -n x ]] python3 z",
                ">! out python3 z", ">>! out python3 z", "2>! out python3 z", "&>! out python3 z", "- python3 z",
                "a=(1)python3 z",
                # bash's reading is kept: ">!" is ">" to a file "!", and a=(1)z is one word
                ">! python3 z", "a=(1)z python3"])
expect("pass", ["echo =python3", ">!out echo python3", "{a,b}c d", "repeat_it 1 python3", "echo repeat 1 python3",
                "[[ -n x ]] && echo pip"])

print("a first word that can expand to nothing")
expect("deny", ["$SUDO pip install requests", "$(true) python3 z", "`true` python3 z", "\"$@\" python3 z",
                "${x:+} python3 z", "\"${args[@]}\" pip list", "$x $y python3 z", "$x >/dev/null python3 z",
                "echo \"python3 z\" | ${x:+}sh", "$SHELL -c 'python3 z'", "echo 'python3 z' | $SHELL", "$SUDO apt install python3"])
expect("pass", ["$EDITOR notes.md", "$(command -v pixi) run python x", "\"$x\" python3", "${PIXI:-pixi} run python x",
                "$x", "\"$@\"", "$HOME/.pixi/bin/pixi run python x", "\"$1\" pip", "$SUDO make install"])

print("brace expansion: several groups, nested, ranges")
expect("deny", ["{python3,}{,python3}", "{{python3,x},y} a", "{,} python3 z", "{p..p}ython3 z", "python{3..3} z",
                "{python3,{x}} z", "{python3,x}{,} z", "{x,{y,{z,pip}}} list"])
expect("pass", ["echo {python3,}{,python3}", "ls {1..3}", "{a..c} x", "{a,b}{c,d} x", "echo {1..9999}"])
check("brace expansion into over 256 words is too complex", "too large" in reason("{1..1000} x"))

print("brace expansion of words with quotes (bash expands braces before removing quotes)")
expect("deny", ["{'python3',-V}", "{\"python3\",-V}", "{\\python3,-V}", "{py\\thon3,-V}", "{'py'thon3,}",
                "{'pip',} -V", "env {'python3',-V}", "p{'y',}thon3 -V"])
expect("pass", ["'{python3,x}'", "\"{\"python3,x}", "echo {'python3',x}", "\"{python3,}\" x"])

print("a program word that is ${NAME:-WORD}: WORD may be the program")
expect("deny", ["${PYTHON:-python3} -c 1", "\"${PYTHON:-python3}\" -c 1", "${PIP:-pip} install x", "${x-python3} -V",
                "${x:=python3} -V", "\"${x:-pip}\" -V", "${x:-python3}", "${PYTHON:-python3} -m pytest",
                "${x:+python3} -V", "${SUDO:-sudo} pip install x", "${x:+sudo} python3 z", "${a:-${b:-python3}} x",
                "${SH:-bash} -c 'python3 z'", "echo 'pip list' | ${SH:-sh}", "env ${PY:-python3} x",
                "${PY:-/usr/bin/python3} x", "${PY:-'python3'} x", "${SH:-sh} <<'E'\npip list\nE",
                "find . -exec ${PY:-python3} {} \\;"])
expect("pass", ["${EDITOR:-vim} f.py", "\"${EDITOR:-vim}\" notes.py", "'${x:-pip}' -V", "${CC:-cc} -o x x.c",
                "\"${PIXI:-pixi}\" run python x", "echo ${PYTHON:-python3}", "${x:-} ls", "${x:?python3} ls"])

print("a program word with ${NAME:-WORD} in it: also read with each WORD, other expansions dropped")
expect("deny", ["${VIRTUAL_ENV:-.venv}/bin/uvicorn app:app", "${PYTHON:-python}${PYVER} -V", "${PY:-python}3 -V",
                "\"${VIRTUAL_ENV:-.venv}/bin/uvicorn\" app", "\"${PY:-python}\"3 -V", "${PY:-python}${V:-3} -V",
                "${VENV:-$HOME/.venv}/bin/pip install x", "\"${VENV_DIR:-$HOME/.venv}/bin/python\" x", "${PY-python}3 -V",
                "${PY=python}3 -V", "${PY:=python}3 -V", "${PY:+python}3 -V", "${PY+python}3 -V", "${a:-${b:-python}}3 -V",
                "${1:-python}3 -V", "${arr[0]:-python}3 -V", "${PY:-'python'}3 -V", "${A:-x}python3 z",
                "env ${PY:-python}3 -V", "${RUN:-env}${X} PYTHONPATH=. ${PY:-python}3 x"])
expect("pass", ["${EDITOR:-vi} file", "${CC:-cc}${SUFFIX} -o x x.c", "${TOOLS:-./tools}/build.sh", "${NODE:-node}${V} app.js",
                "echo ${PY:-python}3", "'${PY:-python}'3 -V", "${PIXI:-pixi}${X} run python x", "${x:-a}}python3 z",
                "${PY:-py thon}3 -V", "${x:-", "${x:?python}3 -V", "${x:-}${y} ls", "${GIT:-git}${X} commit -m 'pip'"])

print("expansions dropped from a program word")
expect("deny", ["${a}${b}python3 z", "$a$1$@python3 z", "$(true)``python3 z", "${x:-}python3 z", "${x:-a}$y/python3 z"])
expect("pass", ["${a}${b}ls python3", "$(true)``ls python3", "${x:-a}}python3 z"])

print("wrappers: setsid, flock, strace, xvfb-run, taskset, chrt, lockf, script, sandbox-exec, direnv, mise, asdf")
expect("deny", [
    "setsid python3 app.py", "setsid -f python3 app.py", "flock /tmp/l python3 x", "flock -w 5 /tmp/l pip list",
    "flock -n /tmp/l -c 'python3 x'", "strace -f python3 x", "strace -o log -e trace=open python3 x", "ltrace python3 x",
    "xvfb-run python3 x", "xvfb-run -a -s '-screen 0 1024x768x24' pytest", "xvfb-run --server-args '-ac' python3 x",
    "taskset -c 0 python3 x", "taskset 0x1 python3 x", "chrt -f 10 python3 x", "lockf -k f python3 -V",
    "lockf -t 5 f pip list", "script -q /dev/null python3 -V", "script -q -c 'python3 x' /dev/null",
    "script --command='pip list' log", "sandbox-exec -p '(version 1)(allow default)' python3 -V",
    "sandbox-exec -f prof.sb pip list", "direnv exec . python3 x", "mise exec -- python3 x",
    "mise x python@3.12 -- python x", "mise exec -c 'pip list'", "asdf exec python3 x",
])
expect("pass", ["taskset -p 1234", "taskset -pc 0 1234", "chrt -p 1234", "flock 9", "script log.txt", "direnv allow",
                "mise use python@3.12", "mise exec node -- node app.js", "setsid -f make", "strace -c ls",
                "flock /tmp/l -c 'pixi run python x'", "xvfb-run pixi run pytest"])

print("wrappers: the gateway's gate with NAME -- CMD, xcrun")
expect("deny", [
    "gate with openai -- python train.py", "gate with hf -- pip install x", "gate with openai -- uv run x.py",
    "sudo -n gate with openai -- python x", "gate with openai -- sh -c 'python x'", "gate with openai -- env A=1 pytest -q",
    "cd a && gate with openai -- python3 x", "xcrun python3 x.py", "xcrun -sdk macosx python3 x.py", "xcrun --sdk macosx pip3 list",
    "xcrun -toolchain t -r python3 -V", "xcrun -log -verbose python3 x", "xcrun -no-cache pytest",
])
expect("pass", [
    # the gateway's README example; gate's other commands; usage errors (gate runs nothing)
    "gate with openai -- pixi run python train.py", "gate with openai -- curl -s https://example.com", "gate status",
    "gate why python", "gate with -- python x", "gate with a b -- python x", "gate with openai python x",
    "echo gate with openai -- python x", "gate with openai --",
    # xcrun: -f only prints where a tool is
    "xcrun -f python3", "xcrun --find python3", "xcrun -find pip3", "xcrun --show-sdk-path", "xcrun -show-sdk-platform-path",
    "xcrun clang -v", "xcrun simctl list", "xcrun -sdk iphoneos clang x.c",
])

print("screen, tmux, trap")
expect("deny", [
    "screen -dmS s python3 app.py", "screen -dm bash -c 'python3 app.py'", "screen -S s -X stuff 'python3 app.py\\n'",
    "tmux new -d 'python3 app.py'", "tmux new -d python3 app.py", "tmux new-session -d -s srv 'pip install x'",
    "tmux split-window -h 'pytest -f'", "tmux send-keys -t srv 'pip install x' Enter", "tmux -L x new -d 'python3 app.py'",
    "tmux if-shell 'python3 -c 1' 'display ok'", "tmux new -d \\; send-keys 'python3 x' Enter", "tmux -c 'pip list'",
    "tmux run-shell -b 'python3 x'", "trap 'python3 -V' EXIT", "trap -- 'pip list' INT", "trap \"$(echo python3)\" EXIT",
])
expect("pass", ["screen -ls", "screen -r s", "screen -S s -X quit", "tmux ls", "tmux attach -t s",
                "tmux new -d 'pixi run python app.py'", "tmux new -d pixi run python app.py", "tmux send-keys -t s 'ls' Enter",
                "trap 'rm -f x' EXIT", "trap -p", "trap - EXIT", "echo trap python3"])

print("alias NAME=VALUE: VALUE is code (it runs where NAME is typed as a command)")
expect("deny", ["alias p=python", "alias p=python; p x.py", "alias p='pip install'", "alias -- p=python3", "alias -s py=python",
                "alias ll='ls -l' p='uv run x'", "alias t=\"python3 -V\""])
expect("pass", ["alias", "alias ll", "alias ll='ls -l'", "alias p='pixi run python'", "alias -p", "unalias p", "echo alias p=python"])

print("xargs running a shell: its code may come from stdin")
expect("deny", ["echo 'pip -V' | xargs -0 sh -c", "echo python3 | xargs sh -c", "printf 'python3 -V' | xargs -I{} sh -c '{}'",
                "echo 'pip list' | xargs -J % sh -c 'cd /tmp && %'",
                "echo 'pip list' | xargs -I % bash -c 'echo hi; %'", "echo 'pip list' | xargs --replace bash -c '{}'",
                "echo 'pip list' | xargs -i bash -c '{}'", "xargs sh -c <<< 'python3 z'"])
expect("pass", ["echo a | xargs sh -c 'echo $1' _", "echo python3 | xargs -I{} sh -c 'echo {}' | grep x", "echo pip | xargs echo",
                "printf '%s\\n' a.py b.py | xargs -I{} sh -c 'wc -l {}'", "echo pip | xargs -I% sh -c 'echo %; ls'"])

print("output into a >(...) that is a shell reading stdin")
expect("deny", ["echo 'python3 -V' | tee >(bash) >/dev/null", "echo 'pip list' > >(bash)", "echo 'pip list' > >(sh -s)",
                "cat <<E > >(bash)\npython3 z\nE", "echo x | tee >(python3 y)"])
expect("pass", ["echo 'pip list' | tee >(grep x)", "echo 'pip list' | tee >(bash x.sh)", "echo 'pip list' | tee >(bash -c 'cat')",
                "echo pip > >(cat)", "ls | tee >(wc -l)"])

print("extglob and zsh glob groups: patterns, not subshells")
expect("deny", [
    # bash with extglob ends @( ... ) at a ")" in ${x:-, and runs the next line
    "echo @(${x:-)\npython3 -V", "ls @(a|${x:-)b\npython3 -V", "!(${x:-)\npython3 -V", "echo !(${x:-)\npython3 -V",
    "case x in @(${x:-)) echo;; esac\npython3 -V", "echo @(#); python3 -V",
    # a "#" in a pattern is not a comment
    "case x in @(#)) echo;; esac; python3 -V", "case x in @(a|#b)) echo;; esac; python3 -V",
    # "!" and a subshell in bash without extglob and in dash
    "!(python3 -V)",
    "ls (src|tests)/*.py; python3 -V", "echo @($(python3 x))", "ls !(`pip list`)", "f () { python3 x; }",
    "f() { python3 x; }", "if true; then f () { python3; }; fi", "function f () { python3 x; }",
    # zsh: for NAME (WORDS) CMD, =(CMD), glob qualifiers that run code
    "for i (1 2) python3 x", "diff =(python3 x) f", "echo *(e:'python3 -V':)", "ls *(+python3)", "ls *(.e['pip list'])",
])
expect("pass", ["ls !(*.py)", "rm -f (foo|bar).py", "ls (src|tests)/*.py", "ls (*.py|*.txt)", "echo !(python3 -V)",
                "x=1 (python3 -V)", "ls !(test|python3)", "rm !(seed)", "ls *(.)", "ls *(om[1])", "ls @('$(pip)')",
                "echo a () { python3; }", "ls -d !(python3|pip)"])

print("a glob group with no \")\": the rest is read as commands, not taken into the word")
expect("deny", ["echo @(a\npython3 -V", "ls !(x\npip list", "echo (a|b\npython3 -V", "echo @(a; python3 -V",
                "ls *.py(x && pip list", "bash -c 'echo @(a\npython3 -V'", "echo 'echo @(a; pip list' | sh"])
expect("pass", ["echo @(a\nls", "echo @(a; ls", "ls !(*.py) python3", "echo @(a|b) pip", "echo 'echo @(a; pip list'"])

print("dash: [[ is a command, so a | next to a blank is a pipe")
expect("deny", ["[[ x | python3 -V ]]", "[[ a =~ x |python3 ]]", "[[ a =~ x| python3 ]]", "[[ -n x ]] | python3 z"])
expect("pass", ["[[ $x =~ ^(pip|uv)$ ]] && echo y", "[[ $x =~ (a | pip) ]] && echo y", "[[ $x =~ a|pip ]] && echo y",
                "[[ -n x || $y =~ a|pip ]] && echo y", "[[ -n x || $y =~ (a|pip) ]] && echo y"])

print("zsh: { ... } always { ... }")
expect("deny", ["{ true } always { python3 -V }", "{ true; } always { python3 -V; }"])
expect("pass", ["{ true; } always { echo pip; }"])

print("escapes: $'...', printf and echo -e text into a shell")
expect("deny", ["$'\\x70ython3' -V", "$'\\160ip' -V", "$'\\u0070ip' -V", "$'p\\x69p' list", "printf 'pyth\\157n3 -V' | sh",
                "echo -e 'p\\x79thon3 -V' | bash", "echo -e 'p\\0171thon3 -V' | bash", "printf 'p\\ython3 -V' | sh",
                "printf 'ls\\tx\\npip list' | sh"])
expect("pass", ["echo $'\\x70ip'", "printf 'pyth\\157n3 -V'", "printf 'a\\tb\\n' | sh", "echo $'it\\'s' python",
                "printf '\\x70ixi run python x' | sh"])

print("Claude Code's shell is bash 3.2: $(...) is also read as it ends it (an apostrophe pair in a heredoc, a case pattern)")
FAKE_SHELLS = {}
for name, ver in (("bash32", "3.2.57(1)-release (arm64-apple-darwin25)"), ("bash52", "5.2.37(1)-release"),
                  ("bash51", "5.1.16(1)-release"), ("bash53", "5.3.15(1)-release (aarch64-apple-darwin25.5.0)"),
                  ("bash510", "5.10.0(1)-release"), ("bash60", "6.0.0(1)-release")):
    d = TMP / name
    d.mkdir()
    (d / "bash").write_text(f'#!/bin/sh\necho "GNU bash, version {ver}"\n')
    (d / "bash").chmod(0o755)
    FAKE_SHELLS[name] = str(d / "bash")
B32 = {"SHELL": FAKE_SHELLS["bash32"]}
b32_deny = [
    # Claude's usual commit heredoc: a ")" after an apostrophe pair ends the $( early, backticks after it run
    "git commit -m \"$(cat <<'EOF'\nDon't use pip (it's slow): run `pip install -e .` instead\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nDon't retry (it's idempotent now); `python -m app` handles it\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\nFixed it :) now \"pixi\" works `pip list`\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\n# Don't (it's) fine in a comment\nDon't (it's) `pip list`\nEOF\n)\"",
    # unquoted $(...): the rest of the text is read as commands
    "x=$(cat <<'EOF'\nDon't use pip (it's slow): run `pip install -e .` instead\nEOF\n)",
    # a case pattern's ")" ends it too, and so does a ")" with no "(" in it
    "git commit -m \"$(cat <<'EOF'\nUse case $x in pip) `python -V` ;; esac\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\nSteps: 1) replace `pip install -e .` with pixi\nEOF\n)\"",
]
b32_pass = [
    # the same $( end: no ")" outside the quotes bash 3.2 sees, or parens that pair up
    "git commit -m \"$(cat <<'EOF'\nDon't use pip, it's slow\n- run `pip install -e .` instead\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nUse pixi (not pip) for `pip install`\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nIt's (mostly) done, they're fine: `pip list`\nEOF\n)\"",
    # an odd apostrophe: a syntax error in bash 3.2, nothing runs
    "git commit -m \"$(cat <<'EOF'\nDon't run `pip install` here\nEOF\n)\"",
    # ends early, but nothing after it runs
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\nFixed it :) now \"pixi\" works\npytest passes\nEOF\n)\"",
    # a comment hides the quotes and the ")" (when bash 3.2 runs it)
    "git commit -m \"$(cat <<'EOF'\n# Don't (it's) `pip list`\nEOF\n)\"", "x=$(cat <<'EOF'\n# Don't (it's) `pip list`\nEOF\n)",
    "echo $(case $x in a) echo 1;; esac)", "echo \"$(case $t in pip) echo 1;; esac)\" pip",
    # $'...' has escapes there too (the ${x} makes the guard look at the bash 3.2 reading)
    "echo $(printf $'it\\'s') pip", "echo \"$(printf $'it\\'s') pip\"", "echo $(printf $'it\\'s' ${x}) ';) ; pip list'",
]
expect("deny", b32_deny, "bash 3.2: ", **B32)
expect("pass", b32_pass, "bash 3.2: ", **B32)
for label, kw in (("zsh", {}), ("bash 5", {"SHELL": FAKE_SHELLS["bash52"]})):
    expect("pass", b32_deny, f"{label}: ", **kw)
check("bash 3.2: CLAUDE_CODE_SHELL counts over SHELL",
      decision(b32_deny[0], SHELL=FAKE_SHELLS["bash52"], CLAUDE_CODE_SHELL=FAKE_SHELLS["bash32"]) == "deny")
check("bash 3.2: a SHELL that is not bash is not asked its version", decision(b32_deny[0], SHELL=str(FAKE_PIXI)) == "pass")
r = reason(b32_deny[0], **B32)
check("bash 3.2: the reason explains it and offers git commit -F", "bash 3.2" in r and "git commit -F" in r, r)
check("bash 3.2: no such note for other hits", "bash 3.2" not in reason("pip list", **B32))
r = reason(b32_deny[1], **B32)
check("bash 3.2: the note also comes with a tool looked up by name", "`python`" in r and "bash 3.2" in r, r)
check("bash 3.2: PIXI_GUARD=ask still asks", decision("PIXI_GUARD=ask " + b32_deny[0], **B32) == "ask")
check("bash 3.2: deep but checkable nesting is still scanned (bash 3.2 reads plain $(...) the same)",
      hit("echo " + "$(echo " * 200 + "$(python x)" + ")" * 200, **B32) == "python")
# The real shells agree (when here): with pip, python and pytest as functions that say RAN, and
# git and gh doing nothing, macOS's /bin/bash 3.2 runs one in exactly the commands refused above.
STUBS = "pip() { echo RAN >&2; }; python() { echo RAN >&2; }; pytest() { echo RAN >&2; }; git() { :; }; gh() { :; }\n"


def bash_version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except OSError:
        return (0, 0)
    m = re.search(r"version (\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def bash_major(path):
    return bash_version(path)[0]


def runs_tool(bash, cmd):
    # (a generous limit: a busy machine once took over 10 s on a heredoc in ${ ...; })
    r = subprocess.run([bash, "-c", STUBS + cmd], capture_output=True, text=True, timeout=60, cwd=str(PLAIN),
                       stdin=subprocess.DEVNULL)
    return "RAN" in r.stderr


real = {}
for p in ("/bin/bash", shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/opt/local/bin/bash"):
    if p and os.path.exists(p):
        real.setdefault(3 if bash_major(p) == 3 else 5 if bash_major(p) >= 4 else 0, p)
if 3 in real:
    for cmd in b32_deny:
        check(f"real bash 3.2 runs the tool in {cmd!r}", runs_tool(real[3], cmd))
    for cmd in b32_pass:
        check(f"real bash 3.2 runs nothing in {cmd!r}", not runs_tool(real[3], cmd))
if 5 in real:
    for cmd in b32_deny + b32_pass:
        check(f"real bash 5 runs nothing in {cmd!r}", not runs_tool(real[5], cmd))

print("Claude Code's shell is bash 5.2 or later: a heredoc in $(...) also ends at a line that starts with its delimiter and has a \")\" after it")
B52 = {"SHELL": FAKE_SHELLS["bash53"]}
b52_deny = [
    # the rest of the line after "EOF" is code, and its ")" ends the $(...): the backticks after it run
    "git commit -m \"$(cat <<'EOF'\nFix EOFError on empty input\n\nEOFError was raised on empty stdin :)\n"
    "Repro: `python -c 'input()' < /dev/null`\n\nCo-Authored-By: x <x@example.com>\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\nEOFError is raised 1) run `pytest` to see it\nEOF\n)\"",
    "git commit -m \"$(cat <<-'EOF'\n\tEOFError :) `pip list`\nEOF\n)\"",
    # code on that line runs, and so do the lines after it while the $(...) is open
    "git commit -m \"$(cat <<'EOF'\nEOF2 <<EOF `$(python -V)`\nmore\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nEOF (fine)\n`python -V`\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nEOF `python -V` :)\nEOF\n)\"", "git commit -m \"$(cat <<'EOF'\nEOFError: `pip list` :)\nmore\nEOF\n)\"",
    "x=$(cat <<'EOF'\nEOF $(python -V) here\nEOF\n)",
    # unquoted $(...): the lines after the ")" are commands
    "x=$(cat <<'EOF'\nEOF handling fixed :)\npytest -q\nEOF\n)",
]
b52_pass = [
    # ends early, but nothing after it runs
    "git commit -m \"$(cat <<'EOF'\nEOF handling fixed :)\npytest passes\nEOF\n)\"",
    # no line that starts with EOF and has a ")"
    "git commit -m \"$(cat <<'EOF'\nHandle EOFError (empty stdin)\n\nTests: `pytest` passes\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\nEOFError on empty input\nRepro: `python -c 'input()'` (see #1)\nEOF\n)\"",
    "x=$(cat <<'EOF'\nEOFx\npython -V )\nEOF\n)", "git commit -m \"$(cat <<'EOF'\n EOFError :) `pip list`\nEOF\n)\"",
    "git commit -m \"$(cat <<'EOF'\n\tEOFError :) `pip list`\nEOF\n)\"",
    # not in $(...): backticks and the top level read heredocs as usual
    "x=`cat <<'EOF'\nEOFx :) ; python -V\nEOF\n`", "cat <<'EOF'\nEOFx :) `python -V`\nEOF",
]
expect("deny", b52_deny, "bash 5.2+: ", **B52)
# <(...) and >(...) too (not run against the real shell below: its process substitution then never ends)
expect("deny", ["echo hi > >(cat <<'EOF'\nEOFx :)\npython -V\nEOF\n)", "wc -l <(cat <<'EOF'\nEOFx :)\npython -V\nEOF\n)",
                "wc -l <(cat <<'EOF'\nEOF `python -V` :)\nEOF\n)"],
       "bash 5.2+: ", **B52)
expect("pass", b52_pass, "bash 5.2+: ", **B52)
for label, kw in (("zsh", {}), ("bash 5.1", {"SHELL": FAKE_SHELLS["bash51"]})):
    expect("pass", b52_deny, f"{label}: ", **kw)
for label in ("bash52", "bash510", "bash60"):
    check(f"bash 5.2+: {label} reads it so too", decision(b52_deny[0], SHELL=FAKE_SHELLS[label]) == "deny")
check("bash 5.2+: CLAUDE_CODE_SHELL counts over SHELL",
      decision(b52_deny[0], SHELL=FAKE_SHELLS["bash51"], CLAUDE_CODE_SHELL=FAKE_SHELLS["bash53"]) == "deny")
for cmd in (b52_deny[0], b52_deny[2]):  # a tool looked up by name, and pip
    r = reason(cmd, **B52)
    check(f"bash 5.2+: the reason explains it and offers git commit -F ({cmd[:40]!r})", "bash 5.2" in r and "git commit -F" in r, r)
check("bash 5.2+: no such note for other hits", "bash 5.2" not in reason("pip list", **B52))
check("bash 5.2+: PIXI_GUARD=ask still asks", decision("PIXI_GUARD=ask " + b52_deny[0], **B52) == "ask")
# A tool from pixi global is not refused in that reading either.
(TMP / "b52home" / ".pixi" / "bin").mkdir(parents=True)
(TMP / "b52home" / ".pixi" / "bin" / "pytest").write_text("#!/x/python3\n")
(TMP / "b52home" / ".pixi" / "bin" / "pytest").chmod(0o755)
expect("pass", [b52_deny[1]], "bash 5.2+, pytest from pixi global: ", HOME=str(TMP / "b52home"),
       PATH=f"{TMP / 'b52home' / '.pixi' / 'bin'}{os.pathsep}{BASE_PATH}", **B52)
real52 = next((p for p in ("/bin/bash", shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/opt/local/bin/bash")
               if p and os.path.exists(p) and bash_version(p) >= (5, 2)), None)
real51 = next((p for p in ("/bin/bash", shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/opt/local/bin/bash")
               if p and os.path.exists(p) and (4, 0) <= bash_version(p) < (5, 2)), None)
if real52:
    for cmd in b52_deny:
        check(f"real bash 5.2+ runs the tool in {cmd!r}", runs_tool(real52, cmd))
    for cmd in b52_pass:
        check(f"real bash 5.2+ runs nothing in {cmd!r}", not runs_tool(real52, cmd))
if real51:
    for cmd in b52_deny + b52_pass:
        check(f"real bash 5.1 runs nothing in {cmd!r}", not runs_tool(real51, cmd))

print("checkpoint's job_start: read as /bin/sh reads it (checkpoint runs the job) and as the gateway's bash (it runs the job "
      "while it serves the session), not as Claude Code's shell")
JSH32, JSH52 = {"PIXI_GUARD_JOB_SH": FAKE_SHELLS["bash32"]}, {"PIXI_GUARD_JOB_SH": FAKE_SHELLS["bash53"]}
for cmd in b32_deny[:4]:
    got = jdecision(command=cmd, **JSH32)
    check(f"deny  job, /bin/sh bash 3.2, Claude Code's shell zsh: {cmd[:50]!r}", got == "deny", got)
    got = jdecision(command=cmd, **JSH, **B32)
    check(f"pass  job, /bin/sh not bash, Claude Code's shell bash 3.2: {cmd[:50]!r}", got == "pass", got)
for cmd in b32_pass[:2]:
    got = jdecision(command=cmd, **JSH32)
    check(f"pass  job, /bin/sh bash 3.2: {cmd[:50]!r}", got == "pass", got)
r = jreason(command=b32_deny[0], **JSH32)
check("job, /bin/sh bash 3.2: the note says the job runs with /bin/sh", "checkpoint runs the job with /bin/sh, which here is bash 3.2" in r
      and "Claude Code's shell" not in r, r)
check("job argv, /bin/sh bash 3.2: sh -c code read so too",
      jdecision(argv=["sh", "-c", b32_deny[0]], **JSH32) == "deny" and jdecision(argv=["sh", "-c", b32_deny[0]], **JSH) == "pass")
for cmd in b52_deny[:3]:
    got = jdecision(command=cmd, **JSH52)
    check(f"deny  job, /bin/sh bash 5.2+, Claude Code's shell zsh: {cmd[:50]!r}", got == "deny", got)
    # the gateway's bash (5.2 and later) reads it so whatever /bin/sh is
    for label, kw in (("not bash", JSH), ("bash 3.2", JSH32)):
        got = jdecision(command=cmd, **kw)
        check(f"deny  job, /bin/sh {label}, read as the gateway's bash: {cmd[:50]!r}", got == "deny", got)
for cmd in b52_pass:
    got = jdecision(command=cmd, **JSH)
    check(f"pass  job, /bin/sh not bash, the gateway's bash: {cmd[:50]!r}", got == "pass", got)
r = jreason(command=b52_deny[0], **JSH52)
check("job, /bin/sh bash 5.2+: the note says the job runs with /bin/sh", "checkpoint runs the job with /bin/sh, which here is bash 5.2" in r, r)
r = jreason(command=b52_deny[0], **JSH)
check("job, /bin/sh not bash: the note says the gateway runs checkpoint's jobs with its own bash",
      "while the gateway serves the session, it runs checkpoint's jobs with its own bash, which is bash 5.2" in r
      and "/bin/sh" not in r and "Claude Code's shell" not in r, r)
check("job, /bin/sh bash 3.2: PIXI_GUARD=ask still asks", jdecision(command="PIXI_GUARD=ask " + b32_deny[0], **JSH32) == "ask")
# Both at once: with /bin/sh bash 3.2, a ${ ...; } (code to the gateway's bash 5.3) also makes the text be read as bash
# 3.2 reads it (${...} as text): there the $( ends at the ")" inside it, and the rest is code.
job32_53 = "x=\"$(${ echo ) ; }\" ; pip install foo ; \")\""
check("deny  job, /bin/sh bash 3.2 and the gateway's bash: a ${ ...; } in a $(...) read as bash 3.2 too",
      jdecision(command=job32_53, **JSH32) == "deny")
r = jreason(command=job32_53, **JSH32)
check("job, /bin/sh bash 3.2 and a ${ ...; }: the note is /bin/sh's", "`pip`" in r and "/bin/sh, which here is bash 3.2" in r, r)
for label, kw in (("not bash", JSH), ("bash 5.3", JSH52)):
    got = jdecision(command=job32_53, **kw)
    check(f"pass  job, /bin/sh {label}: {job32_53!r}", got == "pass", got)
check(f"pass  gateway job: {job32_53!r}", verdict(*run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "mcp__plugin_gate_gw__job_start",
                                                          "tool_input": {"command": job32_53}})) == "pass")
if 3 in real:
    check(f"real bash 3.2 runs the tool in {job32_53!r}", runs_tool(real[3], job32_53))
if 5 in real:
    check(f"real bash 5 runs nothing in {job32_53!r}", not runs_tool(real[5], job32_53))
if shutil.which("dash"):  # (a bad substitution: dash stops there)
    check(f"real dash runs nothing in {job32_53!r}", not runs_tool(shutil.which("dash"), job32_53))
# The real /bin/sh (no override): bash 3.2 on macOS, dash on Debian.
sh_ver = bash_version("/bin/sh")
if sh_ver[0] == 3 or sh_ver == (0, 0):
    want = "deny" if sh_ver[0] == 3 else "pass"
    got = jdecision(command=b32_deny[0], PIXI_GUARD_JOB_SH=None)
    check(f"job, the real /bin/sh ({'bash 3' if want == 'deny' else 'not bash'}): {want}", got == want, got)
    if want == "deny" and 3 in real:
        check("the real /bin/sh runs the tool in that job", runs_tool("/bin/sh", b32_deny[0]))

print("the gateway's job_start: its command or argv, read as the gateway's own bash (5.2); a job on another host is not checked")
NOHOST = object()
ph = hooks["PreToolUse"][0]["hooks"][0]


def gwjob(command=None, argv=None, cwd=None, host=NOHOST, session_cwd=PLAIN, sid="s1", extra=None, **kw):
    ti = dict(extra or {})
    if command is not None:
        ti["command"] = command
    if argv is not None:
        ti["argv"] = argv
    if cwd is not None:
        ti["cwd"] = str(cwd)
    if host is not NOHOST:
        ti["host"] = host
    ti["on_pause"] = "continue"
    return run("pre", {"session_id": sid, "transcript_path": "/x", "cwd": str(session_cwd),
                       "hook_event_name": "PreToolUse", "tool_name": GWJOB, "tool_input": ti}, **kw)


def gwdecision(**kw):
    return verdict(*gwjob(**kw))


def gwreason(**kw):
    return why(gwjob(**kw)[0])


# The gateway's job runs `bash --noprofile --norc FILE`, FILE ending in the command, or in argv joined with shlex.quote.
for command, argv, want in JOB_PAIRS:
    got = gwdecision(command=command)
    check(f"{want:5} gateway job command {command!r}", got == want, got)
    got = gwdecision(argv=argv)
    check(f"{want:5} gateway job argv {argv!r}", got == want, got)
for argv, command in JOB_QUOTED:
    got = gwdecision(argv=argv)
    check(f"pass  gateway job argv {argv!r}: one word each", got == "pass", got)
for argv, want in ((["gate", "with", "openai", "--", "python", "train.py"], "deny"),
                   (["gate", "with", "openai", "--", "pixi", "run", "python", "train.py"], "pass"),
                   (["pixi", "run", "pip", "install", "torch"], "deny"), (["pixi", "run", "pip", "list"], "pass")):
    got = gwdecision(argv=argv)
    check(f"{want:5} gateway job argv {argv!r}", got == want, got)
GWID = "m_" + "abcdefghijklmnopqrstuvwxyz"[:20] + "234567"  # an id of this machine (or another) as the gateway names it
for label, host, want in (("no host given", NOHOST, "deny"), ("localhost", "localhost", "deny"), ('""', "", "deny"),
                          ("null", None, "deny"), ("an m_ id (this machine's, to the gateway, may be)", GWID, "deny"),
                          ("a host named in gateway.toml", "gpu1", "pass"), ("a host name that is no m_ id", "m_gpu", "pass"),
                          ("an m_ word too long for an id", GWID + "a", "pass"), ("an m_ word with capitals", "m_" + "A" * 26, "pass"),
                          ("LOCALHOST (not the gateway's word for this machine)", "LOCALHOST", "pass")):
    for kw in ({"command": "python train.py"}, {"argv": ["python", "train.py"]}):
        got = gwdecision(host=host, **kw)
        check(f"{want:5} gateway job on {label} ({list(kw)[0]})", got == want, got)
r = gwreason(command="python train.py", host=GWID)
check("gateway job on an m_ id: the reason says jobs on other hosts are not checked (name the host)",
      "another machine's id" in r and "gateway.toml" in r, r)
check("gateway job on localhost: no such note", "another machine's id" not in gwreason(command="python train.py", host="localhost"))
for cmd in b52_deny[:3]:
    got = gwdecision(command=cmd)
    check(f"deny  gateway job, its bash 5.2, Claude Code's shell zsh: {cmd[:50]!r}", got == "deny", got)
    got = gwdecision(command=cmd, host="gpu1")
    check(f"pass  gateway job on another host: {cmd[:50]!r}", got == "pass", got)
r = gwreason(command=b52_deny[0])
check("gateway job, bash 5.2: the note says the gateway runs it with its own bash",
      "the gateway runs the job with its own bash, which is bash 5.2" in r and "checkpoint" not in r and "Claude Code's shell" not in r, r)
for cmd in b32_deny[:2]:
    for label, kw in (("Claude Code's shell", B32), ("/bin/sh", JSH32)):
        got = gwdecision(command=cmd, **kw)
        check(f"pass  gateway job, {label} bash 3.2 (not the job's shell): {cmd[:40]!r}", got == "pass", got)
# With no cwd, the gateway's MCP server runs a local job in CLAUDE_PROJECT_DIR (else its own folder, the
# project's): not the session's folder, which a cd may have changed.
WS = {"workspace": "w_" + "a" * 26}
for label, kw, want in (
    ("a pixi #! line in the job's cwd", dict(argv=["./x.py"], cwd=pixi_x), "pass"),
    ("a python #! line in the job's cwd", dict(argv=["./x.py"], cwd=plain_x), "deny"),
    ("the job's cwd counts, not the session's", dict(command="./x.py", cwd=plain_x, session_cwd=pixi_x), "deny"),
    ("no cwd: CLAUDE_PROJECT_DIR's (python #! line there), not the session's",
     dict(argv=["./x.py"], session_cwd=pixi_x, CLAUDE_PROJECT_DIR=str(plain_x)), "deny"),
    ("no cwd: CLAUDE_PROJECT_DIR's (pixi #! line there), not the session's",
     dict(argv=["./x.py"], session_cwd=plain_x, CLAUDE_PROJECT_DIR=str(pixi_x)), "pass"),
    ("no cwd, no CLAUDE_PROJECT_DIR: the session's (pixi #! line)", dict(argv=["./x.py"], session_cwd=pixi_x), "pass"),
    ("no cwd, no CLAUDE_PROJECT_DIR: the session's (python #! line)", dict(argv=["./x.py"], session_cwd=plain_x), "deny"),
    ("its cwd, not CLAUDE_PROJECT_DIR", dict(argv=["./x.py"], cwd=pixi_x, CLAUDE_PROJECT_DIR=str(plain_x)), "pass"),
    # in a workspace with no cwd, the same: the gateway's MCP server fills in the cwd before the workspace
    # applies (and the gateway refuses a cwd outside it)
    ("a workspace, no cwd: CLAUDE_PROJECT_DIR's (pixi #! line there)",
     dict(argv=["./x.py"], session_cwd=plain_x, CLAUDE_PROJECT_DIR=str(pixi_x), extra=WS), "pass"),
    ("a workspace, no cwd: CLAUDE_PROJECT_DIR's (python #! line there)",
     dict(argv=["./x.py"], session_cwd=pixi_x, CLAUDE_PROJECT_DIR=str(plain_x), extra=WS), "deny"),
    ("a workspace, no cwd, no CLAUDE_PROJECT_DIR: the session's (pixi #! line)", dict(argv=["./x.py"], session_cwd=pixi_x, extra=WS), "pass"),
    ("a workspace, no cwd, no CLAUDE_PROJECT_DIR: the session's (python #! line)", dict(argv=["./x.py"], session_cwd=plain_x, extra=WS), "deny"),
    ("a workspace and a cwd: looked up there", dict(argv=["./x.py"], cwd=pixi_x, extra=WS), "pass"),
    ("a workspace and a cwd, a python #! line there", dict(argv=["./x.py"], cwd=plain_x, session_cwd=pixi_x, extra=WS), "deny"),
    ("a workspace, no cwd, a pixi #! script by its absolute path", dict(argv=[f"{pixi_x}/x.py"], extra=WS), "pass"),
    ("a workspace, no cwd, a python #! script by its absolute path", dict(argv=[f"{plain_x}/x.py"], extra=WS), "deny"),
    ("an empty workspace: as none", dict(argv=["./x.py"], session_cwd=pixi_x, extra={"workspace": ""}), "pass"),
):
    got = gwdecision(**kw)
    check(f"{want:5} gateway job ./x.py, {label}", got == want, got)
r = gwreason(argv=["./x.py"], session_cwd=pixi_x, CLAUDE_PROJECT_DIR=str(plain_x), extra=WS)
check("gateway job in a workspace with no cwd, ./x.py with a python #! line: no folder note (its folder is known)",
      "`x.py` would run Python outside pixi" in r and "not looked up" not in r and "`cwd`" not in r, r)
check("pass  checkpoint job ./x.py with no cwd: the session's folder (CLAUDE_PROJECT_DIR is the gateway's default only)",
      jdecision(argv=["./x.py"], session_cwd=pixi_x, CLAUDE_PROJECT_DIR=str(plain_x)) == "pass")
r = gwreason(argv=["python", "train.py"], session_cwd=PLAIN, CLAUDE_PROJECT_DIR=str(jws))
check("gateway job with no cwd: the advice is for CLAUDE_PROJECT_DIR's project", f"{jws} is a pixi workspace" in r, r)
# Only tool_input's own keys count (the gateway reads b.get("host"), ...): a key in a nested object or in a
# string is another one, and the first "host" in the text no longer turns the check off.
for label, ti, want in (
    ("a host in a nested object before the command", {"meta": {"host": "gpu1"}, "command": "python x.py"}, "deny"),
    ("a key x\"host before the command", {'x"host': "gpu1", "command": "python x.py"}, "deny"),
    ("a host in a nested list", {"meta": [{"host": "gpu1"}], "command": "python x.py"}, "deny"),
    ("a command in a nested object before the command", {"meta": {"command": "ls"}, "command": "python x.py"}, "deny"),
    ("a key x\"command before the command", {'x"command': "ls", "command": "python x.py"}, "deny"),
    ("an argv in a nested object before the argv", {"meta": {"argv": ["ls"]}, "argv": ["python", "x.py"]}, "deny"),
    ("a value that says \"host\": \"gpu1\"", {"note": 'say "host": "gpu1"', "command": "python x.py"}, "deny"),
    ("the host after a nested object (a host of gateway.toml)", {"meta": {"host": "localhost"}, "host": "gpu1", "command": "python x.py"}, "pass"),
    ("the host after the command (a host of gateway.toml)", {"command": "python x.py", "host": "gpu1"}, "pass"),
    ("a nested command decoy, the command fine", {"meta": {"command": "python x.py"}, "command": "ls"}, "pass"),
):
    got = verdict(*run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWJOB, "tool_input": ti}))
    check(f"{want:5} gateway job, {label}", got == want, got)
GWRAW = '{"session_id":"s1","cwd":"%s","tool_name":"%s","tool_input":%s}'
for label, ti, want in (
    # Python's json (the gateway's) decodes keys: "\u0068ost" is host
    ("a \\u-escaped host key (a host of gateway.toml)", '{"command":"python x.py","\\u0068ost":"gpu1"}', "pass"),
    ("a \\u-escaped localhost", '{"command":"python x.py","host":"local\\u0068ost"}', "deny"),
    ("a \\u-escaped command key", '{"\\u0063ommand":"python x.py"}', "deny"),
    ("a nested tool_input with a command in it (its own command is ls)",
     '{"tool_input":{"command":"python x.py"},"command":"ls"}', "pass"),
):
    got = verdict(*run("pre", None, raw=GWRAW % (PLAIN, GWJOB, ti)))
    check(f"{want:5} gateway job, {label}", got == want, got)
raw = '{"x":{"tool_input":{"command":"ls"}},"session_id":"s1","cwd":"%s","tool_name":"%s","tool_input":{"command":"python x.py"}}'
check("deny  gateway job: the payload's own tool_input, not one nested before it", verdict(*run("pre", None, raw=raw % (PLAIN, GWJOB))) == "deny")
raw = json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWJOB,
                  "tool_input": {"meta": {"host": "gpu1", "argv": ["ls"]}, "argv": ["python", "x.py"]}}, indent=2)
check("deny  gateway job, pretty-printed JSON with decoys (lines between the keys)", verdict(*run("pre", None, raw=raw)) == "deny")
r = gwreason(argv=["python", "train.py"], session_cwd=jws)
check("gateway job refusal: the pixi way as the job's command, PIXI_GUARD=ask in front of it",
      "make the job's command `pixi run python ...`" in r and "`PIXI_GUARD=ask` in front of its command" in r
      and "PIXI_GUARD=ask <command>" not in r, r)
r = gwreason(argv=["pip", "install", "torch"], cwd=jpy)
check("gateway job refusal in a Python project not on pixi: the skill, then pixi add", "pixi-guard:project" in r and "Then manage packages with `pixi add" in r, r)
check("gateway job: PIXI_GUARD=ask in front of the command asks", gwdecision(command="PIXI_GUARD=ask python train.py") == "ask")
check("gateway job: PIXI_GUARD=ask as the first argv element asks", gwdecision(argv=["PIXI_GUARD=ask", "python", "train.py"]) == "ask")
check("gateway job ask: names the job", "this job runs `python`" in gwreason(argv=["PIXI_GUARD=ask", "python", "x"]))
for label, kw in (("neither command nor argv", {}), ("an empty argv", {"argv": []})):
    out, err = gwjob(**kw)
    check(f"gateway job with {label}: no output", out is None and not err, (out, err))
r = subprocess.run([ph["command"], *[a.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)) for a in ph["args"]]],
                   input=json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWJOB,
                                     "tool_input": {"argv": ["pip", "install", "x"]}}),
                   capture_output=True, text=True, env=env(True), cwd=str(TMP))
check("gateway job_start through hooks.json as Claude Code runs it: deny", r.returncode == 0 and '"deny"' in r.stdout, r)

print("the gateway's terminal_send: its text, read as typed into the gateway's bash")


def gwsend(text=None, enter=None, keys=None, cwd=PLAIN, sid="s1", **kw):
    ti = {"id": "t_01", "seq": 3}
    if text is not None:
        ti["text"] = text
    if keys is not None:
        ti["keys"] = keys
    if enter is not None:
        ti["enter"] = enter
    return run("pre", {"session_id": sid, "transcript_path": "/x", "cwd": str(cwd),
                       "hook_event_name": "PreToolUse", "tool_name": GWSEND, "tool_input": ti}, **kw)


def sdecision(text, **kw):
    return verdict(*gwsend(text, **kw))


for text, want in (
    ("pip install x\n", "deny"), ("python train.py\n", "deny"), ("ls\npython x\n", "deny"), ("cd sub && pytest -q\n", "deny"),
    # typed without Enter: a later send (or keys) may press it
    ("python train.py", "deny"), ("uv sync", "deny"), ("pixi run pip install x\n", "deny"),
    ("gate with openai -- python train.py\n", "deny"),
    ("pixi run python train.py\n", "pass"), ("ls -la\n", "pass"), ("git commit -m 'drop pip'\n", "pass"),
    ("cd sub && pixi run pytest -q\n", "pass"), ("y\n", "pass"), ("q", "pass"), ("echo python\n", "pass"),
    ("gate with openai -- pixi run python train.py\n", "pass"),
):
    got = sdecision(text)
    check(f"{want:5} terminal text {text!r}", got == want, got)
check("deny  terminal text with enter: true", sdecision("python x", enter=True) == "deny")
check("pass  terminal text with keys after it", sdecision("ls", keys=["Enter"]) == "pass")
for label, kw in (("keys only", {"keys": ["C-c"]}), ("an empty text", {"text": "", "enter": True})):
    out, err = gwsend(**kw)
    check(f"terminal send with {label}: no output", out is None and not err, (out, err))
check("terminal: PIXI_GUARD=ask asks", sdecision("PIXI_GUARD=ask python x\n") == "ask")
r = why(gwsend("PIXI_GUARD=ask python x\n")[0])
check("terminal ask: names the terminal input", "this terminal input runs `python`" in r, r)
r = why(gwsend("python x\n", cwd=jws)[0])
check("terminal refusal: send it again with PIXI_GUARD=ask, run it with pixi run",
      "send it again as `PIXI_GUARD=ask <command>`" in r and "pixi run python" in r and "job" not in r.replace(str(TMP), ""), r)
check("deny  terminal, its bash 5.2, Claude Code's shell zsh", sdecision(b52_deny[0]) == "deny")
r = why(gwsend(b52_deny[0])[0])
check("terminal, bash 5.2: the note says the terminal runs the gateway's bash",
      "the gateway's terminal runs its own bash, which is bash 5.2" in r, r)
check("pass  terminal, Claude Code's shell bash 3.2 (not the terminal's)", sdecision(b32_deny[0], **B32) == "pass")
# A terminal's folder is not known (it may have cd'd anywhere): a relative *.py path is not looked up there.
check("pass  Bash ./x.py with a pixi #! line in the cwd", decision("./x.py", cwd=pixi_x) == "pass")
check("deny  terminal ./x.py with a pixi #! line in the session's cwd", sdecision("./x.py\n", cwd=pixi_x) == "deny")
r = why(gwsend("./x.py\n", cwd=pixi_x)[0])
check("terminal ./x.py: the reason says to type its absolute path", "terminal's folder" in r and "absolute path" in r, r)
check("pass  terminal: a pixi #! script by its absolute path", sdecision(f"{pixi_x}/x.py\n") == "pass")
check("deny  terminal: a python #! script by its absolute path", sdecision(f"{plain_x}/x.py\n") == "deny")
r = subprocess.run([ph["command"], *[a.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)) for a in ph["args"]]],
                   input=json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWSEND,
                                     "tool_input": {"id": "t_01", "seq": 1, "text": "pip install x\n"}}),
                   capture_output=True, text=True, env=env(True), cwd=str(TMP))
check("terminal_send through hooks.json as Claude Code runs it: deny", r.returncode == 0 and '"deny"' in r.stdout, r)
for label, ti, want in (
    ("a text in a nested object before the text", {"meta": {"text": "ls"}, "text": "pip install x\n"}, "deny"),
    ("a key x\"text before the text", {'x"text': "ls", "text": "pip install x\n"}, "deny"),
    ("a nested text decoy, the text fine", {"meta": {"text": "pip install x"}, "text": "ls\n"}, "pass"),
):
    got = verdict(*run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWSEND, "tool_input": {"id": "t_01", "seq": 1, **ti}}))
    check(f"{want:5} terminal, {label}", got == want, got)

print("bash 5.3 and later: ${ CMD; } and ${| CMD; } run CMD (to zsh, dash and older bash, a bad substitution)")
B53 = {"SHELL": FAKE_SHELLS["bash53"]}
# refused when bash 5.3 or later runs them, read as before (plain text) otherwise
b53_deny = [
    "echo ${ python -c 1; }", "echo ${| python -c 1; }", "echo ${|python -c 1; }", "echo ${\tpython -c 1; }",
    "echo ${\npython -c 1\n}", "echo \"${ pip install x; }\"", "x=${ python -V; }", "echo ${ python -c 1;}",
    "echo ${ if true; then python; fi }", "echo ${ case x in x) python;; esac }", "echo ${ echo a | python; }",
    "echo ${ ${ python; }; }", "echo ${ echo a; } ${ python; }", "echo ${ function f { python; }; f; }",
    "echo ${ f() { python; }; f; }", "echo ${ echo $(echo }) ; python; }", "echo ${ echo `echo }`; python; }",
    "echo ${ echo \"}\"; python; }", "echo ${x:-${ python; }}", "echo \"${x:-${ python; }}\"",
    "cat <<EOF\n${ python; }\nEOF", "echo $(( ${ python; } + 1 ))", "echo $[ ${ python; } + 1 ]", "a=(${ python; })",
    "[[ ${ python; } ]]", "for i in ${ python; }; do :; done", "echo hi >${ pip list; }", "echo `echo ${ python; }`",
    "echo ${ echo a;}b${ python;}", "echo \"$(echo ${ pip list; })\"", "echo ${ (python) }",
    "echo ${ while false; do :; done; python; }", "echo ${ [[ -n x ]] && python; }", "eval 'echo ${ pip list; }'",
    "echo \"${ coproc c { :; }; python; }\"", "echo \"${ coproc { :; }; python; }\"",
    # in "...": its "}" is the one where a reserved word may come (not after a word), past its groups
    "echo \"${ echo }; python; }\"", "echo \"${ f() { :; }; python; }\"", "echo \"${ function f { :; }; python; }\"",
    # a heredoc in it ends at a line that starts with its delimiter and has a "}" after it; the rest of
    # that line is code
    "echo ${ cat <<'EOF'\nEOF python; }\nEOF\n}",
    # a "}" in a case pattern ends nothing
    "echo \"${ case } in (}) python;; esac; }\"", "echo \"${ case x in x) echo;; (}) :;; esac; python; }\"",
    "cat <<EOF\n${ case } in (}) python;; esac; }\nEOF", "echo \"${ case } in\n}) python;;\nesac\n}\"",
    "echo \"${ case x in x|}) python;; esac; }\"",
]
# refused when bash 5.3 runs them (the rest is read as code by the other readings too)
b53_deny_too = [
    "echo ${ { echo a; }; python; }", "echo ${ echo a; #}\npython; }", "echo ${ echo a # }\npython\n}",
    "echo ${ echo }; python; }", "echo ${ echo a; }}; python",
    "echo ${ cat <<'EOF'\nEOF }\npython\nEOF\n}", "echo ${ cat <<'EOF'\nEOFx }\npython\nEOF\n}",
    "echo ${ cat <<'EOF'\nEOF {}\npython\nEOF\n}", "echo ${ cat <<'EOF' | cat\nEOFa }\npython\nEOF\n}",
    "echo ${ cat <<EOF\n$(python)\nEOF\n}",
    # in "...": the heredoc ends at "EOF }", whose "}" ends it; a reserved word may come after esac
    "echo \"${ cat <<'EOF'\nEOF }\"; python", "echo \"${ case x in x) :;; esac }\"; python",
    "echo \"${ while false; do :; done }\"; python", "echo \"${ [[ -n x ]] }\"; python",
]
b53_pass = [
    "echo ${ echo a; }", "echo ${| REPLY=python; }", "echo ${ echo a; } python", "echo ${ echo a; }python",
    "echo ${ if true; then echo; fi }; echo after", "echo ${ [[ -n x ]] }", "echo ${ (echo a) }", "echo ${ (( 1 )) }",
    "echo ${ case x in x) echo;; esac; }", "echo ${ echo a & }", "echo ${ cat <<'EOF'\nhello python\nEOF\n}",
    "echo ${ cat <<'EOF'\nEOF)\npython\nEOF\n}", "cat <<'EOF'\n${ python; }\nEOF", "echo '${ python; }'",
    "echo ${ echo a; } # ${ python; }", "echo ${ echo a; } }", "echo ${ }", "echo ${| }", "echo ${ echo pip; }",
    "echo $( echo ${ cat <<'EOF'\nEOF )\npython\nEOF\n}; )", "echo ${ echo $( cat <<'EOF'\nEOF }\npython\nEOF\n); }",
    "git commit -m \"${ cat <<'EOF'\nUse pixi, not pip (it's faster)\nEOF\n}\"", "echo ${ echo a\t;\t}",
    "echo ${ time echo; }", "echo ${ ! false; }", "echo ${ echo a; } \"${ echo b; }\"", "echo ${ echo $'}'; echo python; }",
    "echo ${ echo a; }}", "echo ${x:+${ echo python; }}", "echo ${ coproc c { echo python; }; }",
    "echo \"${ if true; then :; fi }; python; }\"", "echo \"${ { :; } }; python; }\"", "echo \"${ echo a; }; python; }\"",
    "echo \"${ cat <<'EOF'\nEOF }\npython\nEOF\n}\"",
    # (a "}" or "{" in a case pattern is no reserved word)
    "echo \"${ case } in (}) echo;; esac; }\"", "echo \"${ case x in x) :;; ({ ) :;; esac; } python\"",
    # (a "}" right after a keyword ends it: a syntax error, nothing runs)
    "echo \"${ if }; then python; fi; }\"", "echo \"${ if true; then }; python; fi; }\"",
    "echo \"${ if :; then :; else }; python; fi; }\"", "echo \"${ if :; then :; elif }; then python; fi; }\"",
    "echo \"${ while }; do python; done; }\"", "echo \"${ while :; do }; python; done; }\"",
    "echo \"${ until }; do python; done; }\"", "echo \"${ ! }; python; }\"", "echo \"${ time }; python; }\"",
    "echo \"${ coproc }; python; }\"", "echo \"${ function f }; python; }\"",
    # plain ${...}: untouched
    "echo ${VAR}", "echo ${VAR:-x}", "echo ${x:-python}", "echo \"${HOME:-/tmp}/python\"", "echo ${#x} ${x%% *}",
]
expect("deny", b53_deny + b53_deny_too, "bash 5.3+: ", **B53)
expect("pass", b53_pass, "bash 5.3+: ", **B53)
expect("deny", ["${PYTHON:-python3} x.py", "echo ${x:-$(python -V)}", "${VENV:-.venv}/bin/uvicorn app"],
       "bash 5.3+, plain ${...} as before: ", **B53)
# what ${ CMD; } prints into a shell's code is code, as with $(...); what ${| CMD; } prints is not its value
expect("deny", ["bash -c \"${ cat <<'EOF'\npython x\nEOF\n}\"", "eval \"${ echo python x; }\""], "bash 5.3+: ", **B53)
expect("pass", ["bash -c \"${| cat <<'EOF'\npython x\nEOF\n}\"", "bash -c \"${ cat <<'EOF'\nls\nEOF\n}\""], "bash 5.3+: ", **B53)
# under pixi only an installer is refused, as in $(...)
expect("deny", ["pixi run bash -c 'echo ${ pip install x; }'"], "bash 5.3+: ", **B53)
expect("pass", ["pixi run bash -c 'echo ${ pip list; }'", "pixi run python ${ echo x.py; }"], "bash 5.3+: ", **B53)
for label, kw in (("zsh", {}), ("bash 5.2", {"SHELL": FAKE_SHELLS["bash52"]}), ("bash 3.2", B32)):
    expect("pass", b53_deny, f"{label}: ", **kw)
for label in ("bash510", "bash60"):
    check(f"bash 5.3+: {label} reads it so too", decision(b53_deny[0], SHELL=FAKE_SHELLS[label]) == "deny")
check("bash 5.3+: CLAUDE_CODE_SHELL counts over SHELL",
      decision(b53_deny[0], SHELL=FAKE_SHELLS["bash52"], CLAUDE_CODE_SHELL=FAKE_SHELLS["bash53"]) == "deny")
check("bash 5.3+: the refusal names the program", hit(b53_deny[0], **B53) == "python")
check("bash 5.3+: PIXI_GUARD=ask still asks", decision("PIXI_GUARD=ask " + b53_deny[0], **B53) == "ask")
for cmd in b53_deny[:3]:
    got = jdecision(command=cmd, PIXI_GUARD_JOB_SH=FAKE_SHELLS["bash53"])
    check(f"deny  job, /bin/sh bash 5.3+, Claude Code's shell zsh: {cmd!r}", got == "deny", got)
    # a checkpoint job is read as the gateway's bash too (it runs the job while it serves the session)
    for label, kw in (("bash 5.2", {"PIXI_GUARD_JOB_SH": FAKE_SHELLS["bash52"]}), ("not bash", JSH), ("bash 3.2", JSH32)):
        got = jdecision(command=cmd, **kw)
        check(f"deny  job, /bin/sh {label}, read as the gateway's bash 5.3: {cmd!r}", got == "deny", got)
    # the gateway's bash (5.2 today, any version its pixi.toml gets): read as 5.3 too
    check(f"deny  gateway job, its own bash, Claude Code's shell zsh: {cmd!r}", gwdecision(command=cmd) == "deny")
    check(f"deny  terminal, the gateway's bash: {cmd!r}", sdecision(cmd + "\n") == "deny")
check("pass  gateway job: ${ CMD; } with nothing to refuse", gwdecision(command=b53_pass[0]) == "pass")
# ... and as 5.2, where ${ ... } is a bad substitution up to its first "}" (a bare "{" does not nest, as it
# does in zsh): bash 5.2 drops the rest of that line and runs the next ones, even what 5.3 reads as a heredoc.
gw52_deny = [
    "echo ${ cat <<'EOF'\n}\npython x.py\nEOF\n}", "echo ${ cat <<EOF\n}\npython x.py\nEOF\n}",
    "x=${ cat <<'EOF'\nsome } text\npip install x\nEOF\n}", "echo ${ cat <<'EOF'\n{ }\npython x.py\nEOF\n}",
    # (a heredoc in $(...) ending at a line that starts with "EOF" and has a ")" after it, as bash 5.2 ends it)
    "echo ${ cat <<'X'\n}\necho $(cat <<EOF\nEOF :); python\nEOF\n)\nX\n}",
    "echo ${ cat <<'X'\n}\necho \"$(cat <<'EOF'\nEOFError :) `python -V`\nEOF\n)\"\nX\n}",
]
gw52_pass = [
    "echo ${ echo hi; }", "echo \"${ date; }\"", "x=${| REPLY=hello; }; echo $x", "echo ${ cat <<'EOF'\npip install x\nEOF\n}",
    "echo \"${ cat <<'EOF'\n{ }\npython x.py\nEOF\n}\"", "git commit -m \"${ cat <<'EOF'\nUse pixi, not pip (it's faster)\nEOF\n}\"",
]
for cmd in gw52_deny:
    check(f"deny  gateway job, read as bash 5.2 too: {cmd!r}", gwdecision(command=cmd) == "deny")
    check(f"deny  terminal, read as bash 5.2 too: {cmd!r}", sdecision(cmd + "\n") == "deny")
    check(f"deny  checkpoint job, /bin/sh not bash, read as the gateway's bash 5.2: {cmd!r}", jdecision(command=cmd, **JSH) == "deny")
expect("pass", gw52_deny, "bash 5.3+ as Claude Code's shell (no 5.2 reading): ", **B53)
for cmd in gw52_pass:
    check(f"pass  gateway job: {cmd!r}", gwdecision(command=cmd) == "pass")
    check(f"pass  checkpoint job, /bin/sh not bash: {cmd!r}", jdecision(command=cmd, **JSH) == "pass")
    check(f"pass  terminal: {cmd!r}", sdecision(cmd + "\n") == "pass")
r = gwreason(command=gw52_deny[0])
check("gateway job read as bash 5.2: the refusal names the program and says why",
      "`python`" in r and "bash 5.2 has no `${ CMD; }`" in r, r)
r = jreason(command=gw52_deny[0], **JSH)
check("checkpoint job read as the gateway's bash 5.2: the note says the gateway runs it",
      "while the gateway serves the session, it runs checkpoint's jobs with its own bash, which is bash 5.2 or later, and bash 5.2 has no"
      in r and "/bin/sh" not in r, r)
# Real shells, when here: the usual places, and PIXI_GUARD_TEST_BASH (paths separated by ":").
REAL_BASH = [p for p in ("/bin/bash", shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/opt/local/bin/bash",
                         *os.environ.get("PIXI_GUARD_TEST_BASH", "").split(os.pathsep)) if p and os.path.exists(p)]
real53 = next((p for p in REAL_BASH if bash_version(p) >= (5, 3)), None)
real52 = next((p for p in REAL_BASH if bash_version(p) == (5, 2)), None)
older = [p for p in REAL_BASH if (0, 0) < bash_version(p) < (5, 3)]
if real53:
    for cmd in b53_deny + b53_deny_too:
        check(f"real bash 5.3+ runs the tool in {cmd!r}", runs_tool(real53, cmd))
    for cmd in b53_pass + gw52_deny + gw52_pass:
        check(f"real bash 5.3+ runs nothing in {cmd!r}", not runs_tool(real53, cmd))
for v, p in sorted({bash_version(p): p for p in reversed(older)}.items()):  # (one of each version)
    for cmd in b53_deny:
        check(f"real bash {'.'.join(map(str, v))} runs nothing in {cmd!r}", not runs_tool(p, cmd))
if real52:
    for cmd in gw52_deny:
        check(f"real bash 5.2 runs the tool in {cmd!r}", runs_tool(real52, cmd))
    for cmd in gw52_pass:
        check(f"real bash 5.2 runs nothing in {cmd!r}", not runs_tool(real52, cmd))

print("a shell prefix (CLAUDE_CODE_SHELL_PREFIX, the gateway's gate-sh): while the gateway's managed shell isn't ready, it runs "
      "the command in the shell that made its snapshot, found the way Claude Code finds its shell: CLAUDE_CODE_SHELL, then SHELL "
      "(when in the environment), then the preferred shell on PATH, in /bin, /usr/bin, /usr/local/bin, /opt/homebrew/bin, the "
      "other there, the other on PATH (bash preferred when SHELL holds \"bash\", else zsh); the first executable bash or zsh by "
      "its absolute path. Never /bin/sh. Once ready, in the gateway's bash")
PFX = {"CLAUDE_CODE_SHELL_PREFIX": "/x/hooks/gate-sh"}
D32, D53 = str(Path(FAKE_SHELLS["bash32"]).parent), str(Path(FAKE_SHELLS["bash53"]).parent)
# (a zsh here on every machine; a versioned bash 5.3 and zsh, a zsh and a bash 3.2 that aren't executable, a bash 3.2 whose
# name holds "bash" without starting with it, a bash 5.3 in a folder whose name holds "=", a folder named bash)
for d, name, text in (("zsh", "zsh", "zsh 5.9 (x86_64-apple-darwin24.0)"), ("vbash", "bash5", "GNU bash, version 5.3.15(1)-release"),
                      ("vzsh", "zsh-5.9", "zsh 5.9 (x86_64-apple-darwin24.0)"), ("noxzsh", "zsh", "zsh 5.9"),
                      ("noxbash", "bash", "GNU bash, version 3.2.57(1)-release"), ("gnu32", "gnubash", "GNU bash, version 3.2.57(1)-release"),
                      ("eq=53", "bash", "GNU bash, version 5.3.15(1)-release")):
    (TMP / d).mkdir()
    (TMP / d / name).write_text(f'#!/bin/sh\necho "{text}"\n')
    (TMP / d / name).chmod(0o644 if d.startswith("nox") else 0o755)
(TMP / "dirbash" / "bash").mkdir(parents=True)
(TMP / "noshells").mkdir()
ZSH, DZ, VBASH, VZSH, GNU32 = (str(TMP / "zsh" / "zsh"), str(TMP / "zsh"), str(TMP / "vbash" / "bash5"), str(TMP / "vzsh" / "zsh-5.9"),
                               str(TMP / "gnu32" / "gnubash"))
EQ53, FISH = str(TMP / "eq=53" / "bash"), "/usr/local/bin/fish"
# (SHELL is a sh that is bash 3.2, as macOS's /bin/sh is)
(TMP / "sh32").mkdir()
(TMP / "sh32" / "sh").write_text('#!/bin/sh\necho "GNU bash, version 3.2.57(1)-release (arm64-apple-darwin25)"\n')
(TMP / "sh32" / "sh").chmod(0o755)
SH32 = str(TMP / "sh32" / "sh")
# (a relative PATH entry, from the folder the hook runs in, whose bash is 5.3)
REL53 = os.path.relpath(D53)


def path_without(*names):
    """A PATH of one folder that links every program on BASE_PATH (the first of each name) but names."""
    d = TMP / ("path-without-" + "-".join(names))
    d.mkdir()
    for p in BASE_PATH.split(os.pathsep):
        try:
            entries = sorted(os.listdir(p))
        except OSError:
            continue
        for n in entries:
            f = os.path.join(p, n)
            if n not in names and not os.path.lexists(d / n) and os.path.isfile(f) and os.access(f, os.X_OK):
                (d / n).symlink_to(f)
    return str(d)


# (NOSH: a PATH without bash or zsh; NOSHELLS: none in the places either. PIXI_GUARD_SHELL_DIRS replaces the places,
# ":" between them; FB32, FB53: one folder, whose bash is 3.2 or 5.3, and no bash or zsh on PATH)
NOSH = path_without("bash", "zsh")
NOSHELLS = {"PIXI_GUARD_SHELL_DIRS": str(TMP / "noshells"), "PATH": NOSH}
FB32, FB53 = {"PIXI_GUARD_SHELL_DIRS": D32, "PATH": NOSH}, {"PIXI_GUARD_SHELL_DIRS": D53, "PATH": NOSH}


def gate_fallback(e, places, family=""):
    """gate-sh's not-ready shell (its gw_fallback_shell, the gateway's managed.fallback_shell) for a snapshot made by
    `family` ("": none): CLAUDE_CODE_SHELL, then SHELL, then the preferred shell on PATH, in the places, the other there,
    the other on PATH; the first that is an absolute path without "=" to an executable file holding zsh (a zsh) or bash
    (a bash), of that family."""
    def fam(p):
        return "zsh" if "zsh" in p else "bash" if "bash" in p else ""

    def fits(p):
        return (p.startswith("/") and "=" not in p and fam(p) != "" and family in ("", fam(p))
                and os.path.isfile(p) and os.access(p, os.X_OK))
    path = [d for d in e.get("PATH", "").split(":") if d.startswith("/") and "=" not in d]

    def which(n):
        p = next((f"{d}/{n}" for d in path if os.path.isfile(f"{d}/{n}") and os.access(f"{d}/{n}", os.X_OK)), None)
        return p if p and fits(p) else None

    def where(n):
        return next((f"{d}/{n}" for d in places if fits(f"{d}/{n}")), None)
    a, b = ("bash", "zsh") if "bash" in e.get("SHELL", "") else ("zsh", "bash")
    for c in (e.get("CLAUDE_CODE_SHELL", ""), e.get("SHELL", "")):
        if fits(c):
            return c
    return which(a) or where(a) or where(b) or which(b)


def prefix_want(**kw):
    """The decision on b32_deny[0] behind the prefix: deny when a bash 3.x may run it. That is Claude Code's shell as
    bashver() reads it (CLAUDE_CODE_SHELL, else SHELL, named bash*), or gate-sh's not-ready shell with no snapshot, with
    one Claude Code made in this environment (its rule is gate-sh's without a snapshot), or with one made by a
    CLAUDE_CODE_SHELL that isn't there now. (Rows without SHELL set CLAUDE_CODE_SHELL to a fish, so that bashver()
    doesn't ask the SHELL bash as sh sets itself.)"""
    e = env(True, **kw)
    places = [d for d in e.get("PIXI_GUARD_SHELL_DIRS", "").split(":") if d]
    shells = {gate_fallback(e, places)}
    ccs = e.get("CLAUDE_CODE_SHELL", "")
    for family in {"zsh" if "zsh" in p else "bash" for p in shells | {ccs} if p and ("bash" in p or "zsh" in p)}:
        shells.add(gate_fallback(e, places, family))
    cc = ccs or e.get("SHELL", "")
    if os.path.basename(cc).startswith("bash"):
        shells.add(cc)
    return "deny" if any(s and "zsh" not in s and bash_major(s) == 3 for s in shells) else "pass"


# Not taken (not bash or zsh, relative, missing, not executable, holding "=", a sh that is bash 3.2): the first bash found
for label, shell in (("not bash or zsh", str(FAKE_PIXI)), ("sh", "/bin/sh"), ("fish", FISH), ("empty", ""),
                     ("a zsh that isn't executable", str(TMP / "noxzsh" / "zsh")), ("a sh that is bash 3.2", SH32),
                     ("bash by a relative name", "bash"), ("a bash that isn't there", "/nowhere/bash"),
                     ("a bash 5.3 in a folder holding '='", EQ53)):
    for cmd in b32_deny[:3]:
        got = decision(cmd, SHELL=shell, **PFX, **FB32)
        check(f"deny  prefix, SHELL {label}, the first bash found bash 3.2: {cmd[:40]!r}", got == "deny", got)
        got = decision(cmd, SHELL=shell, **FB32)
        check(f"pass  no prefix, SHELL {label}, the first bash found bash 3.2 (not asked): {cmd[:40]!r}", got == "pass", got)
    got = decision(b32_deny[0], SHELL=shell, **PFX, **FB53)
    check(f"pass  prefix, SHELL {label}, the first bash found bash 5.3 (never /bin/sh): {b32_deny[0][:40]!r}", got == "pass", got)
for cmd in b32_pass[:3]:
    got = decision(cmd, SHELL=str(FAKE_PIXI), **PFX, **FB32)
    check(f"pass  prefix, SHELL not bash or zsh, the first bash found bash 3.2: {cmd[:40]!r}", got == "pass", got)
for cmd in b52_deny[:2] + b53_deny[:2]:
    got = decision(cmd, SHELL=str(FAKE_PIXI), **PFX, **FB53)
    check(f"deny  prefix, SHELL not bash or zsh, the first bash found bash 5.3: {cmd[:40]!r}", got == "deny", got)
# Which shell, row by row; prefix_want() (gate-sh's rule, in Python) agrees with each. None found: gate-sh refuses the
# command (126), so only the gateway's bash runs it.
NOCC, CC, GONE = {"CLAUDE_CODE_SHELL": "/nowhere/fish"}, "CLAUDE_CODE_SHELL", "/nowhere/bash"


def at(places, *path):
    """The places (":" between them), and a PATH of the folders given, then NOSH."""
    return {"PIXI_GUARD_SHELL_DIRS": places, "PATH": ":".join([*path, NOSH])}


for label, kw, want in (
        # CLAUDE_CODE_SHELL, then SHELL
        ("SHELL zsh", {"SHELL": ZSH, **FB32}, "pass"),
        ("SHELL bash 5.2", {"SHELL": FAKE_SHELLS["bash52"], **FB32}, "pass"),
        ("SHELL bash 3.2", {**B32, **FB53}, "deny"),
        ("CLAUDE_CODE_SHELL bash 5.2", {CC: FAKE_SHELLS["bash52"], "SHELL": str(FAKE_PIXI), **FB32}, "pass"),
        ("CLAUDE_CODE_SHELL bash 5.2 (the gateway's), SHELL bash 3.2", {CC: FAKE_SHELLS["bash52"], **B32, **FB32}, "pass"),
        ("CLAUDE_CODE_SHELL zsh, SHELL bash 3.2", {CC: ZSH, **B32, **FB32}, "pass"),
        ("CLAUDE_CODE_SHELL zsh by a relative name, SHELL bash 3.2", {CC: "zsh", **B32, **FB53}, "deny"),
        ("CLAUDE_CODE_SHELL a bash that isn't there, SHELL bash 3.2", {CC: GONE, **B32, **FB53}, "deny"),
        ("CLAUDE_CODE_SHELL a bash 5.3 in a folder holding '=', SHELL bash 3.2", {CC: EQ53, **B32, **FB53}, "deny"),
        # any path holding the shell's name (Claude Code's rule)
        ("SHELL a versioned bash name (a bash 5.3)", {"SHELL": VBASH, **FB32}, "pass"),
        ("SHELL a versioned zsh name", {"SHELL": VZSH, **FB32}, "pass"),
        ("CLAUDE_CODE_SHELL a versioned bash name (a bash 5.3), SHELL bash 3.2", {CC: VBASH, **B32, **FB53}, "pass"),
        ("CLAUDE_CODE_SHELL a bash 3.2 named gnubash (bashver doesn't ask it)", {CC: GNU32, "SHELL": ZSH, **FB53}, "deny"),
        # SHELL only when it is in the environment: bash as sh (macOS's /bin/sh) sets one itself, the login shell
        ("SHELL not in the environment, bash 3.2 in the places", {"SHELL": None, **NOCC, **FB32}, "deny"),
        ("SHELL not in the environment, a zsh on PATH, bash 3.2 in the places", {"SHELL": None, **NOCC, **at(D32, DZ)}, "pass"),
        # the preferred shell (bash when SHELL holds "bash", else zsh) on PATH, in the places, the other there, the other
        # on PATH
        ("SHELL fish, a zsh on PATH, bash 3.2 in the places", {"SHELL": FISH, **at(D32, DZ)}, "pass"),
        ("SHELL fish, a zsh then bash 3.2 in the places", {"SHELL": FISH, **at(f"{DZ}:{D32}")}, "pass"),
        ("SHELL fish, bash 5.3 then bash 3.2 in the places", {"SHELL": FISH, **at(f"{D53}:{D32}")}, "pass"),
        ("SHELL fish, bash 3.2 then bash 5.3 in the places", {"SHELL": FISH, **at(f"{D32}:{D53}")}, "deny"),
        ("SHELL fish, bash 5.3 in the places, bash 3.2 on PATH", {"SHELL": FISH, **at(D53, D32)}, "pass"),
        ("SHELL empty, no places, bash 3.2 on PATH", {"SHELL": "", **at("", D32)}, "deny"),
        ("SHELL empty, no places, bash 5.3 on PATH", {"SHELL": "", **at("", D53)}, "pass"),
        ("SHELL a bash that isn't there, bash 5.3 in the places, bash 3.2 on PATH", {"SHELL": GONE, **at(D53, D32)}, "deny"),
        ("SHELL a bash that isn't there, bash 3.2 in the places, bash 5.3 on PATH", {"SHELL": GONE, **at(D32, D53)}, "pass"),
        ("SHELL a bash that isn't there, a zsh then bash 3.2 in the places", {"SHELL": GONE, **at(f"{DZ}:{D32}")}, "deny"),
        ("SHELL fish, only a zsh, in the places", {"SHELL": FISH, **at(DZ)}, "pass"),
        ("SHELL fish, only a zsh, on PATH", {"SHELL": FISH, **at(str(TMP / "noshells"), DZ)}, "pass"),
        ("SHELL fish, no bash or zsh anywhere", {"SHELL": FISH, **NOSHELLS}, "pass"),
        # on PATH: absolute folders without "=", an executable file
        ("SHELL fish, a relative PATH entry with bash 5.3, then bash 3.2", {"SHELL": FISH, **at("", REL53, D32)}, "deny"),
        ("SHELL fish, a bash that isn't executable on PATH, then bash 3.2",
         {"SHELL": FISH, **at("", str(TMP / "noxbash"), D32)}, "deny"),
        ("SHELL fish, a folder named bash on PATH, then bash 3.2", {"SHELL": FISH, **at("", str(TMP / "dirbash"), D32)}, "deny"),
        ("SHELL fish, a bash 5.3 in a folder holding '=' on PATH, then bash 3.2",
         {"SHELL": FISH, **at("", str(TMP / "eq=53"), D32)}, "deny"),
        # a CLAUDE_CODE_SHELL bash that isn't there now (the gateway's, during a rebuild) may have made the snapshot: gate-sh
        # then runs the first bash; one that names a zsh changes nothing (a zsh is read anyway)
        ("CLAUDE_CODE_SHELL a bash that isn't there, SHELL zsh, bash 3.2 in the places", {CC: GONE, "SHELL": ZSH, **at(D32, DZ)}, "deny"),
        ("CLAUDE_CODE_SHELL a bash that isn't there, SHELL zsh, bash 5.3 in the places", {CC: GONE, "SHELL": ZSH, **at(D53, DZ)}, "pass"),
        ("CLAUDE_CODE_SHELL a zsh that isn't there, SHELL zsh, bash 3.2 in the places",
         {CC: "/nowhere/zsh", "SHELL": ZSH, **at(D32, DZ)}, "pass")):
    got, model = decision(b32_deny[0], **kw, **PFX), prefix_want(**kw)
    check(f"{want:5} prefix, {label}: {b32_deny[0][:40]!r}", got == want == model, f"got {got}, gate-sh's rule {model}")
for cmd in b52_deny[:1] + b53_deny[:1] + gw52_deny[:1]:
    got = decision(cmd, SHELL="", **PFX, **NOSHELLS)
    check(f"deny  prefix, no bash or zsh anywhere: still read as the gateway's bash: {cmd[:40]!r}", got == "deny", got)
for label, ccs in (("zsh by a relative name", "zsh"), ("a bash that isn't there", "/nowhere/bash")):
    got = decision(b32_deny[0], CLAUDE_CODE_SHELL=ccs, **B32, **FB53)
    check(f"pass  no prefix, CLAUDE_CODE_SHELL {label}, SHELL bash 3.2: Claude Code's shell", got == "pass", got)
r = reason(b32_deny[0], SHELL=str(FAKE_PIXI), **PFX, **FB32)
check("prefix, the first bash found bash 3.2: the note says the gateway's not-ready shell runs it with that bash",
      f"while the gateway's managed shell isn't ready it runs them with {FAKE_SHELLS['bash32']}, which here is bash 3.2 "
      "(the version of macOS's /bin/bash)" in r and "git commit -F" in r and "/bin/sh" not in r, r)
r = reason(b32_deny[0], SHELL="", **PFX, PIXI_GUARD_SHELL_DIRS="", PATH=f"{D32}:{NOSH}")
check("prefix, bash 3.2 found on PATH: the note names it",
      f"while the gateway's managed shell isn't ready it runs them with {FAKE_SHELLS['bash32']}, which here is bash 3.2" in r, r)
r = reason(b32_deny[0], CLAUDE_CODE_SHELL="zsh", **B32, **PFX, **FB53)
check("prefix, SHELL bash 3.2 that isn't Claude Code's shell: the note names it",
      f"while the gateway's managed shell isn't ready it runs them with {FAKE_SHELLS['bash32']}, which here is bash 3.2" in r, r)
# ... and once ready (its build may finish during the session), in the gateway's own bash, whatever CLAUDE_CODE_SHELL is:
# read as for a gateway job
for cmd in b52_deny[:2] + b53_deny[:2] + gw52_deny[:2]:
    for label, kw in (("SHELL zsh", {"SHELL": ZSH}), ("SHELL not bash or zsh, the first bash found bash 3.2", {"SHELL": str(FAKE_PIXI), **FB32}),
                      ("CLAUDE_CODE_SHELL zsh", {"CLAUDE_CODE_SHELL": ZSH}), ("CLAUDE_CODE_SHELL bash 5.1", {"CLAUDE_CODE_SHELL": FAKE_SHELLS["bash51"]}),
                      ("CLAUDE_CODE_SHELL bash 5.3", {"CLAUDE_CODE_SHELL": FAKE_SHELLS["bash53"]})):
        got = decision(cmd, **kw, **PFX)
        check(f"deny  prefix, {label}: read as the gateway's bash too: {cmd[:40]!r}", got == "deny", got)
    # (gw52_deny: only bash 5.3 alone reads those as harmless, see above)
    for label, kw in (("no prefix, SHELL zsh", {"SHELL": ZSH}), ("no prefix, CLAUDE_CODE_SHELL bash 5.1", {"CLAUDE_CODE_SHELL": FAKE_SHELLS["bash51"]})) \
            if cmd not in gw52_deny else (("no prefix, CLAUDE_CODE_SHELL bash 5.3", {"CLAUDE_CODE_SHELL": FAKE_SHELLS["bash53"]}),):
        got = decision(cmd, **kw)
        check(f"pass  {label}: {cmd[:40]!r}", got == "pass", got)
for cmd in b52_pass + gw52_pass:
    got = decision(cmd, SHELL=ZSH, **PFX)
    check(f"pass  prefix, SHELL zsh: {cmd[:40]!r}", got == "pass", got)
r = reason(b52_deny[0], SHELL=ZSH, **PFX)
check("prefix, the gateway's bash: the note says the managed shell runs it there once ready",
      "once the gateway's managed shell is ready it runs them with the gateway's own bash, which is bash 5.2" in r, r)
r = reason(b52_deny[0], SHELL=FAKE_SHELLS["bash53"], **PFX)
check("prefix, SHELL bash 5.3: the note is Claude Code's shell's", "Claude Code's shell here is bash 5.2" in r, r)
check(f"deny  prefix, SHELL bash 3.2: {job32_53!r} read as bash 3.2 too", decision(job32_53, **B32, **PFX) == "deny")
check(f"pass  prefix, SHELL zsh: {job32_53!r}", decision(job32_53, SHELL=ZSH, **PFX) == "pass")
check("prefix: a job is read as before (checkpoint's /bin/sh, the gateway's bash)",
      jdecision(command=b32_deny[0], SHELL=str(FAKE_PIXI), **PFX, **FB32, **JSH) == "pass"
      and gwdecision(command=b32_deny[0], SHELL=str(FAKE_PIXI), **PFX, **FB32) == "pass")
# The real places and PATH (no override): whatever this machine has (/bin/zsh and macOS's /bin/bash 3.2; a bash 5 on Linux).
for label, shell in (("not bash or zsh (zsh preferred)", str(FAKE_PIXI)), ("a bash that isn't there (bash preferred)", "/nowhere/bash")):
    want = prefix_want(SHELL=shell, PIXI_GUARD_SHELL_DIRS="/bin:/usr/bin:/usr/local/bin:/opt/homebrew/bin")
    got = decision(b32_deny[0], SHELL=shell, **PFX)
    check(f"{want:5} prefix, SHELL {label}, this machine's shells", got == want, got)
check("prefix: the usual places are Claude Code's (and gate-sh's): /bin, /usr/bin, /usr/local/bin, /opt/homebrew/bin",
      "${PIXI_GUARD_SHELL_DIRS-/bin:/usr/bin:/usr/local/bin:/opt/homebrew/bin}" in GUARD.read_text())
# Budgets: the look-ahead for the "}" is one pass, nested ones count as in $(...) (200 deep at most). A
# gateway job reads the text again as bash 5.2 (view 5), which finds 30 nested "${x:-'$(...)'}" too deep,
# as it did before bash 5.3 was read. A checkpoint job with /bin/sh bash 3.2 (macOS's) and a command behind a
# prefix whose not-ready shell may be bash 3.2 are read as bash 3.2 too (view 3), and as zsh and dash where they
# end a ${ ...} elsewhere: four readings of 120KB or more are over the parse budget (b32want), as zsh's
# nesting of each bare "{" asks for one more.
for label, cmd, want, gwwant, b32want in (
    ("${ x60000, unclosed", "echo " + "${ " * 60000 + "\nls", "too large", "too large", "too large"),
    ("${| x60000, unclosed", "echo " + "${|" * 60000 + "\nls", "too large", "too large", "too large"),
    ("${ x30000, closed", "echo " + "${ " * 30000 + "; }" * 30000 + "; ls", "too large", "too large", "too large"),
    ("\"${ x40000, unclosed", 'echo "' + "${ \"" * 40000 + "\nls", "too large", "too large", "too large"),
    ("${x:-${ x30000", "echo " + "${x:-${ " * 30000 + "; }}" * 30000 + "; ls", "too large", "too large", "too large"),
    ("nested ${x:-{${ x30", "echo " + "${x:-{${ :; " * 30 + "}}" * 30 + "; ls", "too large", "too large", "too large"),
    ("$( x60000 in ${ ...", "echo ${ " + "$( " * 60000 + "; }", "too large", "too large", "too large"),
    ("nested \"${x:-'$(: \"${ x30", 'echo "' + "${x:-'$(: \"${ :; " * 30 + "x; })'}\"" * 30 + "; ls", "pass", "too large", "too large"),
    ("{ x60000 in ${ ...", "echo ${ " + "{ " * 60000 + "; }", "pass", "pass", "too large"),
    ("f() x30000 in ${ ...", "echo ${ " + "f() " * 30000 + "; }", "pass", "pass", "pass"),
    ("fi x40000 in ${ ...", "echo ${ " + "fi " * 40000 + "}", "pass", "pass", "pass"),
    ("a } x40000 in ${ ...", "echo ${ " + "a } " * 40000 + "; }", "pass", "pass", "too large"),
    ("a } x20000 in ${ ...", "echo ${ " + "a } " * 20000 + "; }", "pass", "pass", "pass"),
    ("${ a; } x20000", "echo " + "${ a; }" * 20000 + "; ls", "pass", "pass", "pass"),
    ("${ a; } x20000, then pip", "echo " + "${ a; }" * 20000 + "; pip list", "`pip`", "`pip`", "`pip`"),
    ("${ cat <<'E'\\n}\\nE\\n} x8000", "echo " + "${ cat <<'E'\n}\nE\n}\n" * 8000 + "ls", "pass", "pass", "too large"),
    ("${ cat <<'E'\\n}\\nE\\n} x4000", "echo " + "${ cat <<'E'\n}\nE\n}\n" * 4000 + "ls", "pass", "pass", "pass"),
):
    r, cpu, wall = budgeted(reason, cmd, wall=6, **B53)
    check(f"bash 5.3+: {label} ({len(cmd) // 1000}KB): {want}, {cpu:.2f}s CPU, {wall:.2f}s",
          (r == "" if want == "pass" else want in r) and cpu < CPU and wall < 6, r[:80])
    r, cpu, wall = budgeted(gwreason, command=cmd, wall=6)
    check(f"gateway job: {label} ({len(cmd) // 1000}KB): {gwwant}, {cpu:.2f}s CPU, {wall:.2f}s",
          (r == "" if gwwant == "pass" else gwwant in r) and cpu < CPU and wall < 6, r[:80])
    for setup, fn in (("checkpoint job, /bin/sh bash 3.2", lambda: jreason(command=cmd, **JSH32)),
                      ("prefix, the first bash found bash 3.2", lambda: reason(cmd, SHELL=str(FAKE_PIXI), **PFX, **FB32))):
        r, cpu, wall = budgeted(fn, wall=6)
        check(f"{setup}: {label} ({len(cmd) // 1000}KB): {b32want}, {cpu:.2f}s CPU, {wall:.2f}s",
              (r == "" if b32want == "pass" else b32want in r) and cpu < CPU and wall < 6, r[:80])

print("a checkpoint job may run in dash or zsh too, and a command behind a shell prefix in zsh, to which a ${ ...; } is a "
      "parameter expansion: where they end it elsewhere than bash does, the text is read their way as well")
# (real dash or zsh runs the tool in each; no bash does, see below)
zd_deny = [
    "x=$(: \"${ '}'\" )\npip list\n'}'", "echo $(: \"${ '{}\"\\}; )\npip list", "echo \"$(: \"${ '{}\"\\}; )\"\npip list",
    "echo $(: \"${ '{}\"\\};  )\npip list\nE\n}", "( : \"${ x:-'}'\" )\npip list\n'\n}", "( : \"${| '}'\" ) ; pip list ; '}'",
]
zd_pass = [
    # the same, nothing to refuse
    "x=$(: \"${ '}'\" )\nls\n'}'", "echo $(: \"${ '{}\"\\}; )\nls", "( : \"${ x:-'}'\" )\nls\n'\n}",
    # where they end it as bash does
    "x=$(: \"${ echo a; }\" )\nls", "echo \"${ echo '}'; }\"", "echo ${ a; } ; ls", "echo \"${ cat <<'EOF'\n{ }\nok\nEOF\n}\"",
]
for label, fn in (("checkpoint job, /bin/sh not bash", lambda c: jdecision(command=c, **JSH)),
                  ("checkpoint job, /bin/sh bash 3.2", lambda c: jdecision(command=c, **JSH32)),
                  ("prefix, SHELL zsh", lambda c: decision(c, SHELL=ZSH, **PFX)),
                  ("prefix, SHELL not bash or zsh, only a zsh found",
                   lambda c: decision(c, SHELL=str(FAKE_PIXI), **PFX, PIXI_GUARD_SHELL_DIRS=DZ, PATH=NOSHELLS["PATH"]))):
    for cmd in zd_deny:
        got = fn(cmd)
        check(f"deny  {label}: {cmd!r}", got == "deny", got)
    for cmd in zd_pass:
        got = fn(cmd)
        check(f"pass  {label}: {cmd!r}", got == "pass", got)
# Only bash runs them: no zsh or dash reading.
for label, fn in (("bash 5.3 as Claude Code's shell", lambda c: decision(c, **B53)), ("gateway job", lambda c: gwdecision(command=c)),
                  ("terminal", lambda c: sdecision(c + "\n"))):
    for cmd in zd_deny:
        got = fn(cmd)
        check(f"pass  {label}: {cmd!r}", got == "pass", got)
check("deny  checkpoint job, /bin/sh not bash: the refusal names the program", jreason(command=zd_deny[0], **JSH).startswith(
    "pixi-guard: refused, `pip` would run"), jreason(command=zd_deny[0], **JSH))
real_zd = [p for p in (shutil.which("dash"), "/bin/zsh", shutil.which("zsh")) if p and os.path.exists(p)]
for cmd in zd_deny if real_zd else ():
    ran = [p for p in real_zd if runs_tool(p, cmd)]
    check(f"real dash or zsh runs the tool in {cmd!r}", ran, real_zd)
for cmd in zd_pass:
    for p in real_zd:
        check(f"real {p} runs nothing in {cmd!r}", not runs_tool(p, cmd))
for p in REAL_BASH:
    for cmd in zd_deny:
        check(f"real bash {'.'.join(map(str, bash_version(p)))} ({p}) runs nothing in {cmd!r}", not runs_tool(p, cmd))
# (the escaped words above: every shell runs the tool after the ")", and nothing in the near misses)
for p in REAL_BASH + real_zd:
    for cmd in esc_deny:
        check(f"real {p} runs the tool in {cmd!r}", runs_tool(p, cmd))
    for cmd in esc_pass:
        check(f"real {p} runs nothing in {cmd!r}", not runs_tool(p, cmd))

# aliases: an interactive bash expands them, so the value is code (alias X=python, then X)
for text, want in (("alias p=python\np x.py\n", "deny"), ("alias p=python; p x.py\n", "deny"), ("alias p='pip install'\n", "deny"),
                   ("alias ll='ls -l'\nll\n", "pass"), ("alias p='pixi run python'\np x.py\n", "pass"), ("alias\n", "pass")):
    got = sdecision(text)
    check(f"{want:5} terminal text {text!r}", got == want, got)

print("commands that run their arguments: parallel, hyperfine, numactl, entr, watchexec, nodemon, unbuffer, MCP inspector")
expect("deny", [
    "parallel python run.py ::: 1 2 3", "parallel -j4 'pip install {}' ::: a b", "parallel --jobs 4 python {} ::: a.py",
    "parallel -j 4 --bar python {} ::: a.py", "ls *.py | parallel python", "parallel ::: 'python a.py' ls",
    "parallel :::+ ls 'pip list'", "echo 'pip list' | parallel", "parallel {} ::: 'pip list'", "parallel 'cd x && {}' ::: 'pytest -q'",
    "parallel -q python -c 'print(1)' ::: a", "sem -j 2 python x.py", "parallel -j3 -- 'pip list' ls", "parallel python -- a b",
    "hyperfine 'python a.py'", "hyperfine -w 3 'ls' 'pip list'", "hyperfine --setup 'pip install x' 'ls'",
    "hyperfine -P n 1 3 'python a.py {n}'", "hyperfine -L v a,b ls --prepare='pip list'", "hyperfine --shell python3 'print(1)'",
    "hyperfine --shell=python3 'print(1)'", "hyperfine -Spython3 'print(1)'",
    "hyperfine -N -- 'python x.py'", "hyperfine -s'pip list' ls",
    "numactl --cpunodebind=0 python x", "numactl -N 0 -m 0 python x", "numactl --interleave all python x", "numactl -C0-3 pytest",
    "nodemon --exec python app.py", "nodemon -x 'python app.py'", "nodemon --exec='pip list'", "nodemon app.py", "nodemon -w src app.py",
    "watchexec -e py pytest", "watchexec -e py -- python app.py", "watchexec -r 'pip install -e . && pytest'",
    "watchexec -w src -c -- pytest", "ls *.py | entr pytest", "ls | entr -c python x", "ls | entr -s 'pytest -q'",
    "unbuffer python x", "unbuffer -p python x", "npx @modelcontextprotocol/inspector python server.py",
    "npx -y @modelcontextprotocol/inspector -e K=V -- python server.py", "npx @modelcontextprotocol/inspector@0.16 uv run server.py",
    "bunx @modelcontextprotocol/inspector --config c.json --server s python s.py", "npx -c 'pip list'",
])
expect("pass", [
    "parallel echo ::: 'pip list'", "parallel -q echo 'a; pip list' ::: x", "parallel -j4 gzip ::: *.log", "ls | parallel -X rm", "parallel --tag echo {} :::: pip.txt",
    "parallel pixi run python {} ::: a.py", "hyperfine 'ls' 'pixi run python a.py'", "hyperfine --export-json out.json make",
    "hyperfine -n pip ls", "hyperfine -P n 1 3 'sleep {n}'", "numactl --hardware", "numactl -N 0 make", "nodemon server.js",
    "nodemon --exec 'pixi run python app.py'", "nodemon -w app.py server.js", "watchexec -e py -- pixi run pytest",
    "watchexec -e py make", "ls | entr make", "ls | entr -s 'make test'", "unbuffer make", "npx prettier --write .",
    "npx @modelcontextprotocol/inspector pixi run python server.py", "npx @modelcontextprotocol/inspector node build/index.js",
    "npx -y create-react-app python-app",
])

print("npm exec and npx: their command is a command (a package's bin, or with -p any program on PATH)")
expect("deny", [
    "npm exec -- python x.py", "npm exec -c 'pip list'", "npm exec --call='python x'", "npm x -- pytest -q",
    "npm exec --package=foo -- python x.py", "npm exec -p foo -- pip install x", "npm exec -w app -- python x",
    "npx -- python x.py", "npx python x.py", "npx -p foo -y -- uv run x.py", "bunx -- python x.py",
    "npm exec -- sh -c 'python x'", "npm exec -- @modelcontextprotocol/inspector python s.py",
])
expect("pass", [
    "npm exec -- prettier --write .", "npm exec -- pixi run python x.py", "npm exec --package=foo -- echo python",
    "npm exec -w python -- tsc", "npm install python-shell", "npm run python", "npm exec", "npx -- tsc -p .",
    "npx -p pip-licenses-js -- licenses", "npm x -- eslint .",
])
# npm's options before exec; pnpm and yarn exec / dlx, bun x: as npx
expect("deny", [
    "npm --yes exec -- python x", "npm -w app exec -- python x", "npm --prefix . exec -- python x", "npm -y x -- pytest -q",
    "npm --workspace=app exec -- python x", "npm exec --prefix . -- python x", "npm --loglevel silent exec -- uv run x.py",
    "pnpm exec python x.py", "pnpm dlx python x", "pnpm -C app exec pytest", "pnpm --filter app exec python x.py",
    "pnpm exec -c 'pip list'", "pnpm exec --shell-mode 'pytest -q'", "pnpm dlx --package foo python x.py",
    "yarn exec python x.py", "yarn dlx python x", "yarn --cwd app exec pytest", "yarn dlx -p foo python x.py",
    "bun x python x", "bun x -- pytest -q", "bun --bun x python x.py",
])
expect("pass", [
    "pnpm exec tsc", "pnpm dlx create-vite app", "pnpm install python-shell", "pnpm run python", "pnpm -C app exec tsc",
    "yarn exec tsc", "yarn dlx create-vite app", "yarn add python-shell", "yarn python", "yarn --cwd app exec eslint .",
    "bun x eslint .", "bun run python", "bun install pip", "npm --prefix exec install", "npm -w app run python",
    "pnpm exec -- pixi run python x.py", "bun x pixi run pytest",
    # after --, an option is the command (npm reads no options there): npx -- -c is not npx's -c
    "npx -- -c 'pip list'", "npm exec -- -c 'pip list'",
])

print("pixi run / pixi exec of a package installer: refused (it changes the environment behind pixi.lock's back)")
expect("deny", [
    "pixi run pip install requests", "pixi run python -m pip install requests", "pixi run uv pip install foo", "pixi run uv add foo",
    "pixi run pip3 install -e .", "pixi run python3.12 -m pip install -U x", "pixi run python -mpip install x",
    "pixi run python -u -X dev -m pip install x", "pixi run pip --quiet install x", "pixi run uv --quiet add x",
    "pixi run -e dev pip install x", "pixi run --environment dev -- pip install x", "pixi run -m /x/pixi.toml pip install x",
    "pixi run --manifest-path=/x/pixi.toml --frozen pip install x", "pixi r pip install x", "pixi -v run pip install x",
    "pixi --color never run pip install x", "pixi run 'pip install x'", "pixi run \"python -m pip install x && echo ok\"",
    "pixi exec pip install x", "pixi x pip install x", "pixi exec --spec uv uv pip install x", "pixi exec -s pip -c conda-forge pip install x",
    "/opt/local/bin/pixi run pip install x", "~/.pixi/bin/pixi run uv add x", "cd a && pixi run pip install x",
    "bash -c 'pixi run uv add x'", "sudo pixi run pip install x", "timeout 60 pixi run pip install x",
    "pixi run pypy3 -m pip install x",
])
expect("pass", [
    "pixi run pip list", "pixi run pip show requests", "pixi run pip freeze", "pixi run pip download x", "pixi run pip-audit",
    "pixi run python -m pip list", "pixi run python -m pipx install x", "pixi run python -c 'import pip'", "pixi run python -m pip",
    "pixi run uv pip list", "pixi run uv lock", "pixi run uv --version", "pixi run pytest", "pixi run echo pip install x",
    "pixi run -e pip python x.py", "pixi run --environment install pip list", "pixi run -x install", "pixi run install",
    "pixi exec --spec pip pip list", "pixi exec -s install python x.py", "pixi add --pypi requests", "pixi add pip",
    "pixi install", "pixi global install pip", "pixi run 'pip list && pytest'", "pixi run python x.py install",
])
# What pixi runs is read by the scanner in pixi mode: one word is a shell line to pixi (pixi runs
# `pixi run 'a && b'` as both, and `pixi run echo 'a; b'` as echo with one argument), wrappers and options
# are skipped as anywhere, and only an installer is refused.
expect("deny", [
    # a shell line
    "pixi run 'cd sub && pip install x'", "pixi run 'true; pip install x'", "pixi run 'FOO=1 pip install x'",
    "pixi run 'ls | xargs pip install'", "pixi run 'echo hi\npip install x'", "pixi exec 'cd a && uv add x'",
    "pixi run \"$(echo ok) && python -m pip install x\"", "pixi run 'if true; then uv pip install x; fi'",
    # wrappers
    "pixi run env PIP_X=1 pip install x", "pixi run -- env PIP_NO_CACHE=1 pip install x", "pixi run time pip install x",
    "pixi run nice pip install x", "pixi run nice -n 5 python -m pip install x", "pixi run timeout 60 uv add x",
    "pixi run command pip install x", "pixi run exec pip install x", "pixi run xargs pip install < reqs.txt",
    # options that take a value, before the verb
    "pixi run pip --cache-dir /c install x", "pixi run pip --log /tmp/l install x", "pixi run pip --log=/tmp/l install x",
    "pixi run pip --python /x/python install x", "pixi run python -m pip --cache-dir /c install x",
    "pixi run uv --directory . pip install x", "pixi run uv --project . add x", "pixi run uv --color never pip install x",
    "pixi run uv --unknown-opt v add x", "pixi run pip --unknown-opt v install x",
    # python's combined flags
    "pixi run python -Im pip install x", "pixi run python -Bm pip install x", "pixi run python -Impip install x",
    "pixi run python -W ignore -m pip install x", "pixi run python -Wignore -m pip install x",
    "pixi run python -m pip.__main__ install x", "pixi run python -m uv pip install x", "pixi run python -m uv add x",
    # brace expansion
    "pixi run {pip,} install x", "pixi run {pip,pip3} install x", "pixi run {,python} -m pip install x",
    # shells and more under pixi: what they run is under pixi too
    "pixi run bash -c 'pip install x'", "pixi run sh -c 'cd a; uv add x'", "pixi exec -s pip bash -c 'pip install x'",
    "pixi run bash <<'EOF'\npip install x\nEOF", "echo 'pip install x' | pixi run sh", "pixi run bash <<< 'uv add x'",
    "cat <<'EOF' | pixi run bash\nuv pip install x\nEOF", "pixi run bash -c \"$(cat <<'EOF'\npip install x\nEOF\n)\"",
    "pixi run find . -name '*.whl' -exec pip install {} +", "pixi run npx pip install x", "pixi run pixi run pip install x",
    "pixi run eval 'pip install x'", "pixi run watchexec -- pip install -e .",
    # redirections around the command
    "pixi run > log pip install x", "pixi run 'pip install x' > log 2>&1",
])
expect("pass", [
    "pixi run pytest", "pixi run python -m pytest", "pixi run python -m pip list", "pixi run pip list", "pixi run uv --version",
    "pixi run 'cd sub && pytest -q'", "pixi run 'python -m pytest && python -m mypy .'", "pixi run 'FOO=1 python x.py'",
    "pixi run env FOO=1 python x.py", "pixi run time python train.py", "pixi run nice -n 5 pytest", "pixi run python -c 'import pip'",
    "pixi run python -Im pytest", "pixi run python -W ignore x.py install", "pixi run python -X dev -m pip show x",
    "pixi run pip --log install.log list", "pixi run pip show install",
    # (an option's value that is a command's name: a log file named install, a project folder named add)
    "pixi run pip --log install list", "pixi run pip --cache-dir=install list", "pixi run uv --project add run x.py", "pixi run uv run add.py", "pixi run uv tool install ruff",
    "pixi run uv pip compile r.in -o install.txt", "pixi run {pytest,} -q", "pixi run echo 'a; pip install x'",
    "pixi run echo pip install x", "pixi run bash -c 'python x.py'", "pixi run bash -c 'pytest -q && mkdocs build'",
    "pixi run bash <<'EOF'\npython x.py\nEOF", "echo 'python x.py' | pixi run sh", "cat <<'EOF' | pixi run bash\npytest\nEOF",
    "pixi run bash -c \"$(cat <<'EOF'\npython x.py\nEOF\n)\"", "pixi run nodemon app.py", "pixi run npx python x.py",
    "pixi run sudo -u bob python x.py", "pixi run 'source .venv/bin/activate'", "pixi run alias p=python",
    "pixi run 'echo $(python -V)'", "pixi exec --spec rich -- python -m rich", "pixi run -- -m pip install x",
])
expect("deny", [
    # the outer shell still runs what is outside pixi, and its $(...) before pixi does
    "pixi run echo $(python x.py)", "pixi run pytest; python x.py", "pixi run bash -c 'ls' && pip install x",
    "pixi run pytest | python x.py", "echo 'python x.py' | pixi run cat | sh",
])
# echo / printf words piped into a shell under pixi: an installer takes its verb, so what they print counts
# (echo pip install x prints one line; printf's format with its arguments put in), not only each word
expect("deny", [
    "echo pip install x | pixi run bash", "echo uv add x | pixi run bash", "echo python -m pip install x | pixi run bash",
    "printf '%s ' pip install x | pixi run sh", "echo pip \"install x\" | pixi run bash", "pixi run bash -c \"$(echo pip install x)\"",
    "printf 'pip %s x\\n' install | pixi run sh", "printf '%s %s %s\\n' pip install x | pixi run sh",
    "printf -- '%-5s %s\\n' pip install | pixi run sh", "echo -n uv pip install x | pixi exec bash",
    "printf 'uv %s\\n' add x | pixi run bash", "pixi run echo pip install x | pixi run bash",
])
expect("pass", [
    "echo pip list | pixi run bash", "echo python x.py | pixi run bash", "printf '%s\\n' pip install x | pixi run sh",
    "printf '%s %s\\n' pip list | pixi run sh", "printf 'pip list %s\\n' install | pixi run sh", "printf '%%s %s\\n' install | pixi run sh",
    "printf -v v '%s ' pip install x | pixi run sh", "echo pip install x | pixi run grep pip", "echo pytest -q | pixi run bash",
])
# (and outside pixi: printf 'py%s' thon prints python)
expect("deny", ["printf 'py%s\\n' thon | bash", "printf '%s%s -V\\n' pyth on3 | sh", "printf '%c%s\\n' pip ython3 | sh"])
# %c prints its argument's first character, and a * width takes an argument of its own (no word alone is python)
expect("deny", ["printf '%c%s\\n' pz ython3 | sh", "printf '%*s%s -V\\n' 1 py thon3 | sh"])
expect("pass", ["printf '%s ' ls -la | bash", "printf '%s\\n' ls pwd | sh", "printf 'py%s\\n' thon", "printf '%c%s\\n' xip ython3 | sh"])
# What pixi run prints goes into the command's pipeline: fed to a shell outside pixi it is code there, as after
# env, nice, ...; a shell under pixi that pipes into a shell has both under pixi.
expect("deny", [
    "pixi run echo python x.py | bash", "pixi run printf 'pip install x' | sh", "pixi run echo python x.py > >(bash)",
    "pixi run tee >(bash) <<<'python x'", "pixi run -e dev echo pip list | sh", "pixi run env echo python x | bash",
    "pixi run pixi run echo python x | bash", "echo python x.py | pixi run tee >(bash)", "pixi exec echo pytest | sh",
    "tee >(bash) <<<'python x'", "env tee >(bash) <<<'python x'", "cat <<<'python x' > >(bash)", "bash <<<'python x' > >(bash)",
])
expect("pass", [
    "pixi run echo python x.py | pixi run bash", "pixi run echo ls | bash", "pixi run echo python x.py | grep x",
    "pixi run echo python x.py > >(cat)", "pixi run tee >(cat) <<<'python x'", "pixi run tee >(pixi run bash) <<<'python x'",
    "pixi run bash -c \"echo 'python x.py' | bash\"", "pixi run bash -c 'cat <<EOF | bash\npython x.py\nEOF'",
    "pixi run bash -c 'echo python x.py | sh'", "pixi exec bash -c \"$(echo python x.py)\"", "bash <<<'ls' > >(bash)",
    # a >(bash) in code that runs under pixi is under pixi too (the mode of the code it is in, not the outer shell's)
    "pixi run bash -c 'echo python x > >(bash)'", "pixi run bash -c \"tee >(bash) <<<'python x'\"",
    "pixi run bash -c 'cat <<<\"python x\" > >(bash)'",
])
# A system package manager under pixi run still installs outside pixi's environment: refused as without it.
expect("deny", ["pixi run brew install python", "pixi run apt-get install -y python3-pip", "pixi exec brew install uv"])
expect("pass", ["pixi run brew install jq", "pixi run apt-get install -y pipewire", "pixi run brew info python"])
# What runs under pixi sets no PATH and changes no folder for the rest of the command.
expect("pass", ["pixi run 'cd /tmp && ls'; ./x.py", "pixi run cd /tmp; ./x.py"], "pixi #! line in the cwd: ", cwd=pixi_x)
expect("deny", ["cd /tmp; ./x.py"], "pixi #! line in the cwd: ", cwd=pixi_x)
check("pixi run pip install: PIXI_GUARD=ask still asks", decision("PIXI_GUARD=ask pixi run pip install x") == "ask")
r = reason("PIXI_GUARD=ask pixi run uv add x")
check("pixi run pip install, ask: says it installs behind pixi's back", "`pixi run uv add`" in r and "behind pixi's back" in r
      and "outside pixi" not in r, r)
pxws = TMP / "pxws"  # a pixi workspace
pxws.mkdir()
(pxws / "pixi.toml").write_text("[workspace]\n")
r = reason("pixi run pip install -e .", cwd=pxws)
check("pixi run pip install: the reason says why and gives pixi add (and the editable project)",
      "`pixi run pip install` changes the pixi environment behind pixi.lock's back" in r and "`pixi add <pkg>`" in r
      and "pixi add --pypi --editable" in r and "would run Python outside pixi" not in r and "run it as `pixi run" not in r, r)
check("pixi run pip install: the override is for running it this way, not outside pixi",
      "explicitly wants it run this way" in r and "PIXI_GUARD=ask <command>" in r, r)
r = reason("pixi exec pip install rich", cwd=pxws)
check("pixi exec pip install: the reason names the cached environment and pixi exec --spec",
      "cached `pixi exec` environment" in r and "`pixi exec --spec <pkg> ...`" in r, r)
r = reason("pixi run python -m pip install x", pixi=False, sid="pxm1")
check("pixi run pip install without pixi found: no 'pixi not found' advice (it names pixi)", "not found" not in r
      and "behind pixi.lock's back" in r, r)
r = reason("pixi exec -s pip bash -c 'pip install x'", cwd=pxws)
check("pixi exec bash -c 'pip install x': named as pixi exec's", "`pixi exec pip install`" in r and "cached `pixi exec` environment" in r, r)
r = reason("pixi run 'cd sub && uv --project . add x'", cwd=pxws)
check("pixi run shell line with uv add: the pixi add advice", "`pixi run uv add`" in r and "`pixi add <pkg>`" in r, r)

print("Python console scripts by name")
expect("deny", ["coverage run -m pytest", "coverage3 report", "coverage-3.12 html", "pip-audit", "pip-licenses", "pip-3.12 list",
                "pip-compile", "uvicorn app:app", "flask run", "gunicorn app:app", "streamlit run a.py", "alembic upgrade head",
                "django-admin startproject x", "celery -A x worker", "torchrun --nproc_per_node 2 t.py", "accelerate launch t.py",
                "deepspeed t.py", "sphinx-build docs out", "mkdocs serve", "jupytext --to py nb.ipynb"])
# pre-commit and nbstripout: usually global git-hook tools (pixi global install pre-commit is common)
expect("pass", ["pre-commit run --all-files", "nbstripout x.ipynb", "pipe-viewer", "echo coverage pip-audit", "flaskr", "mkdocs-x",
                # shell scripts named pip-*: not pip's tools
                "bin/pip-check-deps.sh", "./scripts/pip-audit-report.sh", "bash scripts/pip-sync.sh", "pip-check-deps.sh"])
expect("deny", ["pip-sync requirements.txt", "pip-3 list", "bin/pip-audit", "pip-audit.py"])

print("an active virtual or conda env: a bare name that is a Python script in it")
venv = TMP / "venv"
(venv / "bin").mkdir(parents=True)
for name, text in (("mytool", f"#!{venv}/bin/python\nimport sys\n"),
                   ("longtool", "#!/bin/sh\n'''exec' \"/very/long/path/bin/python3\" \"$0\" \"$@\"\n' '''\nimport x\n"),
                   ("envpy", "#!/usr/bin/env python3\n"), ("shtool", "#!/bin/sh\necho hi\n"),
                   ("ruff", "\x7fELF\x02\x01\x01\n\x00\x00binary"), ("activate", "# This file must be used with source\n")):
    (venv / "bin" / name).write_text(text)
    (venv / "bin" / name).chmod(0o755)
VENV = {"VIRTUAL_ENV": str(venv)}
expect("deny", ["mytool serve", "sudo mytool", "ls | xargs mytool", "longtool x", "envpy", "sh -c 'cd /tmp && mytool'",
                "{mytool,x} y", "${T:-mytool} run", "ls; mytool"], "venv: ", **VENV)
expect("pass", ["ruff check .", "shtool", "ls", "./mytool", "echo mytool", "activate", "mytoolx", "pixi run mytool"], "venv: ", **VENV)
r = reason("mytool serve", **VENV)
check("venv: the reason names the env", f"is a Python script in {venv}/bin" in r, r)
check("no active env: mytool passes", decision("mytool serve") == "pass")
check("venv: PIXI_GUARD=ask asks", decision("PIXI_GUARD=ask mytool", **VENV) == "ask")
conda = TMP / "miniforge3"
(conda / "bin").mkdir(parents=True)
(conda / "bin" / "condatool").write_text(f"#!{conda}/bin/python3.12\n")
(conda / "bin" / "curl").write_text("\xcf\xfa\xed\xfe binary")
pixienv = TMP / "pixiproj2" / ".pixi" / "envs" / "default"
(pixienv / "bin").mkdir(parents=True)
(pixienv / "bin" / "condatool").write_text(f"#!{pixienv}/bin/python3.12\n")
expect("deny", ["condatool x"], "conda env: ", CONDA_PREFIX=str(conda))
expect("pass", ["curl -fsSL x", "mytool"], "conda env: ", CONDA_PREFIX=str(conda))
expect("pass", ["condatool x"], "pixi env (pixi shell): ", CONDA_PREFIX=str(pixienv))
# a detached pixi env (pixi's detached-environments) lives outside .pixi/envs; conda-meta/pixi marks it
detached = TMP / "pixicache" / "envs" / "proj-1234" / "envs" / "default"
(detached / "bin").mkdir(parents=True)
(detached / "conda-meta").mkdir()
(detached / "conda-meta" / "pixi").write_text("{}\n")
(detached / "bin" / "condatool").write_text(f"#!{detached}/bin/python3.12\n")
expect("pass", ["condatool x"], "detached pixi env (pixi shell): ", CONDA_PREFIX=str(detached))
expect("pass", ["mytool x"], "VIRTUAL_ENV without a bin folder: ", VIRTUAL_ENV=str(TMP / "no-such-venv"))

print("tools from pixi global and pixi environments: let through where they resolve (installers still refused)")


def console_scripts(d, names, py):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text(f"#!{py}\nimport sys\n")
        (d / n).chmod(0o755)


gh = TMP / "pghome"  # a HOME with pixi global tools in ~/.pixi/bin
gbin = gh / ".pixi" / "bin"
console_scripts(gbin, ["mkdocs", "black", "ipython", "jupyter", "pytest", "coverage", "jupytext", "uv", "python3", "pip",
                       "conda"] + [f"t{k}" for k in range(1, 18)], gh / ".pixi" / "envs" / "x" / "bin" / "python3")
console_scripts(gh / ".pixi" / "envs" / "black" / "bin", ["black"], gh / ".pixi" / "envs" / "black" / "bin" / "python3")
other = TMP / "otherbin"  # pip install --user / brew copies of the same tools
console_scripts(other, ["mkdocs", "pytest"], "/usr/bin/python3")
pxproj = TMP / "pxproj"  # a pixi workspace with its default env
pxenv = pxproj / ".pixi" / "envs" / "default"
console_scripts(pxenv / "bin", ["pytest", "python", "mkdocs", "pip"], pxenv / "bin" / "python")
(pxenv / "conda-meta").mkdir()
(pxenv / "conda-meta" / "pixi").write_text("{}\n")
(pxproj / "pixi.toml").write_text("[workspace]\n")
phome = TMP / "pxhome2"  # a custom PIXI_HOME (pixi's default on some setups: ~/.local/share/pixi)
console_scripts(phome / "bin", ["mkdocs", "black"], phome / "envs" / "mkdocs" / "bin" / "python3")
console_scripts(phome / "envs" / "mkdocs" / "bin", ["mkdocs"], phome / "envs" / "mkdocs" / "bin" / "python3")
detached = TMP / "cache" / "envs" / "proj-1a2b"  # detached-environments: a pixi env outside the workspace
console_scripts(detached / "bin", ["pytest"], detached / "bin" / "python")
(detached / "conda-meta").mkdir()
(detached / "conda-meta" / "pixi").write_text("{}\n")
notpixi = TMP / "cache2" / "envs" / "proj"  # the same, without pixi's conda-meta/pixi: some other env
console_scripts(notpixi / "bin", ["pytest"], notpixi / "bin" / "python")
G = {"HOME": str(gh), "PATH": f"{gbin}{os.pathsep}{BASE_PATH}"}
expect("pass", ["mkdocs build", "mkdocs serve", "black --check .", "ipython -c 'print(1)'", "jupyter lab", "pytest -q",
                "coverage report", "jupytext --to py nb.ipynb", "python3 -V", "mkdocs build && black --check .",
                "\"pytest\" -q", "${T:-pytest} -q", "~/.pixi/bin/mkdocs build", f"{gbin}/black --check .",
                "$HOME/.pixi/bin/black .", "\"$HOME/.pixi/bin/black\" .", "${HOME}/.pixi/bin/mkdocs build",
                "~/.pixi/envs/black/bin/black .", "PIXI_GUARD=ask mkdocs build", "nice -n 5 black .", "env FOO=1 black .",
                "git commit -m \"$(cat <<'EOF'\nfix\nEOF\n)\" && pytest -q",
                # xcrun that runs no program (prints a path) leaves the PATH of the rest alone
                "xcrun --show-sdk-path && pytest -q", "xcrun -f clang; mkdocs build"], "pixi global: ", **G)
expect("pass", [" && ".join(f"t{k}" for k in range(1, 17))], "pixi global, 16 names: ", PIXI_GUARD_TOOLS="t[0-9]+", **G)
expect("deny", [
    # installers and env managers: pixi add / pixi install instead, even from pixi global
    "uv --version", "pip install x", "conda list", "~/.pixi/bin/uv sync", f"{gbin}/pip list", "$HOME/.pixi/bin/pip list",
    # not in pixi global (nowhere, or elsewhere first on PATH)
    "python -V", "pytest -q && flask run", "/usr/local/bin/mkdocs build", "~/.pixi/bin/sub/mkdocs", "~/.pixi/mkdocs",
    "~/.pixi/bin/../../x/pytest", "$HOMEX/.pixi/bin/black .", "${X}pytest -q", "{pytest,x} -q",
    # PATH set in the command: where the name resolves is not known here
    "PATH=/usr/local/bin:$PATH mkdocs build", "export PATH=/opt/x:$PATH; pytest -q", "env PATH=/usr/local/bin pytest",
    "mkdocs build; PATH+=:/x", "sh -c 'PATH=/x pytest'", "typeset -x PATH=/x; pytest",
    # sudo, su and env -i run it with another PATH; xcrun looks in Xcode first (xcrun python3 is Xcode's)
    "sudo -u bob black .", "sudo mkdocs build", "doas pytest", "env -i pytest", "env - pytest", "env -u PATH pytest",
    "env --unset=PATH pytest", "su -c 'pytest' bob", "runuser -u bob -- pytest", "xcrun python3 -V"], "pixi global: ", **G)
expect("deny", [" && ".join(f"t{k}" for k in range(1, 18))], "pixi global, more than 16 names: ",
       PIXI_GUARD_TOOLS="t[0-9]+", **G)
expect("pass", ["PYTHONPATH=src pytest -q", "MANPATH=x mkdocs build", "git commit -m 'set PATH=x' && black ."],
       "pixi global, not a PATH assignment: ", **G)
expect("deny", ["mkdocs build", "pytest -q"], "a non-pixi copy first on PATH: ",
       HOME=str(gh), PATH=os.pathsep.join([str(other), str(gbin), BASE_PATH]))
expect("pass", ["mkdocs build", "pytest -q"], "pixi global first on PATH: ",
       HOME=str(gh), PATH=os.pathsep.join([str(gbin), str(other), BASE_PATH]))
# Claude Code started with a short PATH (the desktop app): its Bash may still find ~/.pixi/bin
expect("pass", ["mkdocs build", "black ."], "pixi global, not on the hook's PATH: ", HOME=str(gh))
expect("deny", ["flask run", "pip list"], "pixi global, not on the hook's PATH: ", HOME=str(gh))
PH = {"PIXI_HOME": str(phome)}
expect("pass", ["mkdocs build", "$PIXI_HOME/bin/mkdocs build", "\"$PIXI_HOME/bin/black\" .", "${PIXI_HOME}/bin/mkdocs build",
                f"{phome}/bin/mkdocs build", f"{phome}/envs/mkdocs/bin/mkdocs build", "${PIXI_HOME:-$HOME/.pixi}/bin/mkdocs build"],
       "$PIXI_HOME: ", **PH)
expect("deny", ["pytest -q", f"{phome}/lib/mkdocs", f"{phome}/bin/pip list"], "$PIXI_HOME: ", **PH)
# (with no PIXI_HOME, $PIXI_HOME/bin/mkdocs is /bin/mkdocs)
expect("deny", ["$PIXI_HOME/bin/mkdocs build", "${PIXI_HOME}/bin/black ."], "PIXI_HOME not set: ")
PX = {"cwd": pxproj}
expect("pass", [".pixi/envs/default/bin/pytest -q", ".pixi/envs/default/bin/python -c 'print(1)'", ".pixi/envs/venv/bin/httpx x",
                "./.pixi/envs/default/bin/mkdocs build", f"{pxenv}/bin/pytest -q", "cd /tmp && " + f"{pxenv}/bin/pytest -q"],
       "a pixi env by its path: ", **PX)
expect("pass", ["\"$PWD/.pixi/envs/default/bin/pytest\" -q", "$(pwd)/.pixi/envs/default/bin/python -V",
                "${PROJ:-.}/.pixi/envs/default/bin/mkdocs build", "cd sub && \"$OLDPWD\"/.pixi/envs/default/bin/pytest"],
       "a pixi env by a path with an expansion before .pixi: ", **PX)
expect("deny", ["\"$PWD/.pixi/envs/../bin/pytest\"", "$PWD/.pixi/envs/$E/bin/pytest", "$PWD/.venv/bin/pytest",
                "$PWD/.pixi/envs/default/bin/pip install x", "$PWD.pixi/envs/default/bin/pytest"],
       "a pixi env by a path with an expansion before .pixi: ", **PX)
expect("deny", [".pixi/envs/default/bin/pip install x", ".pixi/envs/default/bin/uv sync", ".pixi/env/default/bin/pytest",
                ".pixi/envs/../bin/pytest", ".pixi/envs/default/lib/pytest", ".venv/bin/pytest", "pytest -q", "envs/venv/bin/httpx x"],
       "a pixi env by its path: ", **PX)
# Claude Code started inside `pixi shell`
PS = {"cwd": pxproj, "CONDA_PREFIX": str(pxenv), "PIXI_IN_SHELL": "1", "PATH": f"{pxenv / 'bin'}{os.pathsep}{BASE_PATH}"}
expect("pass", ["pytest -q", "python -V", "mkdocs build", "python -m pytest && mkdocs build"], "pixi shell: ", **PS)
expect("deny", ["pip install x", "flask run", "python3 -V"], "pixi shell: ", **PS)
expect("pass", ["pytest -q"], "a detached pixi env (conda-meta/pixi): ", PATH=f"{detached / 'bin'}{os.pathsep}{BASE_PATH}")
# each kind of pixi bin folder, as the name resolves on PATH
expect("pass", ["black ."], "pixi global's env bin on PATH: ",
       PATH=f"{gh / '.pixi' / 'envs' / 'black' / 'bin'}{os.pathsep}{BASE_PATH}")
expect("pass", ["mkdocs build"], "$PIXI_HOME/bin on PATH: ", PIXI_HOME=str(phome), PATH=f"{phome / 'bin'}{os.pathsep}{BASE_PATH}")
expect("pass", ["mkdocs build"], "$PIXI_HOME/envs/NAME/bin on PATH: ",
       PIXI_HOME=str(phome), PATH=f"{phome / 'envs' / 'mkdocs' / 'bin'}{os.pathsep}{BASE_PATH}")
expect("deny", ["mkdocs build"], "a PIXI_HOME-like folder that is not $PIXI_HOME: ",
       PATH=f"{phome / 'envs' / 'mkdocs' / 'bin'}{os.pathsep}{BASE_PATH}")
# ${PIXI_HOME:-WORD}: with no PIXI_HOME, WORD is what runs
expect("pass", ["${PIXI_HOME:-$HOME/.pixi}/bin/mkdocs build"], "PIXI_HOME not set: ", HOME=str(gh))
expect("deny", ["${PIXI_HOME:-/usr}/bin/pytest -q"], "PIXI_HOME not set: ", HOME=str(gh))
expect("deny", ["pytest -q"], "an env without conda-meta/pixi: ", PATH=f"{notpixi / 'bin'}{os.pathsep}{BASE_PATH}")
r = reason("PATH=/x:$PATH mkdocs build", **G)
check("pixi global: the reason says PATH is set and offers the path", "PATH of its own" in r and "~/.pixi/bin/mkdocs" in r, r)
r = reason("xcrun python3 -V", **G)
check("pixi global: under xcrun the reason names it", "PATH of its own" in r and "xcrun" in r, r)
check("pixi global: PIXI_GUARD=ask still asks for one elsewhere",
      decision("PIXI_GUARD=ask mkdocs build", HOME=str(gh), PATH=os.pathsep.join([str(other), BASE_PATH])) == "ask")
check("a non-pixi copy first on PATH: the refusal names it",
      hit("mkdocs build", HOME=str(gh), PATH=os.pathsep.join([str(other), str(gbin), BASE_PATH])) == "mkdocs")

print("the gateway's jobs and terminals, and checkpoint's jobs: a bare name is looked up on the hook's PATH (Claude Code's, which they get too)")
# The gateway gives its jobs and terminals Claude Code's PATH (its stand-ins first), and a checkpoint job it runs
# checkpoint's environment.
NOTPX = {"HOME": str(gh), "PATH": os.pathsep.join([str(other), str(gbin), BASE_PATH])}  # a non-pixi copy first
for cmd in ("python3 -V", "pytest -q", "mkdocs build", "env FOO=1 black ."):
    check(f"pass  Bash {cmd!r}, pixi global first on the hook's PATH", decision(cmd, **G) == "pass")
    for label, fn in (("gateway job", lambda c, **kw: gwdecision(command=c, **kw)), ("gateway terminal", lambda c, **kw: sdecision(c + "\n", **kw)),
                      ("checkpoint job", lambda c, **kw: jdecision(command=c, **kw))):
        got = fn(cmd, **G)
        check(f"pass  {label} {cmd!r}, pixi global first on the hook's PATH", got == "pass", got)
        if cmd in ("pytest -q", "mkdocs build"):  # (other's tools)
            got = fn(cmd, **NOTPX)
            check(f"deny  {label} {cmd!r}, a non-pixi copy first on the hook's PATH", got == "deny", got)
for label, kw, want in (
    ("argv, a bare name", dict(argv=["pytest", "-q"]), "pass"),
    ("argv, pixi global's by its path", dict(argv=[f"{gbin}/pytest", "-q"]), "pass"),
    ("~/.pixi/bin by its path", dict(command="~/.pixi/bin/python3 -V"), "pass"),
    ("$HOME/.pixi/bin by its path", dict(command="$HOME/.pixi/bin/mkdocs build"), "pass"),
    ("pixi run", dict(command="pixi run pytest -q"), "pass"),
    ("pixi exec", dict(command="pixi exec black ."), "pass"),
    ("on another host (not checked)", dict(command="pytest -q", host="gpu1"), "pass"),
    ("a workspace env's by its path", dict(command=".pixi/envs/default/bin/pytest -q", cwd=pxproj), "pass"),
    # PATH set in the job: where the name resolves is not known, as in Bash
    ("PATH set in it", dict(command="PATH=/opt/x:$PATH pytest -q"), "deny"),
    ("sudo", dict(argv=["sudo", "black", "."]), "deny"),
):
    got = gwdecision(**kw, **G)
    check(f"{want:5} gateway job, pixi global first on the hook's PATH: {label}", got == want, got)
# A tool only in pixi global, not on the hook's PATH: Bash may find it there (its shell snapshot's PATH), but a job or
# terminal gets no snapshot, and the gateway's imported folders (~/.local/bin, where pipx puts tools, say) may hold
# another copy, so it is refused there.
for cmd in ("mkdocs build", "black ."):
    check(f"pass  Bash {cmd!r}, pixi global not on the hook's PATH", decision(cmd, HOME=str(gh)) == "pass")
    for label, fn in (("gateway job", lambda c, **kw: gwdecision(command=c, **kw)), ("gateway terminal", lambda c, **kw: sdecision(c + "\n", **kw)),
                      ("checkpoint job", lambda c, **kw: jdecision(command=c, **kw))):
        got = fn(cmd, HOME=str(gh))
        check(f"deny  {label} {cmd!r}, pixi global not on the hook's PATH", got == "deny", got)
        got = fn(cmd, **PH)
        check(f"deny  {label} {cmd!r}, $PIXI_HOME/bin not on the hook's PATH", got == "deny", got)
        got = fn(f"{gbin}/{cmd}", HOME=str(gh))
        check(f"pass  {label} {cmd!r}, pixi global's by its path", got == "pass", got)
r = gwreason(command="mkdocs build", HOME=str(gh))
check("gateway job, pixi global not on the hook's PATH: the reason offers its path",
      f"A job or terminal gets Claude Code's PATH, which doesn't have {gbin}: run pixi global's `mkdocs` there by its path (`{gbin}/mkdocs`)" in r, r)
r = why(gwsend("black .\n", **PH)[0])
check("gateway terminal, $PIXI_HOME/bin not on the hook's PATH: the reason offers its path", f"(`{phome}/bin/black`)" in r, r)
r = gwreason(command="mkdocs build", **NOTPX)
check("gateway job, a non-pixi copy on the hook's PATH: no such note", "Claude Code's PATH" not in r, r)
check("pass  gateway job, inside pixi shell (its env first on the hook's PATH)", gwdecision(command="pytest -q", **PS) == "pass")
check("deny  gateway job, inside pixi shell: a tool the env lacks", gwdecision(command="flask run", **PS) == "deny")
check("deny  gateway terminal, PATH set in it", sdecision("export PATH=/opt/x:$PATH; mkdocs build\n", **G) == "deny")
r = gwreason(command="pytest -q", cwd=pxproj, **NOTPX)
check("gateway job, a non-pixi copy first: no PATH note, offers pixi run",
      "`pytest` would run Python outside pixi" in r and "PATH" not in r and "make the job's command `pixi run pytest ...`" in r, r)
r = why(gwsend("pytest -q\n", cwd=pxproj, **NOTPX)[0])
check("gateway terminal, a non-pixi copy first: no PATH note", "PATH" not in r and "run it as `pixi run pytest ...`" in r, r)
r = gwreason(command="PATH=/opt/x:$PATH pytest -q", cwd=pxproj, **G)
check("gateway job that sets PATH: the reason says so and offers the path, as in Bash",
      "This command runs it with a PATH of its own" in r and "~/.pixi/bin/pytest" in r and "gateway runs" not in r, r)
check("Bash: no gateway PATH note", "gateway" not in reason("PATH=/x:$PATH mkdocs build", **G))
# (what runs under pixi run sets no PATH for the rest of the command)
expect("pass", ["pixi run env PATH=/x ls; mkdocs build", "pixi run 'PATH=/x ls' && black ."], "pixi global: ", **G)
expect("deny", ["env PATH=/x ls; mkdocs build"], "pixi global: ", **G)

print("a *.py script run by its path: let through if its #! line runs pixi")
sb = TMP / "shebang"
(sb / "tools").mkdir(parents=True)
for name, first in (("report.py", "#!/usr/bin/env -S pixi exec --spec rich -- python"),
                    ("direct.py", "#!/opt/homebrew/bin/pixi run python"), ("envvar.py", "#!/usr/bin/env -S X=1 pixi exec python"),
                    ("plain.py", "#!/usr/bin/env python3"), ("nobang.py", "import sys"), ("pixiname.py", "#!/usr/bin/env pixi-python")):
    (sb / "tools" / name).write_text(first + "\nprint(1)\n")
    (sb / "tools" / name).chmod(0o755)
(sb / "tools" / "dir.py").mkdir()
# a file named like a glob: bash runs what the glob matches (odd1.py), not the file of that name
(sb / "tools" / "odd[1].py").write_text("#!/usr/bin/env -S pixi exec python\n")
(sb / "tools" / "odd1.py").write_text("#!/usr/bin/env python3\n")
SB = {"cwd": sb}
expect("pass", ["./tools/report.py", "tools/report.py --flag", "./tools/direct.py", "./tools/envvar.py", "sudo ./tools/report.py",
                f"{sb}/tools/report.py", "~/shebang/tools/report.py", "./tools/report.py && ls", "\"./tools/report.py\" x",
                "find . -name x -exec ./tools/report.py {} \\;", "ls | xargs ./tools/report.py", "nice ./tools/report.py",
                "./tools/report.py; ./tools/direct.py", " ".join(["./" * k + "tools/report.py;" for k in range(1, 17)])],
       "pixi #!: ", **SB)
expect("deny", ["./tools/plain.py", "./tools/nobang.py", "./tools/missing.py", "./tools/pixiname.py", "./tools/dir.py",
                "./tools/report.py && python x", "./tools/report.py; ./tools/plain.py", "cd tools && ./report.py",
                # (./tools/report.py is let through from this cwd: after a cd it may be another file)
                "cd /tmp; ./tools/report.py", "(cd sub && ./tools/report.py)", "env -C /tmp ./tools/report.py",
                "env --chdir=/tmp ./tools/report.py", "sudo -D /tmp ./tools/report.py", "pushd /tmp; ./tools/report.py",
                "git rebase -x ./tools/report.py main", "report.py",
                "$D/tools/report.py", "${D}./tools/report.py", "./tools/rep*.py", "./tools/odd[1].py", "./tools/{report,x}.py",
                "find . -execdir ./tools/report.py \\;",
                " ".join(["./" * k + "tools/report.py;" for k in range(1, 18)])],
       "pixi #!: ", **SB)
expect("pass", [f"cd /tmp && {sb}/tools/report.py"], "pixi #!: absolute path after cd: ", **SB)
expect("deny", ["./tools/report.py"], "pixi #!, another cwd: ")
r = reason("./tools/plain.py", **SB)
check("pixi #!: the reason shows the #! line that is let through", "plain.py" in r and "#!/usr/bin/env -S pixi exec" in r, r)
r = reason("cd tools && ./report.py", **SB)
check("pixi #!: after cd, the reason says a relative path is not looked up", "after `cd`" in r and "absolute path" in r, r)

print("a *.py script the same command writes: refused (its #! line is read before the command runs)")
PIXI_BANG = "#!/usr/bin/env -S pixi exec --spec rich -- python"
written = [
    f"cat > tools/new.py <<'EOF'\n{PIXI_BANG}\nprint(1)\nEOF\nchmod +x tools/new.py && ./tools/new.py",
    # (the reverse: a pixi #! script overwritten with a python3 one)
    "cat > tools/report.py <<'EOF'\n#!/usr/bin/env python3\nprint(1)\nEOF\n./tools/report.py",
    f"sed -i '' '1s|.*|{PIXI_BANG}|' tools/plain.py && ./tools/plain.py",
    "sed -i.bak 1d tools/report.py; ./tools/report.py", "sed --in-place -e 1d tools/report.py; tools/report.py",
    "perl -pi -e 's/a/b/' tools/report.py && ./tools/report.py", "cp tools/plain.py tools/report.py && ./tools/report.py",
    "mv tools/plain.py tools/report.py && ./tools/report.py", "cp /tmp/report.py tools/ && ./tools/report.py",
    "cp -t tools /tmp/report.py && ./tools/report.py", "install -m 755 /tmp/r.py tools/report.py && ./tools/report.py",
    "ln -sf plain.py tools/report.py && ./tools/report.py",
    "printf '#!/usr/bin/env python3\\n' > tools/report.py; ./tools/report.py", "echo x >> tools/report.py && ./tools/report.py",
    "tee tools/report.py < /dev/null && ./tools/report.py", "{ echo '#!/usr/bin/env python3'; } > tools/report.py; ./tools/report.py",
    "curl -fsSL https://example.com/r.py -o tools/report.py && ./tools/report.py",
    "curl -fsSLo tools/report.py https://example.com/r.py && ./tools/report.py",
    "wget -qO tools/report.py https://example.com/r.py && ./tools/report.py",
    "cat > \"$PWD/tools/report.py\" <<'EOF'\nx\nEOF\n./tools/report.py",
    f"cat > {sb}/tools/report.py <<'EOF'\nx\nEOF\n./tools/report.py", f"echo >> tools/report.py; {sb}/tools/report.py",
    "cat > ./tools/./report.py <<'EOF'\nx\nEOF\ntools/report.py", "cat > \"$F\".py <<'EOF'\nx\nEOF\n./tools/report.py",
    "./tools/report.py && cat > tools/report.py < /dev/null",
]
expect("deny", written, "pixi #!, written: ", **SB)
expect("pass", [
    "chmod +x tools/report.py && ./tools/report.py", "cat tools/report.py && ./tools/report.py", "./tools/report.py > out.txt",
    "./tools/report.py > tools/out.json", "cat > tools/other.py <<'EOF'\nx\nEOF\n./tools/report.py",
    "cp tools/report.py /tmp/backup.py && ./tools/report.py", "cp tools/report.py backup/ && ./tools/direct.py",
    "sed -n 1p tools/report.py && ./tools/report.py", "tee log.txt < /dev/null && ./tools/report.py",
    "git add tools/report.py && ./tools/report.py", "curl -o data.json https://example.com && ./tools/report.py",
    "./tools/report.py 2>&1 | tee report.log", "cp -i a.txt b.txt && ./tools/report.py", "echo x > /tmp/report.py.txt; ./tools/report.py",
], "pixi #!, not written: ", **SB)
r = reason(written[0], **SB)
check("pixi #!, written: the reason says why and to write the file first, without inviting the same command again",
      "This same command writes `new.py`" in r and "Write the file first" in r and "may be run by its path" not in r, r)

print("work budgets (a command too big or nested to check is refused, never a timeout)")
complex_cmds = {
    "2000 sh -c strings then one more": "sh -c 'true'; " * 2000 + "sh -c 'python x'",
    "eval chain": "eval " * 3000 + "python x",
    "env -S chain": "env -S nice " * 2000 + "python x",
    "deep \"$( nesting": "echo " + "\"$( echo " * 20000 + "python x" + " )\"" * 20000,
    "deep find -exec nesting": "find . -exec " * 20000 + "ls",
    "unterminated \"$( x50000": "echo " + "\"$( " * 50000 + "\npython x",
}
for label, cmd in complex_cmds.items():
    (out, err), cpu, _ = budgeted(pre, cmd)
    d = out and out["hookSpecificOutput"]
    check(f"{label} ({len(cmd) // 1000}KB): refused in {cpu:.2f}s CPU", d and d["permissionDecision"] == "deny" and cpu < CPU, (d, err))
check("2000 sh -c strings then python: python found", decision("sh -c 'true'; " * 2000 + "sh -c 'python x'") == "deny")
r = reason("eval " * 3000 + "ls")
check("too-complex reason says so and offers the override", "too large" in r and "PIXI_GUARD=ask" in r, r)
out, _ = pre("PIXI_GUARD=ask " + "eval " * 3000 + "ls")
d = (out or {}).get("hookSpecificOutput", {})
check("PIXI_GUARD=ask on a too-complex command asks, saying why",
      d.get("permissionDecision") == "ask" and "too large" in d.get("permissionDecisionReason", ""), d)
check("deep but checkable nesting is still scanned", hit("echo " + "$(echo " * 200 + "$(python x)" + ")" * 200) == "python")
# Over 256KB: refused unread. Below it, parsing has a budget too: views, nested
# shell strings, re-reads after array errors and brace expansion all count.
views = "{x; echo $[a b]; : $(: ${x:-(}); "
budget_cmds = {
    "over 256KB, unread": "echo " + "a" * 270000,
    "bash -c string read in every view": "bash -c '" + views + "{ echo }; " * 25000 + "'",
    "echo of 1-character words into sh": "echo " + "a " * 125000 + "| sh",
    "bash <(echo of 1-character words)": "bash <(echo " + "a " * 125000 + ")",
    "array errors on every line": "bash -c '" + "a=(;)\n" * 10000 + "'",
    # (it would print 50000 copies of a 100KB format)
    "printf of a long format, used again for each argument, into sh": "printf '" + "a" * 100000 + "%s\\n' " + "x " * 50000 + "| sh",
    "brace expansion in every command": "{a,b}{c,d}{e,f}{g,h} x; " * 9000,
}
for label, cmd in budget_cmds.items():
    r, cpu, _ = budgeted(reason, cmd)
    check(f"{label} ({len(cmd) // 1000}KB): too complex, {cpu:.2f}s CPU", "too large" in r and cpu < CPU, r)
out, _ = pre("PIXI_GUARD=ask echo " + "a" * 270000)
check("PIXI_GUARD=ask on a command over 256KB asks", (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "ask", out)
check("a 250KB command is still read", decision("echo " + "a" * 250000 + "; ls") == "pass"
      and decision("echo " + "a" * 250000 + "; pip list") == "deny")

print("look-aheads stop as soon as a budget is spent (nested ${...$(...)} read in two views took exponential time)")
for label, cmd in (
    ("nested ${x:-{$(: x30", "echo " + "${x:-{$(: " * 30 + ")}" * 30 + "; ls"),
    ("nested \"${x:-'$(: x30", 'echo "' + "${x:-'$(: \"" * 30 + "x)'}\"" * 30 + "; ls"),
    ("nested ${x:-{$(: x3000", "echo " + "${x:-{$(: " * 3000 + ")}" * 3000 + "; ls"),
    ("nested $(( x30000", "echo $(( " + "$(( " * 30000 + "1" + " ))" * 30001),
    ("nested \"$(( x30000", 'echo "$(( ' + "$(( " * 30000 + "1" + " ))" * 30001 + '"'),
    ("unclosed $(( repeated", "echo $(( ;" * 24000 + "\nls"),
    ("unclosed $(( repeated in $(...)", ": $(echo " + "$(( ;" * 30000 + ")\nls"),
    ("(( repeated in $(...), one ) at its end", "echo $(echo " + "((" * 60000 + ")\nls"),
):
    # (and well before the 7 s watchdog, which would say "too large" too)
    r, cpu, wall = budgeted(reason, cmd, wall=6)
    check(f"{label} ({len(cmd) // 1000}KB): too complex, {cpu:.2f}s CPU, {wall:.2f}s", "too large" in r and cpu < CPU and wall < 6, r[:80])


print("raw control characters stay linear (each \\x01 was a new record, re-copying the text)")
for label, cmd, want in (
    ("300K \\u0001 then python3", "\x01" * 300000 + "\npython3 x", "too large"),
    # (150K: JSON writes each as \u0001, and a payload over 1MB is not read)
    ("150K \\u0001 then python3", "\x01" * 150000 + "\npython3 x", "`python3`"),
    ("1MB of \\u0001\\u0002", "\x01\x02" * 500000 + "\npython3 x", "too large"),
):
    r, cpu, _ = budgeted(reason, cmd)
    check(f"{label} ({len(cmd) // 1000}KB): refused, {cpu:.2f}s CPU", want in r and cpu < CPU, r[:80])
raw = '{"session_id":"s1","cwd":"%s","tool_input":{"command":"%s\\npython3 x"}}' % (PLAIN, "\x01" * 300000)
(out, _), cpu, _ = budgeted(run, "pre", None, raw=raw)
check(f"300K raw \\x01 bytes in the JSON: refused, {cpu:.2f}s CPU",
      (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny" and cpu < CPU, out)
out, _ = pre("echo " + "\x01" * 120000 + "a" * 150000 + "; PIXI_GUARD=ask python3 x")
check("over 256KB: PIXI_GUARD=ask after a \\x01 record still asks",
      (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "ask", out)
# A payload over 1MB is not read (see pre; 1.8MB here, a 300KB command): refused as too large to read, and a
# PIXI_GUARD=ask anywhere in it asks (as anywhere in a command over 256KB).
for label, cmd, want in (("PIXI_GUARD=ask first", "PIXI_GUARD=ask echo " + "\x01" * 300000 + "; python3 x", "ask"),
                         ("PIXI_GUARD=ask last", "echo " + "\x01" * 300000 + "; PIXI_GUARD=ask python3 x", "ask"),
                         ("PIXI_GUARD=asking", "echo " + "\x01" * 300000 + "; PIXI_GUARD=asking python3 x", "deny")):
    (out, _), cpu, _ = budgeted(pre, cmd)
    check(f"1.8MB of JSON, {label}: {want}, {cpu:.2f}s CPU",
          (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == want and cpu < CPU, out)
out, _ = pre("echo " + "a" * 300000 + "\x01PIXI_GUARD=ask python3 x")
check("over 256KB: \\x01 right before PIXI_GUARD=ask is not a separator (refused)",
      (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)

print("a program word full of unclosed ${ stays linear (a regex gsub retried each ${ to the end)")
for label, cmd in (
    ("${ x120000", "${" * 120000),
    ("backtick then ${ x120000", "`" + "${" * 120000),
    ("58 backtick bodies of ${ x2048", ("`" + "${" * 2048 + "`") * 58),
    ("${ x120000 then python3", "${" * 120000 + "}; python3 x"),
):
    d, cpu, _ = budgeted(decision, cmd)
    check(f"{label} ({len(cmd) // 1000}KB): {d}, {cpu:.2f}s CPU", d in ("pass", "deny") and cpu < CPU, d)

print("long option words stay linear (substr() per character walked the whole word)")
n = 250000
_, base, _ = timed(decision, "echo " + "i" * n + "; python3 -V")
for cmd in ("env -" + "i" * n + " python3 -V", "sudo -" + "E" * n + " python3 -V", "bash -" + "e" * n + " -c 'python3 -V'",
            "su -" + "m" * n + " -c 'python3 -V'", "xargs -" + "t" * n + " python3 -V",
            "tmux new -" + "d" * n + " 'python3 -V'"):
    d, cpu, _ = budgeted(decision, cmd, cpu=2 * base + 0.3)
    check(f"{cmd[:8]}... ({len(cmd) // 1000}KB): {d}, {cpu:.2f}s CPU (echo of the same size: {base:.2f}s)",
          d == "deny" and cpu < 2 * base + 0.3, d)

print("watchdog: a scan still running after PIXI_GUARD_WATCHDOG seconds is refused (a hook timeout lets it through)")
REAL_AWK = shutil.which("awk")
FAKE_AWKS = {}
for name, scan_does in (
        # the scan (awk -v tools=...) spins, for 30 s at most; other awk calls run as usual. (srand()
        # returns the previous seed, the time of day: in seconds under gawk, BWK awk and busybox, in
        # nanoseconds under mawk 1.3.4 20250131, Debian 13's awk; u scales it to seconds.)
        ("slow", "exec '%s' 'BEGIN { srand(); t = srand(); for (u = 1; t / u >= 1e10; u *= 1000) ; "
                 "for (;;) { srand(); if ((srand() - t) / u > 30) exit } }'" % REAL_AWK),
        ("failing", "exit 2")):
    d = TMP / f"{name}-awk"
    d.mkdir()
    (d / "awk").write_text(f"#!/bin/sh\ncase $2 in tools=*) {scan_does} ;; esac\nexec '{REAL_AWK}' \"$@\"\n")
    (d / "awk").chmod(0o755)
    FAKE_AWKS[name] = f"{d}{os.pathsep}{BASE_PATH}"


def pre_pg(cmd, **kw):
    """pre() in a process group of its own: decision, reason, wall time, CPU time (the hook's) and what
    it left running (or on stderr)."""
    payload = json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Bash", "tool_input": {"command": cmd}})
    r0, t0 = resource.getrusage(resource.RUSAGE_CHILDREN), time.time()
    p = subprocess.Popen(["sh", str(GUARD), "pre"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env(True, **kw), start_new_session=True)
    try:
        out, err = p.communicate(payload, timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        out, err = p.communicate()
    r1, wall = resource.getrusage(resource.RUSAGE_CHILDREN), time.time() - t0
    cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
    ps = subprocess.run(["ps", "-A", "-o", "pgid=,pid=,args="], capture_output=True, text=True).stdout
    left = [l.strip()[:80] for l in ps.splitlines() if l.split()[:1] == [str(p.pid)]]
    if left:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
    d = json.loads(out)["hookSpecificOutput"] if out.strip() else {}
    return (d.get("permissionDecision", "pass"), d.get("permissionDecisionReason", ""), wall, cpu,
            left + ([err] if err else []))


def pre_pg_runs(cmd, wall, **kw):
    """remeasured() runs of pre_pg() under an upper bound of wall seconds, and their smaller wall time."""
    rs = remeasured(pre_pg, cmd, wall=wall, **kw)
    return rs, min(t[2] for t in rs)


def pg_detail(rs):
    """What a failed check shows of pre_pg() runs."""
    return [(d, r[:60], f"{w:.1f}s", left) for d, r, w, _, left in rs]


# Upper wall-time bounds below may take a second run (remeasured()): a scan the watchdog doesn't stop
# runs its 30 s again, one that waits for its sleeper 7 s again. Lower bounds use the first run alone.
for cmd, want in (("ls", "deny"), ("python3 x", "deny"), ("PIXI_GUARD=ask ls", "ask")):
    rs, wall = pre_pg_runs(cmd, 4, PATH=FAKE_AWKS["slow"], PIXI_GUARD_WATCHDOG="1")
    _, _, wall1, cpu, _ = rs[0]
    # (the killed awk is waited for, so its CPU time counts: the budget tests above rely on that)
    check(f"scan stuck, watchdog 1 s: {want} {cmd!r} as too large in {wall:.1f}s ({cpu:.1f}s CPU), nothing left running",
          all(d == want and "too large" in r and not left for d, r, _, _, left in rs)
          and 0.9 < wall1 and cpu > 0.5 and wall < 4, pg_detail(rs))
rs, wall = pre_pg_runs("python3 x", 4, PATH=FAKE_AWKS["failing"])
check(f"scan's awk failing: fails open in {wall:.2f}s, nothing left running",
      all(d == "pass" and not left for d, _, _, _, left in rs) and wall < 4, pg_detail(rs))
for cmd, want in (("ls", "pass"), ("python3 x", "deny"), ("PIXI_GUARD=ask pip list", "ask")):
    rs, wall = pre_pg_runs(cmd, 4)
    check(f"scan done in time: {want} {cmd!r} in {wall:.2f}s, its sleeper killed (nothing left running)",
          all(d == want and "too large" not in r and not left for d, r, _, _, left in rs) and wall < 4, pg_detail(rs))
if not os.environ.get("PIXI_GUARD_VARIANT"):  # (7 s each: once, not for every variant)
    for wd in (None, "30"):
        rs, wall = pre_pg_runs("ls", 8.5, PATH=FAKE_AWKS["slow"], PIXI_GUARD_WATCHDOG=wd)
        check(f"scan stuck, PIXI_GUARD_WATCHDOG={wd}: refused after the default 7 s ({wall:.1f}s, under the hook's 10 s)",
              all(d == "deny" and "too large" in r and not left for d, r, _, _, left in rs)
              and 6.9 < rs[0][2] and wall < 8.5, pg_detail(rs))

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
check("venv console script suggests pixi run", "pixi run uvicorn" in reason(".venv/bin/uvicorn app", cwd=pixi_proj))

pyproj_pixi = TMP / "pyproj-pixi"
pyproj_pixi.mkdir()
(pyproj_pixi / "pyproject.toml").write_text("[project]\nname='a'\n\n[tool.pixi.workspace]\n")
check("pyproject with [tool.pixi] is pixi", "pixi workspace" in reason("python x", cwd=pyproj_pixi))

py_proj = TMP / "pyproj"
(py_proj / "pkg").mkdir(parents=True)
(py_proj / "pyproject.toml").write_text("[project]\nname='a'\n")
r = reason("python x", cwd=py_proj / "pkg")
check("non-pixi python project", "not on pixi" in r and "pixi-guard:project" in r and str(py_proj) in r, r)
wrap = r.split("put the wrapper at ")[1].split(" ")[0].rstrip(".")
check("wrapper path is under the wrap dir", wrap.startswith(str(TMP / "wrap")) and wrap.endswith("/pixi.toml"), wrap)
check("wrapper dir keeps its historic name (name_-cksum)", re.search(r"/pyproj_-\d+/pixi\.toml$", wrap), wrap)
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
r = reason("python x")
check("plain folder: a script runs with pixi run --script (pixi exec has no --script)",
      "`pixi init --script file.py`" in r and "`pixi run --script file.py`" in r, r)

print("project detection")
# A git checkout wrapped out of folder, with docs/requirements.txt: the same wrapper from anywhere in it.
repo = TMP / "repo"
(repo / "docs").mkdir(parents=True)
(repo / ".git").mkdir()
(repo / "pyproject.toml").write_text("[project]\nname='r'\n")
(repo / "docs" / "requirements.txt").write_text("sphinx\n")
r = reason("python x", cwd=repo / "docs")
check("in git, the root is the outermost marker", f"{repo} is a Python project" in r, r)
check("wrap-path from docs/ names the repo root's wrapper", wrap_path(repo / "docs") == wrap_path(repo))
m = Path(wrap_path(repo))
m.parent.mkdir(parents=True)
m.write_text("[workspace]\n")
check("wrapped repo is seen from docs/", f"{repo} is wrapped by" in reason("python x", cwd=repo / "docs"))
check("wrapped repo is seen from its root", f"{repo} is wrapped by" in reason("python x", cwd=repo))
# A wrapper at a nearer marker folder (made before a marker further up appeared) is still found.
old = TMP / "oldrepo"
(old / "sub").mkdir(parents=True)
(old / ".git").mkdir()
(old / "pyproject.toml").write_text("[project]\nname='o'\n")
(old / "sub" / "requirements.txt").write_text("x\n")
sub_manifest = TMP / "wrap" / f"sub_-{subprocess.run(['cksum'], input=str(old / 'sub'), capture_output=True, text=True).stdout.split()[0]}" / "pixi.toml"
sub_manifest.parent.mkdir(parents=True)
sub_manifest.write_text("[workspace]\n")
r = reason("python x", cwd=old / "sub")
check("a wrapper at a nearer marker level is found", str(sub_manifest) in r and "is wrapped by" in r, r)
# Markers above the git top level do not count.
outer = TMP / "outer"
(outer / "plainrepo").mkdir(parents=True)
(outer / "pyproject.toml").write_text("[project]\nname='x'\n")
(outer / "plainrepo" / ".git").write_text("gitdir: /elsewhere\n")  # a worktree's .git is a file
check("markers above the git top level are ignored", "No pixi workspace here" in reason("python x", cwd=outer / "plainrepo"))
# Outside git, the nearest marker is the root.
nogit = TMP / "nogit" / "a" / "b"
nogit.mkdir(parents=True)
(TMP / "nogit" / "a" / "pyproject.toml").write_text("[project]\nname='a'\n")
(nogit / "requirements.txt").write_text("x\n")
check("outside git, the nearest marker is the root", f"{nogit} is a Python project" in reason("python x", cwd=nogit))
# $HOME itself is never a project, even through a symlinked HOME.
home = TMP / "home"
(home / "work" / "plain").mkdir(parents=True)
(home / ".python-version").write_text("3.12\n")
homelink = TMP / "homelink"
homelink.symlink_to(home)
for h in (home, homelink):
    r = reason("python x", cwd=home / "work" / "plain", HOME=str(h))
    check(f"HOME with .python-version is not a project (HOME={h.name})", "No pixi workspace here" in r, r)
(home / "pixi.toml").write_text("[workspace]\n")
check("a pixi workspace at HOME still counts", "pixi workspace" in reason("python x", cwd=home / "work", HOME=str(home)))
(home / "pixi.toml").unlink()

print("reason JSON escaping")
odd = TMP / 'we"ird\\pa\tth\x01x'
odd.mkdir()
(odd / "setup.py").write_text("")
out, err = pre("python x", cwd=odd)
r = out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "") if out else ""
check("path with quote, backslash, tab and control char round-trips", f"{odd} is a Python project" in r, (r, err))
check("hit name with a backslash round-trips", hit('"./we\\ird.py" a') == "we\\ird.py", hit('"./we\\ird.py" a'))
check("hit name control characters are masked", hit("./a\x01b.py") == "a?b.py", hit("./a\x01b.py"))

print("pixi missing")
r1 = reason("python x", pixi=False, sid="m1")
check("first refusal tells the user", "not found" in r1 and "tell the user" in r1 and "command -v pixi" in r1, r1)
r2 = reason("python x", pixi=False, sid="m1")
check("later refusals are short", "has been told" in r2, r2)
check("other sessions are told again", "tell the user" in reason("python x", pixi=False, sid="m2"))
check("pixi missing: still allows non-python", decision("ls", pixi=False) == "pass")
data = TMP / "plugin-data"
reason("python x", pixi=False, sid="d1", PIXI_GUARD_HOME=None, CLAUDE_PLUGIN_DATA=str(data))
check("told markers go under CLAUDE_PLUGIN_DATA", (data / "told" / "d1").exists())
check("... and nothing under the XDG state dir", not (TMP / ".local" / "state" / "pixi-guard").exists())
# Claude Code gives every plugin hook CLAUDE_PLUGIN_DATA; without it (guard.sh run by hand) no markers are kept.
r1 = reason("python x", pixi=False, sid="x1", PIXI_GUARD_HOME=None)
r2 = reason("python x", pixi=False, sid="x1", PIXI_GUARD_HOME=None)
check("without CLAUDE_PLUGIN_DATA no markers: every refusal tells the user", "tell the user" in r1 and "tell the user" in r2, r2)
check("... and nothing goes under the XDG state dir", not (TMP / ".local" / "state" / "pixi-guard").exists())
stale = data / "told" / "stale"
stale.write_text("")
os.utime(stale, (time.time() - 30 * 86400,) * 2)
reason("python x", pixi=False, sid="d2", PIXI_GUARD_HOME=None, CLAUDE_PLUGIN_DATA=str(data))
check("old told markers are pruned", not stale.exists() and (data / "told" / "d2").exists())
stale.write_text("")
os.utime(stale, (time.time() - 30 * 86400,) * 2)
session(pixi=False, PIXI_GUARD_HOME=None, CLAUDE_PLUGIN_DATA=str(data))
check("old told markers are pruned at a session start too", not stale.exists() and (data / "told" / "d2").exists())
# The XDG state dir 0.1.0 kept its markers in is no longer looked at.
h = TMP / "oldstate"
old_told = h / ".local" / "state" / "pixi-guard" / "told"
old_told.mkdir(parents=True)
(old_told / "ancient").write_text("")
os.utime(old_told / "ancient", (time.time() - 30 * 86400,) * 2)
kw = dict(pixi=False, HOME=str(h), PIXI_GUARD_HOME=None, CLAUDE_PLUGIN_DATA=str(h / "data"))
reason("python x", sid="o1", **kw)
session(**kw)
check("0.1.0's XDG state dir is left alone", (old_told / "ancient").exists(), list(h.rglob("*")))

print("finding pixi")
for label, setup, extra in (
    ("~/.pixi/bin", lambda h: h / ".pixi" / "bin", {}),
    ("$PIXI_HOME/bin", lambda h: h / "custom-pixi-home" / "bin", {"PIXI_HOME": "custom-pixi-home"}),
    ("~/.local/bin", lambda h: h / ".local" / "bin", {}),
):
    h = TMP / ("findhome-" + re.sub(r"\W", "", label))
    b = setup(h)
    b.mkdir(parents=True)
    shutil.copy(FAKE_PIXI, b / "pixi")
    kw = {k: str(h / v) for k, v in extra.items()}
    out, _ = session(pixi=None, HOME=str(h), PATH=NOPIXI_PATH, **kw)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    check(f"pixi in {label} is found off PATH", "systemMessage" not in out and str(b / "pixi") in ctx, out)
    check(f"pixi in {label}: refusals give pixi advice",
          "pixi exec" in reason("python x", pixi=None, HOME=str(h), PATH=NOPIXI_PATH, **kw))
r = subprocess.run(["sh", str(PIXI_SH), "run", "x y"], capture_output=True, text=True, env=env(True))
check("pixi.sh runs pixi with its arguments", r.stdout.strip() == "fake pixi run x y", r)
r = subprocess.run(["sh", str(PIXI_SH), "--version"], capture_output=True, text=True, env=env(False))
check("pixi.sh without pixi: exit 127 and a pointer to pixi.sh", r.returncode == 127 and "https://pixi.sh" in r.stderr, r)

print("session start")
# pixi on PATH too: BASE_PATH drops ~/.pixi/bin, where pixi's installer puts it, and with PIXI_GUARD_PIXI's
# pixi not on PATH the context says it "may not be on PATH" (checked with the "found off PATH" ones above).
out, _ = session(PATH=f"{FAKE_PIXI.parent}{os.pathsep}{BASE_PATH}")
ctx = out["hookSpecificOutput"]["additionalContext"]
check("policy context", "pixi" in ctx and "pixi-guard:project" in ctx and "Monitor" in ctx and "terminal" in ctx)
check("policy context: checkpoint's job_start, with the pixi way for a job's command",
      "job_start" in ctx and "`pixi run python train.py`" in ctx, ctx)
check("policy context: the gateway's job_start and terminal_send, and no pixi run pip install",
      "the gateway's job_start and terminal_send" in ctx and "`pixi run pip install`" in ctx, ctx)
check("no missing notice when pixi exists", "not found" not in ctx and "systemMessage" not in out)
out, _ = session(pixi=False)
check("missing pixi: context says so", "not found" in out["hookSpecificOutput"]["additionalContext"])
check("missing pixi: the user sees a notice naming https://pixi.sh", "https://pixi.sh" in out.get("systemMessage", ""), out)

print("skill")
skill = (ROOT / "skills" / "project" / "SKILL.md").read_text()
check("skill finds the hook via ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh" in skill)
check("skill never restores files with git checkout --", "checkout --" not in skill)
check("skill warns that the exclude rules are shared by every worktree of the clone", "every worktree" in skill and "git add -f" in skill)
snippet = re.search(r"In a git checkout.*?```bash\n(.*?)```", skill, re.S).group(1)
if shutil.which("git"):
    fake = TMP / "skillbin"
    fake.mkdir()
    # Like pixi init: writes pixi.toml and appends to .gitignore / .gitattributes.
    (fake / "pixi").write_text('#!/bin/sh\n[ "$1" = init ] || exit 1\nfor a; do d=$a; done\nmkdir -p "$d"\n'
                               'printf \'[workspace]\\nname = "%s"\\n\' "$(basename "$d")" > "$d/pixi.toml"\n'
                               'echo ".pixi/" >> "$d/.gitignore"\necho "pixi.lock -diff" >> "$d/.gitattributes"\n')
    (fake / "pixi").chmod(0o755)
    genv = dict(env(True), PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", GIT_CONFIG_GLOBAL="/dev/null",
                GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t")

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, env=genv, capture_output=True, text=True)

    up = TMP / "upstream"
    (up / "pkg").mkdir(parents=True)
    git(up, "init", "-q", ".")
    (up / ".gitignore").write_text("mine/\n")
    (up / "pkg" / "pyproject.toml").write_text("[project]\nname='p'\n")
    git(up, "add", ".")
    git(up, "commit", "-qm", "init")
    (up / ".gitignore").write_text("mine/\nuncommitted/\n")  # the user's unsaved edit
    wt = TMP / "upstream-wt"
    git(up, "worktree", "add", "-q", str(wt))
    for where, cwd in (("checkout subfolder", up / "pkg"), ("worktree", wt)):
        before = git(cwd, "status", "--porcelain").stdout
        r = subprocess.run(["sh", "-c", snippet], cwd=cwd, env=genv, capture_output=True, text=True)
        after = git(cwd, "status", "--porcelain").stdout
        check(f"skill wrap snippet works in a {where}", r.returncode == 0 and (cwd / "pixi.toml").exists(), r.stderr)
        check(f"skill wrap snippet: git status unchanged in a {where}", before == after, (before, after))
    check("skill wrap snippet keeps the user's .gitignore edit", "uncommitted/" in (up / ".gitignore").read_text())
    check("skill wrap snippet: pixi.toml named after the project", 'name = "pkg"' in (up / "pkg" / "pixi.toml").read_text())

print("version")
pj = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
ver = pj["version"]
check("plugin.json version is semver", re.fullmatch(r"\d+\.\d+\.\d+", ver), ver)
check("plugin.json links its source (homepage, repository)",
      pj.get("homepage") == pj.get("repository") == "https://github.com/p-w-rs/pixi-guard", pj)
mk = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
entry = [p for p in mk["plugins"] if p["name"] == pj["name"]]
check("marketplace lists the plugin by its name", len(entry) == 1, entry)
check("marketplace entry has no conflicting version", entry and entry[0].get("version", ver) == ver)
md = re.search(r"plugin name `[^`]+`, v(\d+\.\d+\.\d+)", (REPO / "CLAUDE.md").read_text())
check("CLAUDE.md states the plugin.json version", md and md.group(1) == ver, md and md.group(1))
# The plugin is pixi-guard (install id pixi-guard@pixi-guard, skill pixi-guard:project); it was pixi up to 0.2.0.
check("plugin.json name is pixi-guard, and so is the marketplace's", pj["name"] == "pixi-guard" == mk["name"], (pj["name"], mk["name"]))
md = re.search(r"plugin name `([^`]+)`", (REPO / "CLAUDE.md").read_text())
check("CLAUDE.md states the plugin.json name", md and md.group(1) == pj["name"], md and md.group(1))
refs = re.findall(r"skill ([A-Za-z0-9_-]+):project", GUARD.read_text())
check("guard.sh names the skill as <plugin name>:project", len(refs) >= 2 and set(refs) == {pj["name"]}, refs)
check("the skill's folder is skills/project", (ROOT / "skills" / "project" / "SKILL.md").exists())
readme = (REPO / "README.md").read_text()
check("README installs it by its id", f"/plugin install {pj['name']}@{mk['name']}" in readme, pj["name"])
check("README names the skill by its id", f"{pj['name']}:project" in readme and "`pixi:project`" not in readme)
check("no top-level bin/ in the plugin", not (ROOT / "bin").exists())
# Latest only: the dev manifest needs the pixi that made pixi.lock (the newest then; moved with every relock, see
# docs/dev/TESTING.md), with no upper bound, and every dependency is "*".
PIXI_MIN = "0.83.0"
toml_text = (ROOT / "pixi.toml").read_text()
manifest = tomllib.loads(toml_text)
check(f"pixi.toml requires-pixi is >={PIXI_MIN}, no upper bound", manifest["workspace"].get("requires-pixi") == f">={PIXI_MIN}"
      and len(re.findall(r"(?m)^requires-pixi\s*=", toml_text)) == 1, manifest["workspace"].get("requires-pixi"))
check("pixi.toml: every dependency is \"*\"", manifest["dependencies"] and set(manifest["dependencies"].values()) == {"*"},
      manifest["dependencies"])

print("robustness")
out, _ = run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Bash",
                     "tool_input": {"command": "python \"x\\ty\" é \\u00e9"}})
check("escapes in payload still parse to a deny", out and out["hookSpecificOutput"]["permissionDecision"] == "deny")
out, _ = run("pre", None, raw='{"session_id":"s1","tool_input":{"command":"echo \\"a\\\\\\" b\\" \\u0026\\u0026 python x"}}')
check("escaped quotes and \\u escapes decode", out and out["hookSpecificOutput"]["permissionDecision"] == "deny", out)
out, _ = run("pre", None, raw='{"tool_input":{"description":"say \\"command\\": \\"python\\"","command":"ls"}}')
check("a quoted \"command\" inside another value is not the command", out is None, out)
out, _ = run("pre", None, raw='{"tool_input":{"meta":{"command":"ls"},"command":"python x"}}')
check("a \"command\" in a nested object is not the command (tool_input's own is)", verdict(out, "") == "deny", out)
raw = '{"tool_input":{"command":"python x.py"},"later":{"host":"gpu1"},"tool_name":"mcp__plugin_gate_gw__job_start"}'
check("a host in an object after tool_input is not tool_input's (a local gateway job: deny)", verdict(*run("pre", None, raw=raw)) == "deny")
out, _ = run("pre", None, raw='{"tool_name":"Bash","tool_input":{"description":"d"},"later":{"command":"pip install x"}}')
check("a command in an object after tool_input is not tool_input's (none: no output)", out is None, out)
out, _ = run("pre", None, raw='{"tool_input":{"note":"a \\\\","command":"python x"}}')
check("a value ending in an escaped backslash ends there", verdict(out, "") == "deny", out)
out, _ = run("pre", None, raw='{"tool_input":{"note":"a \\\\\\" \\"command\\":\\"python x","command":"ls"}}')
check("an escaped quote after an escaped backslash stays in the string", out is None, out)
# More than 64 backslashes before a quote: counted without a loop over each (see oddtail)
for nb, want in ((100, "pass"), (101, "deny"), (4, "pass"), (3, "deny")):
    note = "x" + "\\" * nb + ('"' if nb % 2 == 0 else "") + ' "command": "python x'
    # (an even count of escaped backslashes and an escaped quote: the fake "command" stays in the string;
    # an odd count of escaped backslashes and no quote: the note ends there, and ' "command"...' is no key)
    ti = {"note": note, "command": "ls"} if nb % 2 == 0 else {"note": "x" + "\\" * nb, "command": "python x"}
    got = verdict(*run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": "Bash", "tool_input": ti}))
    check(f"{want:5} a value with {nb} escaped backslashes{' and an escaped quote' if nb % 2 == 0 else ''} before the command",
          got == want, got)
out, _ = run("pre", None, raw='{"session_id":"s1","tool_input":{"command":"python x"},"tool_name":"mcp__plugin_gate_gw__job_start"}')
check("the tool name after tool_input is still read (top level): a job's refusal", "job's command" in why(out), out)
for label, ti in (("an argv in a nested object", {"meta": {"argv": ["ls"]}, "argv": ["python", "x.py"]}),
                  ("a command in a nested object", {"meta": {"command": "ls"}, "argv": ["python", "x.py"]})):
    got = verdict(*run("pre", {"session_id": "s1", "cwd": str(PLAIN), "tool_name": JOB, "tool_input": ti}))
    check(f"deny  checkpoint job, {label} before its own", got == "deny", got)
r = reason('python "a\\b"', cwd=PLAIN)
check("reason is valid JSON with quotes/backslashes", "pixi-guard" in r)
out, err = run("pre", {"tool_name": "Bash"})
check("no command: no output", out is None and not err)
r = subprocess.run(["sh", str(GUARD), "pre"], input="not json", capture_output=True, text=True, env=env(True))
check("garbage input fails open", r.stdout == "" and r.returncode == 0)
big = "echo " + "a" * 50000 + " && python x"
check("long command still scanned", decision(big) == "deny")
check("many command substitutions are all scanned", hit("echo " + "$(true) " * 6000 + "$(python x)") == "python")

print("speed (~240KB, under the 256KB limit; about 1 s CPU here, quadratic code takes 10 s+; the hook timeout is 10 s)")
body = "Don't use pip (it's slow): run `pip install -e .` (or $(python -m x))\n" * 3300
for label, cmd, want in (
    ("heredoc file body", f"cat > notes.md <<'EOF'\n{body}EOF\npython x", "deny"),
    ("commit message", f"git commit -m \"$(cat <<'EOF'\n{body}EOF\n)\" && python x", "deny"),
    ("commit message, nothing after", f"git commit -m \"$(cat <<'EOF'\n{body}EOF\n)\"", "pass"),
    ("one long quoted word", "echo '" + "a" * 240000 + "' && python x", "deny"),
    ("one long bare word", "echo " + "a" * 240000 + " && python x", "deny"),
    ("many words", "echo " + "ab " * 80000 + "&& python x", "deny"),
    ("many lines", "ls -la\n" * 34000 + "python x", "deny"),
    ("escaped double quotes", 'echo "' + 'a\\"b ' * 48000 + '" && python x', "deny"),
    ("bash -c script", "bash -c '" + "echo hi; " * 26000 + "python x'", "deny"),
    ("$'..' in $(...)", "x=$(printf $'it\\'s'); " * 10800 + "python x", "deny"),
    ("case in $(...)", "x=$(case $t in pip) echo 1;; esac); " * 6500 + "ls", "pass"),
    ("case patterns", "case $x in " + "pip|uv) echo;; " * 15000 + "esac; python x", "deny"),
    ("heredoc pipelines", "cat <<E | " * 5000 + "cat\n" + "x\nE\n" * 5000 + "python x", "deny"),
    ("${...} with quotes", "echo ${x:-\"}\"} " * 16000 + "; python x", "deny"),
    ("backticks with \\$", "echo `echo \\$x` " * 15000, "pass"),
    ("many sh -c strings", "sh -c x; " * 26000 + "sh -c 'python x'", "deny"),
    ("{ } groups", "{ echo a; } " * 20000 + "; python x", "deny"),
    ("arrays", "a=(1 2 3); " * 22000 + "python x", "deny"),
    # heredocs waiting for their bodies in $(...): a stack, not a growing string (that took 8 s+)
    ("heredocs in $(...)", ": $(cat " + "<<a" * 48000 + "\n" + "a\n" * 48000 + ")\npython3 x", "deny"),
    ("$[ without ] repeated", "echo $[ ;" * 26000 + "\npython3 x", "deny"),
    ("${...} read again for zsh", "echo \"${x:-'}'}\" " * 14000 + "; python3 x", "deny"),
    ("(( )) repeated", "(( i++ )); " * 21000 + "python3 x", "deny"),
    ("$(( )) repeated", "echo $(( i + 1 )); " * 12000 + "python3 x", "deny"),
    ("$(( )) with ( ) in it", "x=$(( (a + b) * 2 )); " * 10000 + "ls", "pass"),
    # a glob word of many groups (each "(" looked at the whole word so far: 30 s)
    ("one glob word of @(...) groups", "ls " + "@(a|b)" * 40000 + "; python3 x", "deny"),
    ("one glob word of zsh (...) groups", "ls " + "(a|b)" * 48000 + "; python3 x", "deny"),
    ("one glob word of !(...) groups", "ls " + "!(*.py)" * 34000 + "; ls", "pass"),
    ("glob words of groups", "ls " + "*(.)@(a) " * 26000 + "; python3 x", "deny"),
    # a "((" or "$((" whose "))" never comes is not looked for again after it
    # pixi run / exec: each looked at once, a long first word only to its first KB
    ("pixi run commands", "pixi run -e dev pip list; " * 9000 + "python3 x", "deny"),
    ("one long pixi run word", "pixi run '" + "pip list " * 26000 + "' && python3 x", "deny"),
    ("one long pixi run word without blanks", "pixi run " + "a" * 240000 + " && python3 x", "deny"),
    ("npx commands", "npx a b; " * 25000 + "python3 x", "deny"),
    # pixi run shell lines and their wrappers: each read once more, in pixi mode
    ("pixi run shell lines", "pixi run 'cd a && pip list'; " * 8000 + "python3 x", "deny"),
    ("pixi run wrappers and options", "pixi run env A=1 nice pip --log l list; " * 6000 + "python3 x", "deny"),
    ("pixi run python flags", "pixi run python -Im -Wignore x.py; " * 6500 + "python3 x", "deny"),
    ("pnpm exec commands", "pnpm -C a exec tsc; " * 11000 + "python3 x", "deny"),
    # echo / printf words into a shell: each word, and what echo prints (once more); printf '%s\n' prints the
    # words themselves, one a line (not read again)
    ("echo of 30000 words into sh", "echo " + "ab " * 30000 + "| sh", "pass"),
    ("printf '%s\\n' of 30000 words into sh", "printf '%s\\n' " + "ab " * 30000 + "| sh", "pass"),
    ("printf '%s ' of 30000 words into pixi run sh", "printf '%s ' " + "ab " * 30000 + "| pixi run sh", "pass"),
    ("unbalanced (", "(" * 200000 + "ls", "pass"),
    ("(( with no ))", "(( " * 80000 + "\npython3 x", "deny"),
):
    # CPU time of the hook's processes: steadier under machine load than wall time (shown only).
    r, cpu, wall = budgeted(reason, cmd)
    got = "deny" if r else "pass"
    check(f"{label} ({len(cmd) // 1000}KB): {want}, {cpu:.2f}s CPU, {wall:.2f}s",
          got == want and "too large" not in r and len(cmd) < 262144 and cpu < CPU, (got, r[:80]))
# tool_input's own keys are found by one walk over the JSON (see jindex), however many quotes come before them
big = 'echo "' + 'a\\"b ' * 48000 + '" && python x'
decoy = {"meta": {"x": ['a"b\\'] * 30000, "host": "localhost"}}
for label, kw, want in (("its host after it", dict(host="gpu1"), "pass"), ("no host", {}, "deny"),
                        ("a nested decoy full of quotes first, then its host", dict(host="gpu1", extra=decoy), "pass"),
                        ("a nested decoy full of quotes first, no host", dict(extra=decoy), "deny")):
    (out, err), cpu, wall = budgeted(gwjob, command=big, **kw)
    got = verdict(out, err)
    check(f"gateway job, a {len(big) // 1000}KB command of escaped quotes, {label}: {want}, {cpu:.2f}s CPU, {wall:.2f}s",
          got == want and "too large" not in why(out) and cpu < CPU, (got, why(out)[:80]))
# Over 500,000 quotes, or a long stretch with brackets between two strings, the payload is refused unread
# (before the scan's watchdog starts, the walk must stay short); under that, it is read in well under a second.
for label, ti, want, big in (
    ("480K quotes in a decoy, then a host", {"meta": ['"'] * 160000, "command": "python x", "host": "gpu1"}, "pass", False),
    ("480K quotes in a decoy, no host", {"meta": ['"'] * 160000, "command": "python x"}, "deny", False),
    ("600K quotes in a decoy, then a host", {"meta": ['"'] * 200000, "command": "ls", "host": "gpu1"}, "deny", True),
    ("600K quotes, PIXI_GUARD=ask", {"meta": ['"'] * 200000, "command": "PIXI_GUARD=ask ls"}, "ask", True),
    ("600K quotes, PIXI_GUARD=ask as the first argv element", {"meta": ['"'] * 200000, "argv": ["PIXI_GUARD=ask", "ls"]}, "ask", True),
    ("600K quotes, PIXI_GUARD=asking", {"meta": ['"'] * 200000, "command": "PIXI_GUARD=asking ls"}, "deny", True),
    ("300K escaped backslashes (600K characters) before the command", {"note": "\\" * 300000, "command": "python x"}, "deny", False),
    ("1MB of blanks between two keys", {"command": "python x", "note": "x"}, "deny", False),
    ("5000 nested lists before the command", {"meta": "NEST", "command": "ls"}, "deny", True),
    ("1000 nested lists before the command", {"meta": "NEST4", "command": "ls"}, "pass", False),
):
    data = json.dumps({"session_id": "s1", "cwd": str(PLAIN), "tool_name": GWJOB, "tool_input": ti})
    data = data.replace('"NEST"', "[" * 5000 + "]" * 5000).replace('"NEST4"', "[" * 1000 + "]" * 1000)
    if label.startswith("1MB"):
        data = data.replace('"note"', " " * 1000000 + '"note"')
    (out, err), cpu, wall = budgeted(run, "pre", None, raw=data)
    got, r = verdict(out, err), why(out)
    check(f"gateway job, {label} ({len(data) // 1000}KB): {want}{' (too large to read)' if big else ''}, {cpu:.2f}s CPU",
          got == want and (("too large" in r) == big or want == "pass") and cpu < CPU, (got, r[:100]))

# Under the quote budget's 500,000 quotes in less than 1MB: still refused unread.
data = '{"session_id":"s1","cwd":%s,"tool_name":"%s","tool_input":{"meta":[%s],"command":"ls","host":"gpu1"}}' % (
    json.dumps(str(PLAIN)), GWJOB, ",".join(['""'] * 260000))
(out, err), cpu, wall = budgeted(run, "pre", None, raw=data)
check(f"gateway job, 520K quotes in {len(data) // 1000}KB: deny (too large to read), {cpu:.2f}s CPU",
      verdict(out, err) == "deny" and "too large" in why(out) and cpu < CPU, why(out)[:100])
# The walk's time grows with the payload's size (about 0.9 s per MB at worst under gawk, before the scan's 7 s
# watchdog starts): a payload over 1MB is not read at all, whatever is in it; up to 1MB, the worst shapes are
# read well within the budget. (16MB of bracket-heavy values took 13 s under gawk: a hook timeout.)
BR = '[' * 2000 + '"x"' + ']' * 2000 + ','
for label, ti, want, big in (
    ("1.1MB of description, then an ordinary command", '{"description":"%s","command":"ls"}' % ("a" * 1100000), "deny", True),
    ("1.1MB of description, PIXI_GUARD=ask", '{"description":"%s","command":"PIXI_GUARD=ask ls"}' % ("a" * 1100000), "ask", True),
    ("1.1MB of description, PIXI_GUARD=\"ask\"", '{"description":"%s","command":"PIXI_GUARD=\\"ask\\" ls"}' % ("a" * 1100000), "ask", True),
    ("1.1MB of description, a host of gateway.toml", '{"description":"%s","command":"ls","host":"gpu1"}' % ("a" * 1100000), "deny", True),
    ("16MB of bracket-heavy values", '{"a":[%s0],"command":"ls"}' % (BR * 4000), "deny", True),
    ("1MB of bracket-heavy values, then python", '{"a":[%s0],"command":"python x"}' % (BR * 260), "deny", False),
    ("1MB of escaped keys with [] values, then python", '{%s"command":"python x"}' % "".join('"\\\\%x":[],' % k for k in range(84000)), "deny", False),
    ("1MB of escaped backslashes, then python", '{"note":"%s","command":"python x"}' % ("\\\\" * 520000), "deny", False),
):
    data = '{"session_id":"s1","cwd":%s,"tool_name":"%s","tool_input":%s}' % (json.dumps(str(PLAIN)), GWJOB, ti)
    (out, err), cpu, wall = budgeted(run, "pre", None, raw=data)
    got, r = verdict(out, err), why(out)
    check(f"gateway job, {label} ({len(data) // 1000}KB): {want}{' (too large to read)' if big else ''}, {cpu:.2f}s CPU",
          got == want and ("too large" in r) == big and (big or "`python` would run" in r) and (len(data) > 1048576) == big
          and cpu < CPU, (got, len(data), r[:100]))

print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
