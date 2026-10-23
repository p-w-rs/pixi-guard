# pixi-guard

A Claude Code plugin that keeps Claude's Python on pixi (pixi.sh).
Claude runs Python and Python tools only through pixi (`pixi run`,
`pixi exec`, `pixi add`). When it runs `python`, `pip`, `uv`, `conda`,
`poetry`, `pytest` and the like directly, the command is refused and Claude
is told the pixi way to do it. You can still say yes to a one-off.

It guards against habits, not attackers: see [Limits](#limits).

Sister projects: [gateway](https://github.com/p-w-rs/gateway),
[checkpoint](https://github.com/p-w-rs/checkpoint).

> Developing on this repo? Start with [CLAUDE.md](CLAUDE.md).

## Examples

| Claude runs | Result |
|---|---|
| `python3 train.py` | refused: use `pixi run python3 train.py` in a pixi workspace, `pixi exec --spec python python train.py` without one |
| `pip install requests` | refused: use `pixi add requests` (or `pixi add --pypi requests`) |
| `pixi run pytest -q` | runs |
| `pixi run pip install requests` | refused: it changes the environment behind `pixi.lock`; use `pixi add` |
| `cd api && uvx ruff check .` | refused: every part of a command line is read |
| `echo 'pip install x' \| sh` | refused: text fed to a shell is read as commands |
| `npm exec -- python x.py`, `xcrun python3 x.py` | refused: the command they start is checked |
| `git commit -m "drop pip"` | runs: words in quotes aren't commands |
| `mkdocs serve`, after `pixi global install mkdocs` | runs: the tool comes from pixi |
| `./tools/report.py`, whose first line is `#!/usr/bin/env -S pixi exec --spec rich -- python` | runs |
| `PIXI_GUARD=ask python3 -V` | asks you first |

## Install

In Claude Code:

```
/plugin marketplace add p-w-rs/pixi-guard
/plugin install pixi-guard@pixi-guard
```

From a shell: `claude plugin install pixi-guard --marketplace p-w-rs/pixi-guard`.
In the desktop app: **+** next to the prompt box → **Plugins** → **Add
marketplace**, enter `p-w-rs/pixi-guard`, then install `pixi-guard`.

Then start a new session, or run `/reload-plugins` in a running one. To
check that it works, ask Claude to run `pip --version`: it is refused, and
Claude is told the pixi way.

To try it from a checkout without installing:
`claude --plugin-dir plugins/pixi-guard`.

**Installed as `pixi@pixi-guard`?** Up to 0.2.0 the plugin was named `pixi`,
and that install doesn't update to the new name. Remove it, then install the
new one:

```
claude plugin uninstall pixi@pixi-guard
claude plugin marketplace update pixi-guard
claude plugin install pixi-guard@pixi-guard
```

If you added the marketplace with a tag (`p-w-rs/pixi-guard#v0.2.0`), the
update keeps that tag: run `claude plugin marketplace remove pixi-guard` and
`claude plugin marketplace add p-w-rs/pixi-guard` instead of the update.
Your wrappers and converted projects are kept.

**Requirements.** macOS or Linux with `sh` and `awk`: the hook needs neither
pixi nor Python. Claude Code 2.1.139 or newer, for exec-form hooks
(`command` with `args`). pixi-guard is only hooks (no mods), so that is all
it needs, in the terminal and in the desktop app. Tested on 2.1.301 (macOS)
and 2.1.304 (Linux). pixi only for running Python in your projects; the
advice is written for pixi 0.81 (`pixi init --script`, `pixi import`,
`pixi workspace`).

**Settings.** None needed. Optional, set where Claude Code starts:
`PIXI_GUARD_TOOLS` (the program names to refuse, as an awk regex such as
`python|pip|uv`, replacing the built-in list) and `PIXI_GUARD_WRAP_DIR`
(where out-of-folder wrappers go). The hook also reads `SHELL` and
`CLAUDE_CODE_SHELL` (which shell runs Claude's commands), `VIRTUAL_ENV` and
`CONDA_PREFIX` (an active env), `PATH`, `HOME` and `PIXI_HOME` (where a tool
comes from), and `CLAUDE_PROJECT_DIR` (a gateway job's folder).

## What it checks

- **Tools.** Commands Claude runs with Bash and Monitor, the desktop app's
  terminal tool, checkpoint's `job_start`, and the gateway's `job_start`
  and `terminal_send` (see [Jobs and terminals](#jobs-and-terminals)).
  Without those plugins nothing changes: the hook only sees tools that
  exist.
- **Programs.** `python` and `pypy` with or without a version (`python3`,
  `python3.12`, `pypy3`), `pythonw` (`python-config` and `python3-config`
  pass), `pip*`, `uv`/`uvx`, `conda`/`mamba`, `poetry`, `pipenv`, `pyenv`,
  `virtualenv`, `hatch`, `pdm`, `rye`, `tox`, `pytest`, `coverage`,
  `ipython`, `jupyter`, `mypy`, `black`, `flake8`, `uvicorn`, `gunicorn`,
  `flask`, `streamlit`, `celery`, `alembic`, `mkdocs` and similar; a `*.py`
  script; a program in a venv or conda env (`.venv/bin/uvicorn`);
  `source .venv/bin/activate`; installing Python with brew, apt, dnf and
  the like. If Claude Code was started in an active
  venv or conda env, a Python script from its `bin` folder (`httpx`) is
  refused too.
- **Every part of the command line.** It follows `&&`, pipes, `$(...)`,
  `sh -c`, `eval`, heredocs and `echo`/`printf` text fed to a shell,
  `xargs`, `find -exec`, `tmux`, wrappers (`sudo`, `env`, `timeout`,
  `nice`, `direnv exec`, `mise exec`, the gateway's `gate with NAME --`,
  `xcrun`, ...), commands that start other commands (`npx`, `npm exec`,
  `pnpm exec`/`dlx`, `yarn exec`/`dlx`, `bunx`/`bun x`, `hyperfine`,
  `watchexec`, `nodemon --exec`), the value of an alias
  (`alias py=python3`), and default values (`${PYTHON:-python3} x.py`).
- **What passes.** A tool that comes from pixi: a name found in
  `~/.pixi/bin`, `$PIXI_HOME/bin` or a pixi environment's `bin` (also
  inside `pixi shell`), or a path there. Installers and env managers
  (`pip`, `uv`, `conda`, ...) stay refused even from there. A `*.py` script
  whose first line runs pixi, run by its path. Words in quotes, quoted
  heredocs, and `case` and glob patterns are not commands.
- **Installers under pixi.** Everything `pixi run` and `pixi exec` start
  runs, except `pip install`, `python -m pip install`, `uv pip install` and
  `uv add`: packages go in with `pixi add` (for `pixi exec`, with
  `--spec`). That holds inside a shell pixi runs too
  (`pixi run bash -c 'pip install x'`). A system package manager installing
  Python is refused there as well (`pixi run brew install python`).
- **Too large to check.** A tool call over 1MB, a command over 256KB or
  nested too deeply, and a check still running after 7 seconds are refused,
  and Claude is told to split the command. (A hook that runs out of time
  lets the command through, so these are refused instead.)

## Jobs and terminals

With [checkpoint](https://github.com/p-w-rs/checkpoint) or
[gateway](https://github.com/p-w-rs/gateway) installed, jobs and terminal
text are checked before they run.

- **checkpoint's `job_start`.** The `command` is read as `/bin/sh` reads it
  (bash 3.2 on macOS). An `argv` list is read as the line checkpoint gives
  `/bin/sh`: each element is one word. Relative paths are looked up in the
  job's `cwd`.
- **The gateway's `job_start`.** Read as the gateway's bash 5.2 reads it, in
  the job's `cwd`, else the project folder (`$CLAUDE_PROJECT_DIR`). A job
  on another host is not checked, like `ssh HOST python3`. `localhost` and
  the gateway's `m_` ids are checked: they can be this machine.
- **The gateway's `terminal_send`.** The text is read as if typed into a
  bash, with or without Enter. `terminal_send` doesn't say which host the
  terminal is on, so text sent to a terminal on another host is checked as
  if it ran here. The terminal's folder is unknown, so run a pixi `#!`
  script there by its absolute path.
- **A Python tool by its bare name** (`pytest`) is refused in gateway jobs
  and terminals even when pixi has it: the gateway runs commands with a
  `PATH` of its own. `pixi run pytest`, or the tool's path
  (`~/.pixi/bin/pytest`), works.

A refusal tells Claude to make the job's command the pixi one:
`pixi run python train.py`, or the `argv` `["pixi", "run", "python", "train.py"]`.

## Saying yes

If you want something run outside pixi, say so. Claude runs it again as
`PIXI_GUARD=ask <command>` (for a job, with `PIXI_GUARD=ask` in front of its
command or as the first `argv` element), and Claude Code asks you first.
Claude is told to do this only when you ask. One prompt covers the whole
command line.

## No pixi, or a project not on pixi

- **pixi not installed?** The hook looks on `PATH` and in the usual install
  folders (`~/.pixi/bin`, `~/.local/bin`, Homebrew, MacPorts), so the desktop
  app's short `PATH` is fine. If pixi isn't there, you see a notice at
  session start, and Claude offers to install it the first time Python comes
  up.
- **Project not on pixi?** Claude asks you once: *convert* it (your code:
  adds `pixi.toml` or `[tool.pixi]` and a lock file you commit) or *wrap* it
  (someone else's code: a `pixi.toml` that git never sees, through
  `.git/info/exclude`, or one under `~/.local/share/pixi-guard/wrap/`).
  Either way it runs things with `pixi run` afterwards. The steps are in the
  skill [`pixi-guard:project`](plugins/pixi-guard/skills/project/SKILL.md),
  which also removes a wrapper when you ask Claude to unwrap the project.

## Update, disable, uninstall

From a shell (in Claude Code, `/plugin` opens the same actions as a menu):

| What | Command |
|---|---|
| Update | `claude plugin marketplace update pixi-guard`, then `claude plugin update pixi-guard@pixi-guard`; then `/reload-plugins` in a running session, or start a new one |
| Turn off for now | `claude plugin disable pixi-guard@pixi-guard` (back on: `claude plugin enable pixi-guard@pixi-guard`) |
| Uninstall | `claude plugin uninstall pixi-guard@pixi-guard` |
| Forget the marketplace | `claude plugin marketplace remove pixi-guard` |

Uninstalling removes the hook, the skill and the plugin's data folder
(`~/.claude/plugins/data/pixi-guard-pixi-guard`, under `$CLAUDE_CONFIG_DIR`
if set; it holds small per-session markers). What it set up in your projects
stays. Remove what you don't want (before you uninstall, Claude can remove a
wrapper for you: ask it to unwrap the project):

- **Wrappers outside a project:** a folder per project under
  `~/.local/share/pixi-guard/wrap/` (or `$XDG_DATA_HOME/pixi-guard/wrap/`, or
  `$PIXI_GUARD_WRAP_DIR`), with its `pixi.toml`, `pixi.lock` and `.pixi/`
  environment. Delete the folder.
- **Wrappers inside a git checkout:** an untracked `pixi.toml`, `pixi.lock`
  and `.pixi/` at the project root (an environment can take gigabytes),
  hidden from `git status` by three lines (`/pixi.toml`, `/pixi.lock`,
  `/.pixi/`, with the project's path in front when it is in a subfolder:
  `/sub/pixi.toml`) in the file `git rev-parse --git-path info/exclude`
  names. Delete the files and the lines. While the lines stay, a
  `pixi.toml` at that path stays hidden, in every worktree of this clone.
  `git clean -fdx` deletes such a wrapper.
- **Converted projects:** `pixi.toml` (or `[tool.pixi]`) and `pixi.lock` are
  your project's files now. `.pixi/` is the environment: delete it to free
  the space, and `pixi install` makes it again.
- `~/.local/state/pixi-guard/` (or under `$XDG_STATE_HOME`): the markers,
  when Claude Code gives the plugin no data folder. Delete it.

## Limits

- It checks the commands Claude runs, not the scripts they run: Python
  started by a Makefile, `npm run` or a shell script isn't seen.
- Names and code made at run time get through: `PY=python3; $PY x`,
  `$(which python) x`, `cat script.txt | sh`, an alias from your shell's rc
  file.
- Commands sent elsewhere pass: `ssh host python3`, `docker run IMAGE python`,
  a gateway job on another host.
- Under `pixi run` and `pixi exec` only pip and uv installs (and a system
  package manager installing Python) are refused: `pixi run sudo python`,
  `pip uninstall`, `uv sync` and `conda install` run there.
- Python tools not on the list, installed another way (pipx, brew), run.
  `pre-commit` and `nbstripout` are left off on purpose.
- Each `terminal_send` is read on its own: text typed into an editor or a
  REPL that looks like a command (`pip install x`) is refused too.
- When Claude Code's shell is bash 3.2 (macOS's `/bin/bash`) or bash 5.2 and
  later, some commit messages in `git commit -m "$(cat <<'EOF' ...)"` end
  early and run the backticks in them. If what then runs is Python, the
  command is refused, and Claude is told to use `git commit -F FILE`.

The full list, with examples: [docs/dev/LIMITS.md](docs/dev/LIMITS.md).

## More

- What changed in each version: [CHANGELOG.md](CHANGELOG.md)
- Reporting a security problem: [SECURITY.md](SECURITY.md)
- Running the tests, and what a release's verification covers:
  [docs/dev/TESTING.md](docs/dev/TESTING.md)
