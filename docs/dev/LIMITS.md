# Limits

pixi-guard guards against habits, not attackers. It reads a command the way
bash 5 does, and the way zsh, dash, bash 3.2, bash 5.2 and bash 5.3 do where
they commonly differ. A command made to hide what it runs can get past it.

This page lists what it doesn't see, and where it refuses more than it
needs to, with examples. The short list is in the
[README](../../README.md#limits).

## Not looked into

- What a command starts on its own: a Makefile, `npm run`, a shell script,
  the commands `parallel -a FILE` reads.
- Commands sent to another machine or a container: `ssh host python3`,
  `docker run IMAGE python x.py`, `docker exec`, `kubectl exec`, and a
  gateway job with another `host`.
- Text fed to a shell from anything but heredocs, here-strings and literal
  `echo`/`printf` words: `cat script.txt | sh`, or uv's install script
  piped from `curl` into `sh` (a later `uv` command is refused).

## Names and code made at run time

- `PY=python3; $PY x`, `$(which python) x`, `bash -c "$(cat file)"`.
- An alias defined outside the checked text: in your shell's rc file, or
  typed by you in a gateway terminal. Also `BASH_ALIASES[p]=python` (or
  `declare -A BASH_ALIASES=(...)`) and then `p x.py`.
- What a shell's `-c` code, or a one-word `pixi run 'echo ...'` line,
  prints into another shell: `bash -c 'echo python x' | bash`,
  `pixi run 'echo python x' | bash`.
- Backtick output used as code (`$(...)` is followed):
  ``bash -c "`echo python x`"``, ``eval "`echo pip install x`"``.
- printf width and precision are not applied:
  `printf '%.*s%s\n' 2 pyx thon | sh` prints `python`. A trailing lone `%`,
  `%q` and `%b` are read loosely.
- Spellings made to hide a name, beyond quotes, escapes, `$'...'`, brace
  expansion and default values.

## Shells behind `>(...)`

- `cmd > >(bash)`: when the target is a shell, the command in front of it
  is not checked. `python x.py > >(bash)` passes (`python x.py > >(tee log)`
  is refused).
- A `pixi run` shell inside `>(...)` is not seen as a shell:
  `echo pip install x | tee >(pixi run bash)`.
- `echo pip install x | pixi run bash -c 'bash'`.

## Under `pixi run` and `pixi exec`

Everything pixi runs is taken to run in its environment, except a package
installer (`pip install`, `python -m pip install`, `uv pip install`,
`uv add`) and a system package manager installing Python.

- These run: `pixi run sudo python`, `pixi run .venv/bin/python`,
  `pixi run bash -c 'PATH=/usr/bin; python x.py'`.
- Other ways to change the environment run: `pip uninstall`, `uv sync`,
  `uv pip sync`, `python -m ensurepip`, `pip-sync`, `conda install`,
  `mamba install`, `micromamba install`.
- pip option abbreviations: `pixi run pip --cache list install x` (pip
  reads `--cache` as `--cache-dir`).
- Refused though harmless: `pixi run python -m pip install --dry-run x`,
  and `pixi run uv add` with no package.

## npm and friends

- `npm -c CMD exec` and `npm --call CMD exec`: npm reads these options
  anywhere on the line, and the hook skips options before `exec`.
- `bun exec` and `pnpx` are not checked.

## Tools found by name

- A tool on the list is let through when the hook's `PATH` finds it in a
  pixi `bin` folder, or finds it nowhere and `~/.pixi/bin` or
  `$PIXI_HOME/bin` has it (Claude Code may start with a shorter `PATH` than
  its Bash gets).
- A command that sets `PATH`, or runs the tool through `sudo`, `su`,
  `env -i` or `xcrun`, gets no such lookup: the tool is refused, and its
  path into pixi works (`sudo ~/.pixi/bin/pytest`).
- Tool names in jobs and the gateway's terminals are looked up on the
  hook's `PATH`: Claude Code's, which they get too (a checkpoint job less
  checkpoint's own pixi environment; the gateway puts its stand-ins first,
  and gives a checkpoint job it runs checkpoint's environment). There a
  tool not on that `PATH` is refused even when `~/.pixi/bin` or
  `$PIXI_HOME/bin` has it: no shell snapshot adds those folders, and the
  folders the user imported into the gateway (`~/.local/bin`, say) come
  after Claude Code's and may hold another copy. Its path into pixi works.
- Python tools installed another way (pipx, brew) run when they are not on
  the list. `pre-commit` and `nbstripout` are left off on purpose: they are
  usually global git-hook tools.
- With a venv or conda env active, a bare name that is a Python script in
  its `bin` is refused even when it wraps a native tool: `cmake`,
  `clang-format` or `pre-commit` installed there with pip run the env's
  Python.

## Scripts with a pixi `#!` line

- Let through only by a literal path. A relative path is not looked up after
  `cd`, `env -C` or `find -execdir` in the same command, nor in a gateway
  terminal, whose folder the hook doesn't know.
- Not when the same command writes the file (`cat > x.py <<'EOF'`, `sed -i`,
  `cp`, `tee`, `curl -o`): its `#!` line is read before the command runs.

## Jobs and terminals

- A job is checked once, when `job_start` is called.
- A checkpoint `argv` list is read up to its first element that is not a
  string.
- Each `terminal_send` is read on its own. A heredoc typed over several
  sends, or text typed into an editor or a REPL, may be refused.
- `terminal_send` names no host, so text sent to a terminal on another host
  is checked as if it ran here.
- A tool call over 1MB is refused even when its gateway job runs on another
  host: the hook doesn't read that far.
- Only the tool input's own keys are read. A `host` or `command` inside a
  nested object is ignored, as the tools ignore it.

## Shell dialects

- bash 3.2 (macOS's `/bin/bash`) ends a `$(...)` at the first `)` outside
  the quotes it sees, even one in a quoted heredoc or a `case` pattern, and
  there an apostrophe pair counts as quotes. So in Claude's usual
  `git commit -m "$(cat <<'EOF' ...`, a message like
  ``Don't use pip (it's slow): run `pip install -e .` instead`` ends the
  `$(...)` early, and the backticks after it run. That reading is checked
  only when Claude Code's shell is bash 3.x (`SHELL` or `CLAUDE_CODE_SHELL`;
  for a checkpoint job, when `/bin/sh` is, as on macOS). When neither names
  a bash or zsh, the bash Claude Code then finds itself is not asked. With
  a shell prefix (`CLAUDE_CODE_SHELL_PREFIX`), it is also checked when the
  shell the gateway's managed shell runs commands in until it is ready may
  be bash 3.x: the one that made Claude Code's shell snapshot (never
  `/bin/sh`), found the way Claude Code finds its own. That is
  `CLAUDE_CODE_SHELL`, then `SHELL` when it is in Claude Code's
  environment, then the preferred shell (bash when `SHELL` holds "bash",
  else zsh) on `PATH`, in `/bin`, `/usr/bin`, `/usr/local/bin`,
  `/opt/homebrew/bin`, the other there, the other on `PATH`: the first that
  is an executable bash or zsh given by its full path (a zsh is read
  anyway). pixi-guard can't see the snapshot, so it takes the first found,
  which made the snapshot unless something changed since. A
  `CLAUDE_CODE_SHELL` bash that is gone now (the gateway's, during a
  rebuild) is taken to have made it, and the first bash found is read.
  Other changes during the session (a shell installed or removed, `SHELL`
  or `PATH` changed) can leave a bash 3.x that runs the command unread. In
  the desktop app hooks may get a shorter `PATH` than commands; that
  matters only when neither `CLAUDE_CODE_SHELL` nor `SHELL` names a usable
  bash or zsh. Any other prefix is taken to do the same (and Claude Code's
  own shell is read too). Under bash 3.x a `$(...)` holding a `case`
  statement may be refused too. A few things
  bash 3.2 alone runs still pass: a heredoc left open in a `$(...)` that
  is a command, whose text becomes that command
  (`$(cat <<E` ... `pip list` ... `)`), and a `$(` or comment inside a
  `${ ...}` or `${| ...}` in `"$(...)"`.
