# Testing

The suite runs the real `hooks/guard.sh` with a scratch `HOME`, scratch
state and wrapper folders, and a fake pixi (or none, to play a machine
without pixi). It needs no network and no Claude Code.

```bash
cd plugins/pixi-guard
pixi run test          # this machine's sh and awk; prints "N passed, 0 failed"
pixi run test-matrix   # the suite again under each awk and sh found
claude plugin validate .        # the plugin manifest
claude plugin validate ../..    # the marketplace manifest
```

Every behaviour change gets a test: a command that must be refused, and a
near miss that must pass. A test also checks that the version at the top of
[CLAUDE.md](../../CLAUDE.md) matches `plugin.json`.

## The matrix

`test-matrix` runs the whole suite once per variant, through `PATH` shims:
the default `sh` and `awk`, gawk, gawk `--posix`, mawk, busybox awk, BWK awk
(`original-awk`, `bwk-awk`, `one-true-awk` or `nawk`), dash, and dash with
gawk `--posix`. A variant whose tools aren't installed is skipped. The two
7-second watchdog checks run only in `pixi run test`, not in any of the
matrix's variants.

Some checks measure the hook's CPU time against a budget, and run once more
before failing. On a heavily loaded machine a timing check can still fail;
run it again before looking for a bug.

## 0.3.0 verification

Run on 2026-10-22 and 23 (Claude Code 2.1.301 on macOS, 2.1.304 on Linux).

- The suite on macOS arm64 (BWK awk; `/bin/sh` is bash 3.2): 2391 passed;
  the matrix (gawk, gawk `--posix`, dash, dash with gawk `--posix`): 5 of 5
  variants green.
- The suite on Intel macOS through Rosetta (osx-64 packages; the hook's
  `sh` and `awk` ran as x86-64 too): 2391 passed. Rosetta is not real Intel
  hardware.
- The suite on Linux (Debian 13 arm64 VM; dash, mawk 1.3.4 as `awk`): 2371
  passed; the matrix with gawk added: 7 of 7 variants green. Debian 13's
  mawk returns `srand()` in nanoseconds, which the suite's slow-scan stand-in
  now allows for. busybox awk was not run.
- `claude plugin validate` for the plugin and the marketplace.
- A live session (the real Claude Code TUI with a scripted model, installed
  from a local git marketplace): 44 of 44 checks. Python refused, `pixi run`
  and quoted text let through, `PIXI_GUARD=ask` asking even in bypass mode,
  the one notice when pixi is missing, and the move from `pixi@pixi-guard`
  (uninstall, update the marketplace, install `pixi-guard@pixi-guard`): only
  the new plugin's hooks run after it; its data folder is
  `plugins/data/pixi-guard-pixi-guard`.
- Not run: busybox awk, the gateway's real `job_start` and `terminal_send`
  in a live session (the suite feeds the hook inputs shaped like theirs), the
  skill's convert, wrap and unwrap in a live session, an
  install from GitHub of the released version.

