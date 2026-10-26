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

Before a release, move to the newest pixi and Python, then run both suites:

```bash
pixi self-update
pixi update            # relocks python = "*" to the newest
```

Then set `requires-pixi` in `pixi.toml` to that pixi's version, and
`PIXI_MIN` in `tests/run_tests.py` with it (a test checks the manifest). An
older pixi refuses the manifest; to keep one installed, run the suites
through `pixi exec --spec pixi==VERSION pixi run test`.

Where a real bash 3.2, 5.2 or 5.3 is found (`/bin/bash`, `bash` on `PATH`,
the Homebrew and MacPorts folders, or paths in `PIXI_GUARD_TEST_BASH`,
separated by `:`), the suite also checks that it runs the tool in the
commands refused for it and nothing in the near misses; likewise dash and
zsh (on `PATH`, or `/bin/zsh`) for those only they run. For bash 5.2:
`PIXI_GUARD_TEST_BASH=$(pixi exec --spec bash=5.2 bash -c 'command -v bash') pixi run test`.

The suite stops when the temp folder's path (`TMPDIR`) holds `bash` or
`zsh`: the shell prefix tests take any path holding one for that shell.

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

## 0.4.1 verification

Run on 2026-10-26 (macOS arm64): the suite, 3354 passed (a README check
added). The change is to the README and the tests only; nothing else was
rerun (0.4.0's runs below).

## 0.4.0 verification

Run on 2026-10-24 and 25 (Claude Code 2.1.310 on macOS and Linux; pixi
0.83.0).

- The suite on macOS arm64 (BWK awk; `/bin/sh` is bash 3.2): 3351 passed
  (after one rerun: two 160 KB checks ran over their 4 s CPU budget on a
  loaded machine; an A/B run against the 0.3.0-era guard showed no
  slowdown); the matrix (gawk, gawk `--posix`, dash, dash with gawk
  `--posix`): 5 of 5 variants green.
- The suite on Linux (Debian 13 arm64 VM; dash, mawk 1.3.4 as `awk`): 3178
  passed; the matrix with gawk added: 7 of 7 variants green (after one rerun
  for CPU budgets). busybox awk was not run (no busybox on the VM, none on
  conda-forge).
- `claude plugin validate` for the plugin and the marketplace.
- Live sessions (the real Claude Code TUI with a scripted model, installed
  from local git marketplaces), in checkpoint's acceptance_independence:
  pixi-guard alone, with checkpoint, with the gateway, and all three, on
  macOS and Linux: every check passed (96 of 96 in that script on each).
  Python refused in Bash, in both plugins' `job_start` and in the gateway's
  `terminal_send`, while the other plugins keep working.
- An install from GitHub of the released version (3006155, on 2026-10-26):
  on macOS untagged and tag-pinned, the update from 0.3.0, the uninstall
  forms, and live sessions with checkpoint and the gateway; on the Debian VM
  the install and live sessions with both. `python3` was refused in each
  session that ran it.
- Not run: the suite through Rosetta, busybox awk, the skill's convert, wrap
  and unwrap in a live session.

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

