# CLAUDE.md: working on pixi-guard

The plugin is `plugins/pixi-guard/` (plugin name `pixi-guard`, v0.3.0). User docs:
[README.md](README.md); the full limits list is
[docs/dev/LIMITS.md](docs/dev/LIMITS.md), the suite and what a release's
verification covers [docs/dev/TESTING.md](docs/dev/TESTING.md), releases
[CHANGELOG.md](CHANGELOG.md). Work in progress: `.work/<task>/TRACKER.md`
(gitignored).

## Layout

- `hooks/hooks.json`: SessionStart and PreToolUse (matcher
  `Bash|Monitor|mcp__terminal__run_in_terminal|mcp__plugin_check_jobs__job_start|mcp__plugin_gate_gw__job_start|mcp__plugin_gate_gw__terminal_send`,
  an exact-name list), both exec form: `sh` with args
  `${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh MODE`. The tools and the input
  they are read by:
  - `Bash`, `Monitor` and `mcp__terminal__run_in_terminal` (the desktop
    app's terminal tool): `command`;
  - checkpoint's job tool (plugin `check`, MCP server `jobs`): `command` or
    `argv`, `cwd`;
  - the gateway's (plugin `gate`, MCP server `gw`): `job_start` with
    `command` or `argv`, `cwd`, `host`; `terminal_send` with `text`.

  A matcher name for a tool that doesn't exist does nothing, so pixi-guard
  still works alone.
- `hooks/guard.sh`: everything. `guard.sh session` (SessionStart context, plus
  a `systemMessage` to the user when pixi can't be found), `guard.sh pre`
  (PreToolUse) and `guard.sh wrap-path [DIR]` (for the skill).
  `pre` first measures the payload (`wc -c`): over 1MB it is refused unread
  (`jix=cut`; `askjson` still turns that into `ask` when `PIXI_GUARD=ask` is
  in it). Otherwise `jindex` walks it once and lists where the keys it reads
  start (`$jix`): the top level's `tool_name`, `cwd`, `session_id`, and
  tool_input's own `command`, `argv`, `text`, `host`, `cwd`. A key in a
  nested object or inside a string is another key, which the tools ignore.
  Over 500,000 quotes, or 4KB with a bracket between two strings, it prints
  `cut` too. `field KEY [OBJ]` (`field cwd tool_input`) and `argvcmd()`
  read values from those offsets (`jat`). `pre` reads `text` for a
  terminal, else `command`, else `argv` (`argvcmd()`: the list as the job
  tool hands it to its shell, each element quoted as Python's `shlex.quote`
  does, bare when made of `A-Za-z0-9_@%+=:,./-`; a list cut at 1.6MB before
  its `]` is padded past 256KB so the scan calls it `complex`, a backstop
  behind the 1MB cap). By `tool_name` it sets:
  - `job=check` (also for an `argv` from a tool not named here): `bashver /bin/sh` instead of Claude
    Code's shell (checkpoint runs jobs with `/bin/sh`; `PIXI_GUARD_JOB_SH`
    replaces it in tests);
  - `job=gw`: `b52=1` (the gateway's bash is pinned at 5.x, locked at
    5.2.37; no shell is asked) and `gwp=1` (its own PATH, see `PATHSET`). A
    `host` other than none, `""`, `localhost` or a well-formed `m_` id (`m_`
    and 26 of a-z2-7, `hostid`, which adds a note to the reason) exits
    unchecked: the job runs on another machine;
  - `term=1` (`terminal_send`): `b52=1 gwp=1 tcd=1` (`TCD`: the terminal's
    folder is unknown, so a relative `*.py` is not looked up, as after
    `cd`).

  For a job, `getcwd` takes tool_input's `cwd` (`jc`, relative to the
  session's); a gateway job with none runs in `$CLAUDE_PROJECT_DIR`, else
  the session's folder (the gateway's MCP server fills it in the same way,
  before any `workspace` applies). The reason speaks of the job's command
  or the terminal's input (`what`, `shis`, `again`, `run`). The `scan` awk
  program is a small shell tokenizer: quotes (also `$'...'`, escapes
  decoded), comments, `$(...)` and backticks (queued as index ranges),
  `${...}`, arithmetic (also `$[...]`), heredocs (also inside `$(...)`),
  what is piped into a shell (heredoc bodies and `echo`/`printf` words,
  escapes decoded, through pipes, lines ending in `|`, `( )`/`{ }` groups
  and `tee >(bash)`: decided by `flush()` once the pipeline is complete),
  here-strings, redirections, `case` patterns, extglob and zsh glob groups
  (`@(...)`, `(a|b).py`: patterns read to their `)` by `xglob()`, not
  subshells; with no `)`, the rest is read as commands), `[[ ... ]]`,
  `sh -c`, `eval`, `trap`, `find -exec`, `fd -x`, `su`/`runuser`,
  `tmux`/`screen`, wrappers and their options (`env`, `sudo`, `xargs`,
  `timeout`, `flock`, `script`, `numactl`, `entr`, `xcrun`, ...: tables
  `WR`, `OS`, `OL`, `NP`, `CO` in `BEGIN`; `xcrun -f`/`-find`/`--find` only
  prints a path, `NOEX`), `direnv exec`, `mise exec`, the gateway's
  `gate with NAME -- CMD`, commands that run their arguments (`parallel()`,
  `hyperfine()`, `watchexec`, `nodemon()`; `npx()` for `npx`, `bunx` and,
  through `npmexec()`, `npm [OPTS] exec|x`, `pnpm exec|dlx`, `yarn
  exec|dlx`, `bun x`, with `NPV` the options that take a value; `-c`,
  `--call`, `--shell-mode` are shell code), the value of `alias NAME=VALUE`
  (queued as code: an interactive shell, such as a gateway terminal,
  expands it), and program words with `${NAME:-WORD}` in them (`defw()`:
  the word read with each WORD, `${VENV:-.venv}/bin/uvicorn`, is checked
  too). Echo and printf text fed to a shell (`echoargs`) is queued word by
  word and, for more than one word, as the line it prints: echo's words
  joined (`ejoin`), printf's format with its arguments put in (`pfmt`,
  `PFL` the directive flags; its work is counted with `spent()` first).
  `pixi()` reads `pixi [OPTS] run|exec [OPTS] [--] CMD` (option tables
  `OS`/`OL["pixi run"|"pixi exec"]`): one word is a shell line to pixi
  (queued), more are a program and its arguments (checked directly). Either
  way the code is read in pixi mode, `PXM` ("" outside pixi, `run`,
  `exec`), which travels with queued strings (`SQM`) and ranges (`RM`,
  `PXR` the range's own mode); there only `pxinst` refuses anything (with
  `pipargs`, `uvargs` and the tables `PIPC`, `UVC`, `UVPC`, `PIPV`, `UVV`),
  and a system package manager (`pkg`) as anywhere. What pixi runs sets no
  `PATHSET` or `CDX` for the rest (saved and restored), and what its
  `echo`/`printf` prints goes into the outer pipeline (`ECKD`, the check
  depth whose echo counts). `check()`/`check1()` return 0, or the mode
  code (`mcode()`, 1-3) of a shell that reads stdin as code; a `>(bash)`
  gets the mode of the code that has it (`rcode()`, `PSUB`). `endcmd`
  keeps that per command (`CMDSH`) and `flush()` per pipeline stage
  (`PSH`; in a range whose output is code, `feed()`'s `RF`, for every
  command). `watched_scan` runs the scan under a watchdog
  (see invariant 3).
  It reads as bash 5 (view 0); where zsh, dash or bash 3.2 would read the
  text differently (quotes in `"${...}"`, a bare `{` in `${...}`, `${...}`
  inside `$(...)`, `$[...]`, `!(...)`), `parse()` queues the range again for
  that view (`VIEW` 1-4, at most once each). When Claude Code's shell is
  bash 3.x (`bashver`, awk `B32`), view 3 also ends `$(...)` where bash 3.2
  does (`cpar32`: no heredocs or case patterns; `close_sub` asks for view 3
  where that differs). When it is bash 5.2 or later (`B52`), view 4 reads a
  heredoc in a `$(...)`, `<(...)` or `>(...)` body (ranges with `RC`) as
  bash 5.2 does: a line that starts with the delimiter and has a `)` after
  it ends the body, and the rest of that line is code (`body_end` cp 2 in
  `cpar`, 3 in `parse()`; view 0 asks for view 4 where `cpar` sees such a
  line, cp 1). See invariant 10.
  Tools: `istool` lets a path into a pixi env's `bin` through (`pixibin`:
  `~/.pixi/bin`, `$PIXI_HOME/bin`, `.pixi/envs/NAME/bin`), except installers
  and env managers (`instre`), and lists a tool by its bare name for the
  shell (`deftool`, `TN`, with the view tag for the bash notes). When the
  tool would be looked up in a `PATH` the hook doesn't see, `PATHSET` is set
  and `END` refuses the first listed name outright: the command sets `PATH`
  (an assignment, `export`/`declare` ..., zsh's `path=(...)`), or runs it
  through `sudo`/`doas`, `su`/`runuser`, `env -i` or `env -u PATH`, or
  `xcrun` with a program left to run (Xcode's folders come first; not
  `xcrun --show-sdk-path` or `-f`), and from `BEGIN` for every gateway job
  and terminal (`gwp`: the gateway's folders, `import_path`, then
  `/usr/bin:/bin:/usr/sbin:/sbin`). A `*.py` run by its path that the
  same command writes (`wtgt`: redirections, `tee`, `sed -i`, `cp`/`mv`,
  `curl -o`, ...) or after a change of folder (`CDX`) is refused in `END`.
  After the scan, the shell decides what it left (`py PATH`, `tool NAME TAG`
  and `prog NAME` lines): `pyscript` (a `*.py` run by its path with a pixi
  `#!` line passes), `toolpath` (a tool that resolves into a pixi env's
  `bin`, shell `pixibin`, which also knows `conda-meta/pixi`, passes) and
  `envbin` (a Python script in the active venv/conda env's `bin`).
- `hooks/pixi.sh`: finds pixi on PATH or in the usual install folders
  (`--where`), or runs it. Claude Code may start with a minimal PATH.
- `skills/project/SKILL.md`: how to convert or wrap a project (the refusal
  points Claude at it), and how to remove a wrapper.
- `tests/run_tests.py`: hermetic tests through the real `guard.sh`.

## Commands

```bash
cd plugins/pixi-guard
pixi run test                     # must stay green; prints "N passed, 0 failed"
pixi run test-matrix              # again under gawk, gawk --posix, mawk, busybox awk, BWK awk, dash (those found)
claude plugin validate .          # and `claude plugin validate ../..`
claude --plugin-dir .             # try it without installing
```

CI runs both suites on Linux and macOS; see
[docs/dev/TESTING.md](docs/dev/TESTING.md).

## Invariants

1. The hook is POSIX `sh` + `awk` only (BSD awk on macOS, mawk, gawk
   `--posix`, dash as `sh`; no gawk-isms, no backslashes in gsub
   replacements, and no one-character `split()` separator where the text
   may hold a newline: BWK awk splits at newlines too): it must run where
   pixi and Python are missing. Python is for tests only.
2. Linear time: BWK awk's `substr()` and `length()` walk the whole string,
   so text is split into `ch[]` (or a word into an array with `chars()`)
   once and long strings grow through small buffers; a regex `gsub()` over
   a long word can be quadratic too (it retries at every position). Input
   is read with `RS = "\001"`, so records are joined once (`rjoin`), not
   appended one by one. A payload over 1MB is refused unread (`pre`, before
   `jindex`); `jindex` is one linear walk with its own budgets (500,000
   quotes; 4KB with a bracket between two strings, since BWK awk's `gsub()`
   is quadratic in its number of matches) and takes up to about 0.9 s per
   MB under gawk. Over 256KB a command is `complex` unread (the 1.6MB caps
   in `field()` and `argvcmd()` are backstops behind the 1MB cap). A 256KB
   command takes about a second; the hook timeout is 10 s, and a timeout
   lets the command through (hence also the watchdog, invariant 3). So the queues
   (strings: 4x the input in bytes; ranges: 200k), the look-ahead scans
   (`spent()`: 8x the input), heredoc delimiters (256 characters) and
   recursion (`cpar` and `brace_end` 200, `check` 10 deep) have budgets;
   running out prints `complex`, which is refused like a hit ("too large
   or too deeply nested", `PIXI_GUARD=ask` still asks). A new look-ahead loop must
   call `spent()` or stay within one pass, and stop once `hit` is set
   (returning what ends its caller's loop, `HI + 1` for an index): a budget
   only stops work that checks it, and nested look-aheads (`${...$(...)}`
   read in two views) were exponential. A failed look-ahead that can be
   repeated over the same text is memoized (`bracket_end`: no `]` in
   `NORB..NORBH`; `arith_end`: no `)` in `NOAR..NOARH`), and parse() does
   not take the whole word (`wget()`) again at each `(` in it (`WNA`).
3. Fails open on errors: bad input or an awk error means no output, and the
   call goes ahead. But running out of time is a refusal: `watched_scan`
   kills a scan still running after 7 s (`PIXI_GUARD_WATCHDOG`, 1-7, for
   tests) and prints `complex` (and `ask` if `PIXI_GUARD=ask` is in the
   text), because Claude Code's 10 s hook timeout would let the command
   through, and a slow scan is what a command built to get past the
   budgets (or one a budget misses) looks like. The scan's side kills the
   sleeper as soon as the scan ends, and on time-out kills its awk and
   waits for it: nothing is left running (and the killed scan's CPU time
   counts in the tests' budget checks). The watchdog covers only the scan:
   `jindex` runs before it (hence the 1MB cap) and the shell's lookups
   after it (capped, invariant 10). The worst whole hook measured about
   8.1 s, with the scan held to its 7 s.
4. Refusals are `deny` with a reason written for Claude; a `PIXI_GUARD=ask`
   assignment prefix (value quoted or not) turns them into `ask`, never
   `allow`.
5. Anything that starts with `pixi` is allowed as is, except a package
   installer run by `pixi run` or `pixi exec` (`pip install`, `python -m pip
   install`, `uv pip install`, `uv add`; `pixi()` in `scan`): it changes the
   environment behind pixi.lock's back (pixi exec's cached one, behind its
   specs'), so it is refused with the `pixi add` (`pixi exec --spec`)
   advice, and `PIXI_GUARD=ask` still asks. Code a shell runs under pixi
   (`pixi run bash -c '...'`, its heredocs, a `>(bash)` in it: `PXR`, the
   mode of the range being parsed) is read the same way: its Python passes,
   its installers are refused. What `pixi run echo`/`printf` prints into a
   shell outside pixi is code for that shell (`ECKD`). A system package
   manager installing Python is refused under pixi as anywhere (`pixi run
   brew install python`, `pixi run apt-get install python3-pip`). Other
   ways to change the environment (`pip uninstall`, `uv sync`, `conda
   install`) pass; docs/dev/LIMITS.md lists them.
6. The wrapper path (`wrap_manifest`) is the only place it's computed. The
   refusal names it, and the skill gets it from `guard.sh wrap-path DIR`
   (Claude may set up a wrapper without ever being refused). Both use the
   physical path (`pwd -P`), so `/tmp` and `/private/tmp` agree. Its name
   (`<name>_-<cksum>`) must not change, or existing wrappers are lost.
7. No state in the plugin folder (it is replaced on update). "told" markers
   go to `$CLAUDE_PLUGIN_DATA/told` (else `~/.local/state/pixi-guard/told`;
   `PIXI_GUARD_HOME` replaces the folder that holds `told`, and the tests
   set it); `prune_told` drops week-old ones there and in the 0.1.0 XDG
   folder;
   wrappers stay in `~/.local/share/pixi-guard/wrap` (outlive the plugin,
   reachable from Bash). Nothing needs settings.json; the `PIXI_GUARD_*`
   environment variables are optional overrides (`PIXI_GUARD_PIXI` is for
   tests: it names the pixi to use and turns the search off; so are
   `PIXI_GUARD_WATCHDOG` and `PIXI_GUARD_JOB_SH`, the shell asked for a
   job's reading instead of `/bin/sh`).
8. No top-level `bin/` in the plugin.
9. The plugin name is `pixi-guard` (install id `pixi-guard@pixi-guard`,
   skill `pixi-guard:project`, data folder `pixi-guard-pixi-guard`, which
   Claude Code makes of the id by turning each character outside
   `A-Za-z0-9_-` into `-`; the hook takes it from `$CLAUDE_PLUGIN_DATA`),
   and it stays that: a rename orphans
   existing installs. It was `pixi` up to 0.2.0; those installs must be
   uninstalled and `pixi-guard@pixi-guard` installed (README, Install). The
   skill's name in guard.sh's texts must be `<plugin name>:project` (a test
   checks it).
10. What depends on the machine is read only where it decides, and is
   bounded. bash 3.x as Claude Code's shell (`$CLAUDE_CODE_SHELL` or
   `$SHELL`, asked `--version`) turns the bash 3.2 reading of `$(...)` on;
   only then, because zsh and bash 5 users write ``Don't ... (it's) ...
   `pip` `` in commit heredocs, which bash 3.2 alone runs. Likewise bash 5.2
   and later turn view 4 on (``EOFError ... :)`` in a commit heredoc is
   harmless elsewhere). For a checkpoint job the shell asked is `/bin/sh`
   (bash 3.2 on macOS; dash, no version, on Debian), which runs it. Gateway
   jobs and terminals are read as a fixed bash 5.2 (the gateway's own), and
   no shell is asked. A gateway job with no `cwd` runs in
   `$CLAUDE_PROJECT_DIR` (else the session's folder). The
   scan reads `HOME` and `PIXI_HOME` (`ENVIRON`)
   for literal paths into pixi's folders. After the scan (outside the
   watchdog, so capped): at most 16 `*.py` paths, each let through only if
   its first KB starts with a `#!` that runs pixi (literal paths only; a
   relative one not after `cd`, `env -C`, `-execdir`, ...: `CDX`; none the
   command writes); at most 16 tool names, each looked up with `command -v`
   (or, if not found, in `~/.pixi/bin` and `$PIXI_HOME/bin`) and let through
   only from a pixi env's `bin`; and with a venv or conda env active (never
   a pixi one: not under `.pixi/envs`, no `conda-meta/pixi`) at most 256
   bare names looked up in its `bin`, refused if a Python script.

Add a test for every behavior change, both a command that must be refused
and a near miss that must pass; a new gap left open goes in
docs/dev/LIMITS.md. Bump `version` in `plugin.json` (and the line at the
top of this file; a test checks they agree) for every release, or installed
copies never update, and add its entry to CHANGELOG.md.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings.
- Plain, concise docs. No license file and no license wording (the user's
  decision, 2026-10-22).
- A hobby project: no CI/CD (no GitHub Actions) and no enterprise-style
  release process.
