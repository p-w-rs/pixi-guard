# CLAUDE.md: working on pixi-guard

The plugin is `plugins/pixi-guard/` (plugin name `pixi`, v0.1.0). User docs:
[README.md](README.md). Work in progress: `.work/<task>/TRACKER.md`
(gitignored).

## Layout

- `hooks/guard.sh`: everything. `guard.sh session` (SessionStart context),
  `guard.sh pre` (PreToolUse on Bash) and `guard.sh wrap-path [DIR]` (for the
  skill). The `scan` awk program is a small
  shell tokenizer: quotes, comments, `$(...)`, backticks, heredocs, `sh -c`,
  `eval`, `find -exec`, wrappers (`env`, `sudo`, `timeout`, ...).
- `skills/project/SKILL.md`: how to convert or wrap a project (the refusal
  points Claude at it).
- `tests/run_tests.py`: hermetic tests through the real `guard.sh`.

## Commands

```bash
cd plugins/pixi-guard
pixi run test                     # must stay green; prints "N passed, 0 failed"
claude plugin validate .          # and `claude plugin validate ../..`
claude --plugin-dir .             # try it without installing
```

## Invariants

1. The hook is POSIX `sh` + `awk` only (BSD awk on macOS, so no gawk-isms):
   it must run where pixi and Python are missing. Python is for tests only.
2. Fails open: bad input or an awk error means no output, and the call goes
   ahead.
3. Refusals are `deny` with a reason written for Claude; `PIXI_GUARD=ask`
   (an unquoted assignment prefix) turns them into `ask`, never `allow`.
4. Anything that starts with `pixi` is allowed as is.
5. The wrapper path (`wrap_manifest`) is the only place it's computed. The
   refusal names it, and the skill gets it from `guard.sh wrap-path DIR`
   (Claude may set up a wrapper without ever being refused). Both use the
   physical path (`pwd -P`), so `/tmp` and `/private/tmp` agree.

Add a test for every behavior change, both a command that must be refused
and a near miss that must pass.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings.
- Plain, concise docs. No license file unless asked.
