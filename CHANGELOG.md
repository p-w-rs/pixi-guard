# Changelog

## 0.3.0 (unreleased)

**Renamed.** The plugin is now `pixi-guard` (it was `pixi`): install id
`pixi-guard@pixi-guard`, skill `pixi-guard:project`. An install of
`pixi@pixi-guard` doesn't update to it. Uninstall it, update the
marketplace, and install `pixi-guard@pixi-guard` (see
[README, Install](README.md#install)). Wrappers and converted projects are
kept.

Added:

- The gateway's `job_start` and `terminal_send` are checked. A job is read
  as the gateway's bash 5.2 reads it, in its `cwd` or the project folder; a
  job on another host is not checked. Terminal text is read as if typed into
  a bash. In both, a tool by its bare name is refused even when pixi has it,
  since the gateway runs it with a `PATH` of its own.
- `gate with NAME -- CMD` (the gateway's) is followed, and `CMD` checked.
- `pixi run` and `pixi exec` with a package installer are refused:
  `pip install`, `python -m pip install`, `uv pip install`, `uv add`, also
  in a shell line, a shell or a wrapper that pixi runs. The refusal points
  to `pixi add` (for `pixi exec`, `--spec`). A system package manager
  installing Python under pixi is refused too.
- The command started by `npx`, `npm exec`/`x`, `pnpm exec`/`dlx`,
  `yarn exec`/`dlx`, `bunx` and `bun x` is checked (before, only the MCP
  inspector's).
- `xcrun CMD` is followed: `xcrun python3` runs Xcode's Python.
- The value of `alias NAME=VALUE` is read as code (`alias py=python3`).
- `echo`/`printf` text fed to a shell is also read as the line it prints
  (`printf 'py%s\n' thon | bash`).
- The skill also removes a wrapper when asked to unwrap a project.
- A tool call over 1MB is refused unread, as too large to check;
  `PIXI_GUARD=ask` in it still asks.
- README: an "Update, disable, uninstall" section (what stays behind and how
  to remove it) and requirements. New: this changelog,
  [SECURITY.md](SECURITY.md), [docs/dev/LIMITS.md](docs/dev/LIMITS.md),
  [docs/dev/TESTING.md](docs/dev/TESTING.md), and CI on Linux and macOS.
- `plugin.json` links the repository.

Fixed:

- Only the tool input's own keys are read. A `command` or `host` in a nested
  object no longer decides what is checked.
- `tee >(bash) <<<'python x'` and `pixi run echo python x.py | bash` were
  let through.

## 0.2.0 (2026-10-21)

- Also checks Monitor, the desktop app's terminal tool and checkpoint's
  `job_start` (a job's `command`, or its `argv` as checkpoint hands it to
  `/bin/sh`).
- A tool from pixi passes: one found in `~/.pixi/bin`, `$PIXI_HOME/bin` or a
  pixi environment's `bin`, or run by its path there. Installers stay
  refused.
- Refuses more: programs from a venv or conda env (`.venv/bin/uvicorn`), a
  Python script in the active env's `bin`, more tool names, here-strings
  and `echo`/`printf` text fed to a shell, more wrappers and commands that
  run commands, and default values (`${PYTHON:-python3}`).
- Reads commands also as zsh, dash, bash 3.2 and bash 5.2 do where they
  differ; the bash 3.2 and 5.2 readings only when that shell runs them.
- A `*.py` script whose `#!` line runs pixi may be run by its path.
- A command too large to check, or whose check runs over 7 seconds, is
  refused instead of let through.
- Finds pixi outside `PATH` (the desktop app's short `PATH`), and shows a
  notice at session start when pixi is missing.
- Hooks use the exec form; state moved to the plugin's data folder.

## 0.1.0 (2026-10-17)

First release, as plugin `pixi`.

- Refuses `python`, `pip`, `uv`, `conda`, `poetry` and similar outside
  pixi in Bash commands, with pixi advice for the folder.
  `PIXI_GUARD=ask <command>` turns a refusal into a permission prompt.
- Tells you when pixi is missing.
- The skill converts or wraps a project that isn't on pixi, after asking
  which. `guard.sh wrap-path` gives the out-of-folder wrapper's path.