- bash 5.2 and later end a heredoc inside `$(...)` (and `<(...)`, `>(...)`)
  at any line that starts with its delimiter and has a `)` after it, and
  read the rest of that line as commands. So a commit message line like
  ``EOFError was raised :)`` ends the `$(...)`, and backticks after it run
  (``Repro: `python -c ...` ``). That reading is checked when Claude Code's
  shell is bash 5.2 or later, and always for jobs and the gateway's
  terminals (the gateway's own bash is conda-forge's newest: 5.2 today, not
  pinned; while it serves the session it runs checkpoint's jobs too). It is
  also checked behind a shell prefix, whatever `CLAUDE_CODE_SHELL` is: the
  gateway's managed shell, once its build finishes during the session, runs
  commands in that bash.
- Either way the refusal tells Claude to write the message to a file and use
  `git commit -F FILE`.
- bash 5.3 and later run `${ CMD; }` and `${| CMD; }` (zsh, dash and older
  bash call them a bad substitution). CMD is read as commands when Claude
  Code's shell is bash 5.3 or later, and always for jobs, the gateway's
  terminals and commands behind a shell prefix. Those are read as bash 5.2
  too, which takes `${ ...` up to its first `}` as a bad substitution,
  drops the rest of that line and runs the next ones, even lines bash 5.3
  reads as a quoted heredoc in CMD. pixi-guard reads the rest of that line
  too, so it may refuse a job for text that neither bash runs. A checkpoint
  job and a command behind a shell prefix are also read as zsh and dash
  read the `${ ...}` (a parameter expansion), where they end it elsewhere,
  since `/bin/sh` may be one of them, and the managed shell's not-ready
  shell may be zsh.
- A checkpoint job is read both as `/bin/sh` and as the gateway's bash,
  which runs it while the gateway serves the session, so it is refused for
  what either one runs, also when the gateway isn't there. A command behind
  a shell prefix is likewise refused for what the managed shell's
  not-ready shell or the gateway's bash runs.
- Not read: zsh-only syntax beyond `=(...)`, glob qualifiers, and the `{ }`
  and `[[ ]]` short forms. A `|` in a word in `[[ ... ]]` is a regex
  (`[[ $x =~ a|pip ]]`), though dash, which has no `[[`, runs `pip`.

## Size and time

- A payload of tens of MB: the hook still reads it before refusing it as
  too large, and at that size, depending on the machine's load, the read can
  outlast Claude Code's 10 s hook timeout, which lets the command through. A
  model doesn't write that much in one tool call.
- A very large `printf` into a program that is not a shell can be refused as
  too large: its output is worked out either way. Example: a 100-character
  format with 20,000 arguments piped into `cat`.
- A command with `${ ...; }` in it is read up to five ways (bash 5.3, 5.2
  and 3.2, zsh, dash) in a checkpoint job whose `/bin/sh` is bash 3.2, or
  behind a shell prefix whose not-ready shell may be, so there one of
  120KB or more can be refused as too large where a gateway job of it
  passes.
