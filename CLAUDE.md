# CLAUDE.md: working on pixi-guard

The plugin is `plugins/pixi-guard/` (plugin name `pixi`, v0.2.0). User docs:
[README.md](README.md). Work in progress: `.work/<task>/TRACKER.md`
(gitignored).

## Layout

- `hooks/hooks.json`: SessionStart and PreToolUse (matcher
  `Bash|Monitor|mcp__terminal__run_in_terminal|mcp__plugin_check_jobs__job_start`,
  an exact-name list; the third is the desktop app's terminal tool, whose
  input is also `command`; the last is checkpoint's job tool, plugin `check`,
  MCP server `jobs`, input `command` or `argv`, `cwd`, ...), both exec form:
  `sh` with args `${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh MODE`. A matcher name
  for a tool that doesn't exist does nothing, so pixi-guard still works
  alone.
- `hooks/guard.sh`: everything. `guard.sh session` (SessionStart context, plus
  a `systemMessage` to the user when pixi can't be found), `guard.sh pre`
  (PreToolUse) and `guard.sh wrap-path [DIR]` (for the skill). `pre` reads
  the payload's `command` (`field()`), else its `argv` (`argvcmd()`: the
  list as checkpoint hands it to `/bin/sh -c`, each element quoted as
  Python's `shlex.quote` does, bare when made of `A-Za-z0-9_@%+=:,./-`; a
  list cut at 1.6MB before its `]` is padded past 256KB so the scan calls it
  `complex`). For checkpoint's job tool (`tool_name`, or an `argv`), `job`
  is set: `bashver /bin/sh` instead of Claude Code's shell (checkpoint runs
  jobs with `/bin/sh`; `PIXI_GUARD_JOB_SH` replaces it in tests), `getcwd`
  takes `tool_input`'s `cwd` (`field cwd tool_input`, relative to the
  session's), and the reason speaks of the job's command (`what`, `shis`,
  `again`, `run`). The `scan` awk
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
  `timeout`, `flock`, `script`, `numactl`, `entr`, ...: tables `WR`, `OS`,
  `OL`, `NP`, `CO` in `BEGIN`), commands that run their arguments
  (`parallel()`, `hyperfine()`, `watchexec`, `nodemon()`, `npx()` for the
  MCP inspector), and program words with `${NAME:-WORD}` in them (`defw()`:
  the word read with each WORD, `${VENV:-.venv}/bin/uvicorn`, is checked
  too). `watched_scan` runs the scan under a watchdog (see invariant 3).
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
  shell (`deftool`, `TN`, with the view tag for the bash notes; refused
  outright when the command sets `PATH` or uses `sudo`, `su`, `env -i`:
  `PATHSET`). A `*.py` run by its path that the
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
  points Claude at it).
- `tests/run_tests.py`: hermetic tests through the real `guard.sh`.

## Commands

```bash
cd plugins/pixi-guard
pixi run test                     # must stay green; prints "N passed, 0 failed"
pixi run test-matrix              # again under gawk, gawk --posix, mawk, busybox awk, dash (those found)
claude plugin validate .          # and `claude plugin validate ../..`
claude --plugin-dir .             # try it without installing
```

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
   appended one by one. Over 256KB a command is `complex` unread (and
   `field()` decodes at most 1.6MB of JSON string). A 256KB command takes
   about a second; the hook timeout is 10 s, and a timeout lets the
   command through (hence also the watchdog, invariant 3). So the queues
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
   counts in the tests' budget checks).
4. Refusals are `deny` with a reason written for Claude; a `PIXI_GUARD=ask`
   assignment prefix (value quoted or not) turns them into `ask`, never
   `allow`.
5. Anything that starts with `pixi` is allowed as is.
6. The wrapper path (`wrap_manifest`) is the only place it's computed. The
   refusal names it, and the skill gets it from `guard.sh wrap-path DIR`
   (Claude may set up a wrapper without ever being refused). Both use the
   physical path (`pwd -P`), so `/tmp` and `/private/tmp` agree. Its name
   (`<name>_-<cksum>`) must not change, or existing wrappers are lost.
7. No state in the plugin folder (it is replaced on update). "told" markers
   go to `$CLAUDE_PLUGIN_DATA/told` (else `~/.local/state/pixi-guard/told`);
   `prune_told` drops week-old ones there and in the 0.1.0 XDG folder;
   wrappers stay in `~/.local/share/pixi-guard/wrap` (outlive the plugin,
   reachable from Bash). Nothing needs settings.json; the `PIXI_GUARD_*`
   environment variables are optional overrides (`PIXI_GUARD_PIXI` is for
   tests: it names the pixi to use and turns the search off; so are
   `PIXI_GUARD_WATCHDOG` and `PIXI_GUARD_JOB_SH`, the shell asked for a
   job's reading instead of `/bin/sh`).
8. No top-level `bin/` in the plugin.
9. The plugin name stays `pixi` (install id `pixi@pixi-guard`, skill
   `pixi:project`): renaming it would orphan existing installs. If another
   enabled plugin is also named `pixi`, one of them loses its hooks.
10. What depends on the machine is read only where it decides, and is
   bounded. bash 3.x as Claude Code's shell (`$CLAUDE_CODE_SHELL` or
   `$SHELL`, asked `--version`) turns the bash 3.2 reading of `$(...)` on;
   only then, because zsh and bash 5 users write ``Don't ... (it's) ...
   `pip` `` in commit heredocs, which bash 3.2 alone runs. Likewise bash 5.2
   and later turn view 4 on (``EOFError ... :)`` in a commit heredoc is
   harmless elsewhere). For a checkpoint job the shell asked is `/bin/sh`
   (bash 3.2 on macOS; dash, no version, on Debian), which runs it. The
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
and a near miss that must pass. Bump `version` in `plugin.json` (and the line
at the top of this file; a test checks they agree) for every release, or
installed copies never update.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings.
- Plain, concise docs. No license file unless asked.
