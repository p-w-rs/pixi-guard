---
name: project
description: Put a Python project on pixi. Use when pixi-guard refuses a command in a Python project that has no pixi workspace, or when the user asks to convert a project to pixi or to run someone else's Python code through pixi. Covers converting (the user's own code) and wrapping without touching tracked files (someone else's code).
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
git never sees, so `pixi run ...` works there as usual.

```bash
pixi init --format pixi .     # creates pixi.toml; also adds to .gitignore and .gitattributes
printf '%s\n' /pixi.toml /pixi.lock /.pixi/ >> "$(git rev-parse --git-dir)/info/exclude"
```

Undo what pixi init did to `.gitignore`/`.gitattributes`: `git checkout --` them if
they are tracked, delete them if pixi created them. Then:

- `pixi add python=<version>` and the project's dependencies
  (`pixi import --format pypi-txt requirements.txt`, or `pixi add --pypi ...`).
- Install the project itself editable:
  `pixi add --pypi --editable "<name> @ file://$PWD"` (name from
  `pyproject.toml` / `setup.py`).
- Add tasks for the commands the user will run.
- `git status` must show nothing new. Never commit these files.

**Nothing may be added to the folder** (not git, read-only, or the user said
so): the wrapper lives outside the folder, at a path pixi-guard picks. Get it
from the project folder with the hook script next to this skill (this skill's
base directory is shown above; the script is two levels up):

```bash
m=$(sh "<skill base dir>/../../hooks/guard.sh" wrap-path .)   # .../pixi-guard/wrap/<name>-<sum>/pixi.toml
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
