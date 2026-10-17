# pixi-guard

A Claude Code plugin that keeps Python on [pixi](https://pixi.sh). Claude
runs Python and Python tooling only through pixi (`pixi run`, `pixi exec`,
`pixi add`). Running `python`, `pip`, `uv`, `conda`, `poetry` and the like
directly takes your explicit OK.

Sister projects: [gateway](https://github.com/p-w-rs/gateway),
[checkpoint](https://github.com/p-w-rs/checkpoint).

> Developing on this repo? Start with [CLAUDE.md](CLAUDE.md).

## What it does

- **Refuses Python outside pixi.** A hook checks every Bash command Claude
  runs. If any part of it starts `python*`, `pip*`, `uv`/`uvx`, `easy_install`,
  `conda`/`mamba`, `poetry`, `pipenv`, `pyenv`, `virtualenv`, `hatch`, `pdm`,
  `rye`, `pytest`, `ipython`, `jupyter`, `mypy`, `black` and similar, a
  `*.py` script, or `source .venv/bin/activate`, or installs Python with
  brew/apt, the command is refused. The refusal tells Claude the pixi way to do
  it for the folder it's in. It follows `&&`, pipes, `$(...)`, `sh -c`,
  `eval`, `xargs`, `find -exec`, `sudo`/`env`/`timeout` and heredocs fed to a
  shell. Words in quotes or heredocs are not commands, so
  `git commit -m "drop pip"` goes through.
- **You can still say yes.** If you want something run outside pixi, Claude
  re-runs it as `PIXI_GUARD=ask <command>`, and you get Claude Code's
  permission prompt for it. Claude is told to use this only when you ask.
- **pixi not installed?** Claude tells you the first time Python comes up in
  a session, and offers to install it (`brew install pixi` or the pixi.sh
  installer).
- **Project not on pixi?** Claude asks you once: *convert* it (your code:
  adds `pixi.toml` or `[tool.pixi]` and a lock file you commit) or *wrap* it
  (someone else's code: a `pixi.toml` that git never sees, through
  `.git/info/exclude`, or one outside the folder under
  `~/.local/share/pixi-guard/wrap/`). Either way you run things with
  `pixi run ...` afterwards. The steps are in the plugin's skill
  ([`pixi:project`](plugins/pixi-guard/skills/project/SKILL.md)).
- At session start Claude gets two sentences about the policy.

## Install

Needs only [Claude Code](https://code.claude.com). The hook is plain `sh` and
`awk` (macOS, Linux), so it works on a machine without pixi or Python.

```bash
claude plugin marketplace add p-w-rs/pixi-guard
claude plugin install pixi@pixi-guard
```

Or from a clone: `claude plugin marketplace add /path/to/pixi-guard`. Restart
Claude Code. To try it without installing:
`claude --plugin-dir plugins/pixi-guard`.

## Settings

Environment variables, all optional:

- `PIXI_GUARD_TOOLS`: the program names to refuse, as an awk regex
  alternation (`python|pip|uv`), replacing the built-in list.
- `PIXI_GUARD_WRAP_DIR`: where out-of-folder wrappers go (default
  `~/.local/share/pixi-guard/wrap`).
- `PIXI_GUARD_HOME`: small state, such as which sessions were told pixi is
  missing (default `~/.local/state/pixi-guard`).

## Limits

- It checks Bash commands Claude runs. A script that starts Python itself
  (a Makefile, `npm run`, a shell script) isn't looked into.
- It's a guard against habits, not a sandbox: a command built to hide what it
  runs can get past it.
- Running `pixi run bash -c '...'` is trusted: everything inside it runs in
  the pixi environment.
