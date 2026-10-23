---
name: project
description: Put a Python project on pixi. Use when pixi-guard refuses a command in a Python project that has no pixi workspace, or when the user asks to convert a project to pixi or to run someone else's Python code through pixi. Covers converting (the user's own code), wrapping without touching tracked files (someone else's code), and removing a wrapper (unwrapping).
---

# Put a project on pixi

Python on this machine runs only through pixi. When a project isn't on pixi
yet, there are two ways to get it there. Pick with the user, once per project.

## 1. Ask

Look first: who owns it (`git remote -v`, `git log -1 --format=%an`, README),
and what it uses now (`pyproject.toml` and its build backend, `requirements*.txt`,
`environment.yml`, `uv.lock`, `poetry.lock`, `Pipfile`, `.python-version`,
`tox.ini`, a Makefile). Then ask in one short message:

- **Convert**: make pixi the project's own setup (commits a `pixi.toml` or
  `[tool.pixi]` and `pixi.lock`). Right for the user's own code.
- **Wrap**: a pixi workspace only this machine sees; tracked files stay as they
  are. Right for someone else's code, upstream repos, or when the user says
  not to change the project.

Recommend one, then **stop and wait for the answer**, even when the owner
looks obvious: both options create files in the user's folder. Skip the
question only if the user already said which (in this conversation, or by
asking for it). Never change someone else's project setup without a clear yes.

## 2a. Convert

Work on a clean tree (or a branch) so the user can review the diff.

- `environment.yml`: `pixi init --import environment.yml`.
- `pyproject.toml`: `pixi init --format pyproject` adds `[tool.pixi.*]` and
  installs the project itself as an editable package. Keep `[project]`
  dependencies where they are; move dev tools into a feature
  (`pixi add --feature dev pytest`, then an environment that uses it).
- Only `requirements*.txt`: `pixi init`, `pixi add python=<version>`, then
  `pixi import --format pypi-txt requirements.txt`. Prefer conda-forge
  packages (`pixi add numpy`) for heavy compiled ones.
- `uv.lock` / `poetry.lock` / `Pipfile`: treat as `pyproject.toml` (or
  requirements); the old lock file stays until the user says to remove it.
- Python version: take it from `requires-python` / `.python-version`.
- Platforms: pixi init locks only this machine's. If the project also runs
  elsewhere (CI, Linux), `pixi workspace platform add linux-64 ...`.
- Turn the usual commands (Makefile, tox, README, CI) into `[tasks]`:
  `pixi task add test "pytest"`.
- `pixi install`, run the tests through pixi, and show the user the diff.
  Commit `pixi.toml`/`pyproject.toml` and `pixi.lock`; `.pixi/` is ignored
  (pixi init adds it to `.gitignore`).
- Don't delete an old `.venv`, lock files or CI config without asking; offer it.

## 2b. Wrap

**In a git checkout (preferred):** a `pixi.toml` at the project root that
git never sees, so `pixi run ...` works there as usual. Run this from the
project root. `pixi init` also edits `.gitignore` and `.gitattributes`, so it
runs in a temporary folder and only the manifest is moved in; the project's
own files are never touched. The ignore rules go to `info/exclude`, which
`--git-path` finds in worktrees too:

```bash
name=$(basename "$PWD") && t=$(mktemp -d) &&
pixi init --format pixi "$t/$name" && mv "$t/$name/pixi.toml" . && rm -r "$t" &&
ex=$(git rev-parse --git-path info/exclude) && mkdir -p "$(dirname "$ex")" &&
p=$(git rev-parse --show-prefix) &&
printf '/%s%s\n' "$p" pixi.toml "$p" pixi.lock "$p" .pixi/ >> "$ex"
```

`info/exclude` belongs to this clone and is shared by every worktree of
it, so these lines hide a `pixi.toml` at the same path in the other
worktrees too.
Tell the user; converting one of them later needs `git add -f pixi.toml
pixi.lock` (or those lines removed from the exclude file).

Then:

- `pixi add python=<version>` and the project's dependencies
  (`pixi import --format pypi-txt requirements.txt`, or `pixi add --pypi ...`).
- Install the project itself editable:
  `pixi add --pypi --editable "<name> @ file://$PWD"` (name from
  `pyproject.toml` / `setup.py`).
- Add tasks for the commands the user will run.
- `git status` must show nothing new. Never commit these files.

**Nothing may be added to the folder** (not git, read-only, or the user said
so): the wrapper lives outside the folder, at a path pixi-guard picks. Get it
from the project folder with pixi-guard's hook script:

```bash
m=$(sh "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh" wrap-path .)   # .../pixi-guard/wrap/<name>_-<sum>/pixi.toml
pixi init --format pixi "$(dirname "$m")"
pixi add --manifest-path "$m" python=<version>
pixi add --manifest-path "$m" --pypi --editable "<name> @ file://$PWD"
```

pixi-guard recognizes the project as wrapped from then on. Ad-hoc commands
run in the current folder: `pixi run --manifest-path "$m" python ...`. Tasks
run in the manifest's folder, so give them the project as `cwd`:
`test = { cmd = "pytest", cwd = "<project root>" }`. Tell the user the run
command (they may want a shell alias).

## 3. Tell the user

One or two lines: what was done (converted / wrapped, where the manifest is),
and how they run things now (`pixi run test`, `pixi run python ...`).

## 4. Remove a wrapper

When the user asks to unwrap a project, remove only what the wrapper added.
Tell the user what will be deleted first: the `.pixi/` environment can take
gigabytes, and a deleted `pixi.lock` can't be got back.

**In a git checkout:** from the project root. First run `git ls-files
pixi.toml pixi.lock`: if it prints a name, that file is tracked, so it is
the project's own and not a wrapper's; stop there. Otherwise delete the
manifest, the lock file and the environment:

```bash
rm -f pixi.toml pixi.lock && rm -rf .pixi
```

Then the three lines that hid them, unless another worktree of this clone
still has a wrapper at the same path (they all share the exclude file):

```bash
ex=$(git rev-parse --git-path info/exclude) && p=$(git rev-parse --show-prefix) && [ -f "$ex" ] &&
{ grep -vxF -e "/${p}pixi.toml" -e "/${p}pixi.lock" -e "/${p}.pixi/" "$ex" > "$ex.new"; [ $? -le 1 ]; } &&
mv "$ex.new" "$ex"
```

Afterwards `git status` shows nothing new.

**Outside the folder:** the wrapper is the folder that holds the manifest
pixi-guard picks (`pixi.toml`, `pixi.lock`, `.pixi/`). From the project
folder, print its path:

```bash
sh "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh" wrap-path .   # .../pixi-guard/wrap/<name>_-<sum>/pixi.toml
```

If that `pixi.toml` exists, delete the folder it is in by its literal path:
`rm -rf` the printed path without its `/pixi.toml`, written out, not a
`$(...)`.

Then tell the user it is gone: `pixi run` no longer works there, and the
project counts as not on pixi again.
