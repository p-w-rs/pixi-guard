# pixi-guard

A Claude Code plugin that keeps Python on [pixi](https://pixi.sh). Claude
runs Python and Python tooling only through pixi (`pixi run`, `pixi exec`,
`pixi add`). Running `python`, `pip`, `uv`, `conda`, `poetry` and the like
directly takes your explicit OK.

Sister projects: [gateway](https://github.com/p-w-rs/gateway),
[checkpoint](https://github.com/p-w-rs/checkpoint).

> Developing on this repo? Start with [CLAUDE.md](CLAUDE.md).

## What it does

- **Refuses Python outside pixi.** A hook checks every command Claude runs
  with the Bash and Monitor tools (and the desktop app's terminal tool, and
  the jobs it starts with [checkpoint](https://github.com/p-w-rs/checkpoint)'s
  `job_start`, see below). If
  any part of it starts `python*`, `pip*`, `uv`/`uvx`, `easy_install`,
  `conda`/`mamba`, `poetry`, `pipenv`, `pyenv`, `virtualenv`, `hatch`,
  `pdm`, `rye`, `pytest`, `coverage`, `ipython`, `jupyter`, `mypy`,
  `black`, `uvicorn`, `gunicorn`, `flask`, `streamlit`, `celery`,
  `alembic`, `mkdocs` and similar, a `*.py` script, a script from a venv or
  conda env (`.venv/bin/uvicorn`), or `source .venv/bin/activate`, or
  installs Python with brew/apt/dnf/..., the command is refused. A tool
  that comes from pixi is not: a name that resolves into `~/.pixi/bin` or
  `$PIXI_HOME/bin` (`pixi global install mkdocs`) or into a pixi
  environment's `bin` (`.pixi/envs/default/bin`, also inside `pixi
  shell`), or a path there. Installers and env managers (`pip*`, `uv`,
  `conda`, `poetry`, ...) stay refused even from there: packages go in with
  `pixi add`. If Claude Code was started in an activated venv or conda env,
  a Python script from its `bin` folder run by its bare name (`httpx`,
  `fastapi`) is refused too. The refusal tells Claude the pixi way to do it
  for the folder it's in. It follows `&&`, pipes, `$(...)`, `sh -c`,
  `eval`, `trap`, `xargs`, `parallel`, `find -exec`, wrappers and their
  options (`sudo`/`env`/`timeout`/`nice`, `setsid`, `flock`, `script`,
  `xvfb-run`, `numactl`, `unbuffer`, `entr`, `direnv exec`, `mise exec`,
  ...),
  commands that run other commands (`hyperfine`, `watchexec`, `nodemon
  --exec`, the MCP inspector `npx @modelcontextprotocol/inspector`), the
  commands `tmux` and `screen` start, heredocs, here-strings and
  `echo`/`printf` text fed to a shell
  (`bash <<EOF`, `cat <<EOF | bash`, `echo 'pip install x' | sh`, `tee
  >(bash)`) and `$(...)` in unquoted heredocs. A default value is checked
  as (part of) the program too (`${PYTHON:-python3} x`,
  `${VIRTUAL_ENV:-.venv}/bin/uvicorn`). Where bash, zsh and dash
  would read a command differently (quotes inside `"${...}"`, `$[...]`,
  `!(...)`), it is checked each way. Words in quotes or quoted heredocs,
  `case` patterns and glob patterns (`ls !(*.py)`) are not commands, so
  `git commit -m "drop pip"` and Claude's usual `git commit -m "$(cat
  <<'EOF' ... EOF)"` go through. But when Claude Code's shell is bash 3.x
  (macOS's `/bin/bash` as your `$SHELL`) or bash 5.2 or later, that
  heredoc is also read as that bash reads it, which can end the `$(...)`
  early and run backticks in the message (see Limits). A command too big
  or too deeply nested to check quickly is refused too (Claude is told to
  split it), and so is one whose check is still running after 7 seconds,
  rather than let through by the hook's time limit.
- **Checkpoint jobs too.** With the checkpoint plugin installed, a job
  Claude starts with its `job_start` tool is checked the same way before it
  starts (nothing changes without checkpoint: the hook only sees that tool
  when it exists). The job's `command` is read as a shell command line;
  an `argv` list is read as the command line checkpoint hands to `/bin/sh`
  (Python's `shlex.join`): each element is one word, so `&&` or `$(pip
  list)` inside an element is plain text, while `["sh", "-c", "python x"]`
  or a leading `OMP_NUM_THREADS=4` is read as the shell reads it. It is read
  as `/bin/sh` reads it (bash 3.2 on macOS), not as Claude Code's shell, and
  a relative `*.py` path (and the project the advice is for) is looked up in
  the job's `cwd`. A refusal tells Claude to make the job's command the
  pixi one (`pixi run python train.py`, with `argv` `["pixi", "run",
  "python", "train.py"]`). `PIXI_GUARD=ask` works there too, in front of
  the command or as the first `argv` element.
- **Scripts with a pixi `#!` line run.** `./tools/report.py` is let
  through when its first line runs pixi, such as `#!/usr/bin/env -S pixi
  exec --spec rich -- python` (a literal path; a relative one only when the
  command doesn't change folders first). Not when the same command writes
  the file (`cat > x.py <<'EOF'`, `sed -i`, `cp`, `mv`, `tee`, `curl -o`):
  its `#!` line is read before the command runs, so Claude is told to write
  the file first and run it in a command of its own.
- **You can still say yes.** If you want something run outside pixi, Claude
  re-runs it as `PIXI_GUARD=ask <command>` (for a checkpoint job, with
  `PIXI_GUARD=ask` in front of the job's command), and you get Claude Code's
  permission prompt for it. Claude is told to use this only when you ask.
- **pixi not installed?** The hook looks on PATH and in the usual install
  folders (`~/.pixi/bin`, `~/.local/bin`, Homebrew, MacPorts), so it also
  works when the desktop app starts Claude Code with a short PATH. If pixi
  isn't there, you see a notice at session start, and Claude tells you the
  first time Python comes up and offers to install it (`brew install pixi`
  or the pixi.sh installer).
- **Project not on pixi?** Claude asks you once: *convert* it (your code:
  adds `pixi.toml` or `[tool.pixi]` and a lock file you commit) or *wrap* it
  (someone else's code: a `pixi.toml` that git never sees, through
  `.git/info/exclude`, or one outside the folder under
  `~/.local/share/pixi-guard/wrap/`). Either way you run things with
  `pixi run ...` afterwards. The steps are in the plugin's skill
  ([`pixi:project`](plugins/pixi-guard/skills/project/SKILL.md)).
- At session start Claude gets a short note about the policy.

## Install

Needs only [Claude Code](https://code.claude.com) (and pixi for running
Python). The hook is plain `sh` and `awk` (macOS, Linux), so it works on a
machine without pixi or Python. In Claude Code:

```
/plugin marketplace add p-w-rs/pixi-guard
/plugin install pixi@pixi-guard
```

or in one line: `/plugin install pixi --marketplace p-w-rs/pixi-guard`.

In the desktop app: **+** next to the prompt box → **Plugins** → **Add
marketplace**, enter `p-w-rs/pixi-guard`, then install `pixi`.

To try it from a checkout without installing:
`claude --plugin-dir plugins/pixi-guard`.

Nothing to configure. Optional environment overrides, if set where Claude
Code starts: `PIXI_GUARD_TOOLS` (the program names to refuse, as an awk
regex alternation such as `python|pip|uv`, replacing the built-in list) and
`PIXI_GUARD_WRAP_DIR` (where out-of-folder wrappers go). The hook also
reads `SHELL` (or `CLAUDE_CODE_SHELL`), to know if Claude Code's shell is
bash 3.x or 5.2+ (and, for a checkpoint job, asks `/bin/sh --version`),
`VIRTUAL_ENV` and `CONDA_PREFIX`, for an active env (not
pixi's own: `pixi shell` sets `CONDA_PREFIX` too), and `PATH`, `HOME` and
`PIXI_HOME`, to tell where a tool comes from.

## Limits

- It checks the commands Claude runs. A script that starts Python itself
  (a Makefile, `npm run`, a shell script) isn't looked into, nor are the
  commands `parallel -a FILE` reads.
- A checkpoint job is checked once, when `job_start` is called. Its tool
  names are looked up on the hook's `PATH` (Claude Code's, which the job
  gets too, less checkpoint's own pixi environment). An `argv` list is read
  up to its first element that is not a string (checkpoint can't run such a
  list), and one too long to read (over 1.6MB of JSON before its `]`) is
  refused as too large.
- Python tools are refused by name (the list above), as `*.py` files, from
  venv and conda paths, and from an active env's `bin` folder. A name is let
  through when the hook's own `PATH` finds it in a pixi `bin` folder, or
  finds it nowhere and `~/.pixi/bin` or `$PIXI_HOME/bin` has it (Claude
  Code may start with a shorter `PATH` than its Bash gets). A command that
  sets `PATH`, or runs the tool through `sudo`, `su` or `env -i`, gets no
  such lookup. Others installed globally (pipx, brew) run, and
  `pre-commit` and `nbstripout` are left out on purpose: they're usually
  global git-hook tools (`pixi global install pre-commit` is common).
- With a venv or conda env active, a bare name that is a Python script in
  its `bin` is refused even when it wraps a native tool: `cmake`,
  `clang-format` or `pre-commit` installed there with pip each run the
  env's Python.
- Code piped into a shell is followed from heredocs and from literal
  `echo`/`printf` text, through pipes and `( )`/`{ }` groups; text from
  anything else (`cat script.txt | sh`) isn't looked into.
- `PIXI_GUARD=ask` applies to the whole command line it's in: one prompt
  covers every part of it.
- Running `pixi run bash -c '...'` is trusted: everything inside it runs in
  the pixi environment.
- bash 3.2 (macOS's `/bin/bash`) ends a `$(...)` at the first `)` outside
  the quotes it sees, even one in a quoted heredoc or a `case` pattern, and
  there an apostrophe pair counts as quotes. So in Claude's usual `git
  commit -m "$(cat <<'EOF' ...`, a message like ``Don't use pip (it's
  slow): run `pip install -e .` instead`` or a PR body line like ``1) run
  `pip install` `` ends the `$(...)` early, and the backticks after it run.
  That reading is checked only when Claude Code's shell is bash 3.x (from
  `SHELL` or `CLAUDE_CODE_SHELL`; for a checkpoint job, when `/bin/sh` is,
  as on macOS): with zsh or bash 5 that text is safe
  (bash 5.2 has a trap of its own, below), and refusing it would only get
  in the way. Under bash 3.x a `$(...)`
  holding a `case` statement is a syntax error, and may be refused too.
- bash 5.2 and later end a heredoc inside `$(...)` (and `<(...)`,
  `>(...)`) at any line that starts with its delimiter and has a `)` after
  it, and read the rest of that line as commands. So in Claude's usual
  commit heredoc, a line like ``EOFError was raised :)`` ends the `$(...)`
  at its `)`, and backticks after it run (``Repro: `python -c ...` ``).
  That reading is checked only when Claude Code's shell (for a checkpoint
  job, `/bin/sh`) is bash 5.2 or later; zsh, bash 5.1 and earlier read
  such text as the message it is.
  Where bash 5.2 would then stop at a syntax error, a tool named later in
  the message may still be refused (Claude is told to use `git commit -F
  FILE`, which avoids the problem).
- It's a guard against habits, not a sandbox: it reads a command as bash 5
  does, and as zsh, dash, bash 3.2 and bash 5.2 do where they commonly
  differ. Out of scope, and let through:
  - names and code made at run time: `PY=python3; $PY x`,
    `$(which python) x`, aliases, `bash -c "$(cat file)"`;
  - spellings made to hide a name, beyond quotes, escapes, `$'...'`, brace
    expansion and default values;
  - other dialect corners: zsh-only syntax beyond `=(...)`, glob qualifiers,
    `{ }` and `[[ ]]` short forms; and a `|` in a word in `[[ ... ]]` is a
    regex (`[[ $x =~ a|pip ]]`), though dash, which has no `[[`, runs
    `pip`.
