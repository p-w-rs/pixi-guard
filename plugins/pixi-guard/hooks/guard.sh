#!/bin/sh
# pixi-guard hooks. Usage: guard.sh session|pre   (hook payload JSON on stdin)
#
#   session  SessionStart: print the Python-via-pixi policy as additionalContext.
#   pre      PreToolUse(Bash): refuse a command that runs Python or Python tooling
#            outside pixi, with a reason that tells Claude what to do instead.
#            `PIXI_GUARD=ask <command>` turns the refusal into a permission
#            prompt, so running it outside pixi needs the user's yes.
#
# POSIX sh + awk only: it has to work on a machine without pixi (or Python).
# Fails open: anything unexpected means no output, and the call goes ahead.

mode=$1
input=$(cat)
st=${PIXI_GUARD_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/pixi-guard}
wrapdir=${PIXI_GUARD_WRAP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pixi-guard/wrap}
pixi_bin=${PIXI_GUARD_PIXI:-pixi}

# Decoded value of the first "key": "string" in the payload.
field() {
	printf '%s' "$input" | awk -v key="$1" '
	{ text = text $0 "\n" }
	END {
		if (!match(text, "\"" key "\"[ \t\r\n]*:[ \t\r\n]*\"")) exit
		s = substr(text, RSTART + RLENGTH); out = ""
		hex = "0123456789abcdef"
		for (k = 1; k <= length(s); k++) {
			c = substr(s, k, 1)
			if (c == "\"") break
			if (c != "\\") { out = out c; continue }
			k++; c = substr(s, k, 1)
			if (c == "n") out = out "\n"
			else if (c == "t") out = out "\t"
			else if (c == "r") out = out "\r"
			else if (c == "b" || c == "f") out = out " "
			else if (c == "u") {
				h = tolower(substr(s, k + 1, 4)); k += 4; v = 0
				for (j = 1; j <= 4; j++) v = v * 16 + index(hex, substr(h, j, 1)) - 1
				out = out (v > 0 && v < 128 ? sprintf("%c", v) : "?")
			} else out = out c
		}
		printf "%s", out
	}'
}

# JSON string literal of $1.
jstr() {
	printf '%s' "$1" | awk '
	{ text = text (NR > 1 ? "\n" : "") $0 }
	END {
		gsub(/\\/, "\\\\", text); gsub(/"/, "\\\"", text)
		gsub(/\t/, "\\t", text); gsub(/\r/, "\\r", text); gsub(/\n/, "\\n", text)
		printf "\"%s\"", text
	}'
}

have_pixi() { command -v "$pixi_bin" >/dev/null 2>&1; }

PIXI_INSTALL='brew install pixi, or the installer at https://pixi.sh (curl -fsSL https://pixi.sh/install.sh | sh)'

# Is a directory a pixi workspace?
is_pixi_dir() {
	[ -f "$1/pixi.toml" ] && return 0
	[ -f "$1/pyproject.toml" ] && grep -q '^\[tool\.pixi' "$1/pyproject.toml" 2>/dev/null
}
# Does a directory look like a Python project?
is_py_dir() {
	for f in pyproject.toml setup.py setup.cfg requirements.txt requirements-dev.txt \
		Pipfile environment.yml environment.yaml uv.lock poetry.lock .python-version tox.ini; do
		[ -e "$1/$f" ] && return 0
	done
	return 1
}

# Sets proj_kind (pixi | wrapped | python | none) and proj_root for directory $1.
classify() {
	proj_kind=none proj_root= d=$1 pyroot=
	while [ -n "$d" ] && [ -d "$d" ]; do
		if is_pixi_dir "$d"; then proj_kind=pixi proj_root=$d; return; fi
		[ -z "$pyroot" ] && is_py_dir "$d" && pyroot=$d
		[ "$d" = / ] || [ "$d" = "$HOME" ] && break
		d=$(dirname "$d")
	done
	[ -n "$pyroot" ] || return
	proj_root=$pyroot
	if [ -f "$(wrap_manifest "$pyroot")" ]; then proj_kind=wrapped; else proj_kind=python; fi
}

# Out-of-tree wrapper manifest for a project root (used when nothing may be
# added to the project's folder).
wrap_manifest() {
	sum=$(printf '%s' "$1" | cksum | cut -d ' ' -f 1)
	printf '%s/%s-%s/pixi.toml' "$wrapdir" "$(basename "$1" | tr -c 'A-Za-z0-9._-' '_')" "$sum"
}

# Scan a shell command for programs that run Python outside pixi. Prints
# "hit <name>" for the first one found and "ask" if a PIXI_GUARD=ask prefix
# was used. Quotes, comments, $(...), backticks, sh -c, eval, find -exec and
# heredocs fed to a shell are followed; text in quotes or heredocs is not a
# command, so `git commit -m "pip install"` passes.
scan() {
	printf '%s' "$1" | awk -v tools="${PIXI_GUARD_TOOLS:-}" '
	BEGIN {
		if (tools == "") tools = "python|python[0-9][0-9.]*|pythonw|pip|pip[0-9][0-9.]*|pipx|uv|uvx|easy_install|easy_install-[0-9.]+|conda|mamba|micromamba|poetry|pipenv|pyenv|virtualenv|hatch|pdm|rye|flit|twine|tox|nox|ipython|ipython[0-9]|jupyter|jupyter-[a-z-]+|pytest|py\\.test|mypy|black|isort|flake8|pylint|pip-compile|pip-sync"
		toolre = "^(" tools ")$"
		wraps = "^(command|exec|builtin|env|time|nohup|sudo|doas|xargs|nice|ionice|timeout|gtimeout|caffeinate|stdbuf|arch|then|do|else|elif|if|while|until|!|nocorrect|noglob)$"
		shells = "^(sh|bash|zsh|dash|ksh|fish)$"
		pkgmgr = "^(brew|port|apt|apt-get|aptitude|dnf|yum|pacman|zypper|apk|nix-env|winget|choco|scoop)$"
		pypkg = "^(python|python@|python[0-9]|py[0-9]|pip|pipx|uv|poetry|pyenv|conda|miniconda|anaconda|miniforge|mambaforge|micromamba|mamba)"
		hit = ""; ask = 0; nq = 0; ncmd = 0
	}
	{ text = text $0 "\n" }
	END {
		q[++nq] = text
		for (qi = 1; qi <= nq && hit == ""; qi++) parse(q[qi])
		if (hit != "") print "hit " hit
		if (ask) print "ask"
	}

	function push(s) { if (nq < 200) q[++nq] = s }

	# Index of the ")" closing the "(" just before position i of s (quote aware).
	function close_paren(s, i,    depth, c, sq, dq) {
		depth = 1; sq = 0; dq = 0
		for (; i <= length(s); i++) {
			c = substr(s, i, 1)
			if (sq) { if (c == "\047") sq = 0; continue }
			if (c == "\\") { i++; continue }
			if (dq) { if (c == "\"") dq = 0; continue }
			if (c == "\047") sq = 1
			else if (c == "\"") dq = 1
			else if (c == "(") depth++
			else if (c == ")") { if (--depth == 0) return i }
		}
		return length(s) + 1
	}

	function endword() {
		if (w != "" || wq) { nw++; words[nw] = w; quoted[nw] = wq }
		w = ""; wq = 0
	}
	function endcmd() {
		endword()
		ncmd++
		cmdshell[ncmd] = (nw > 0) ? check() : 0
		nw = 0
	}

	function parse(s,    i, n, c, c2, sq, dq, j, k, delim, strip, line, body, eol, nhd, hd, hdstrip, hdcmd, h) {
		w = ""; wq = 0; nw = 0; sq = 0; dq = 0; nhd = 0
		n = length(s)
		for (i = 1; i <= n; i++) {
			c = substr(s, i, 1); c2 = substr(s, i, 2)
			if (sq) { if (c == "\047") sq = 0; else w = w c; continue }
			if (dq) {
				if (c == "\"") { dq = 0; continue }
				if (c == "\\") { i++; w = w substr(s, i, 1); continue }
				if (c2 == "$(") { j = close_paren(s, i + 2); push(substr(s, i + 2, j - i - 2)); w = w "$()"; i = j; continue }
				if (c == "`") { j = index(substr(s, i + 1), "`"); if (!j) j = n - i + 1; push(substr(s, i + 1, j - 1)); w = w "``"; i += j; continue }
				w = w c; continue
			}
			if (c == "\\") { i++; if (substr(s, i, 1) != "\n") w = w substr(s, i, 1); continue }
			if (c == "\047") { sq = 1; wq = 1; continue }
			if (c == "\"") { dq = 1; wq = 1; continue }
			if (c2 == "$(") { j = close_paren(s, i + 2); push(substr(s, i + 2, j - i - 2)); w = w "$()"; i = j; continue }
			if (c2 == "${") { j = index(substr(s, i), "}"); if (!j) j = n - i + 1; w = w substr(s, i, j); i += j - 1; continue }
			if (c == "`") { j = index(substr(s, i + 1), "`"); if (!j) j = n - i + 1; push(substr(s, i + 1, j - 1)); w = w "``"; i += j; continue }
			if (c == "#" && w == "" && !wq) { while (i < n && substr(s, i + 1, 1) != "\n") i++; continue }
			if (c2 == "<<" && substr(s, i + 2, 1) != "<") {
				endword(); i += 2; strip = 0
				if (substr(s, i, 1) == "-") { strip = 1; i++ }
				while (substr(s, i, 1) ~ /[ \t]/) i++
				delim = ""
				for (; i <= n; i++) {
					c = substr(s, i, 1)
					if (c ~ /[ \t\n;&|<>()]/) break
					if (c != "\047" && c != "\"" && c != "\\") delim = delim c
				}
				i--
				nhd++; hd[nhd] = delim; hdstrip[nhd] = strip; hdcmd[nhd] = ncmd + 1
				continue
			}
			if (c == " " || c == "\t") { endword(); continue }
			if (c == "<" || c == ">") { endword(); w = c; endword(); continue }
			if ((c == "{" || c == "}") && (w != "" || wq || (c == "{" && i < n && substr(s, i + 1, 1) !~ /[ \t\n;]/))) { w = w c; continue }
			if (c == "\n" || c ~ /[;&|(){}]/) {
				endcmd()
				if (c == "\n" && nhd) {
					# Heredoc bodies follow this line: skip them, but scan the
					# body of one fed to a shell.
					for (h = 1; h <= nhd; h++) {
						body = ""
						while (i < n) {
							eol = index(substr(s, i + 1), "\n")
							line = eol ? substr(s, i + 1, eol - 1) : substr(s, i + 1)
							i += eol ? eol : n - i
							if (hdstrip[h]) sub(/^\t+/, "", line)
							if (line == hd[h]) break
							body = body line "\n"
						}
						if (cmdshell[hdcmd[h]]) push(body)
					}
					nhd = 0
				}
				continue
			}
			w = w c
		}
		endcmd()
	}

	# Look at one simple command (words[1..nw]). Returns 1 if it is a shell
	# reading a script from stdin (so a heredoc body is code).
	function check(    i, x, base, wrapper, rest, j, k) {
		wrapper = 0
		for (i = 1; i <= nw; i++) {
			x = words[i]
			if (x ~ /^[A-Za-z_][A-Za-z0-9_]*=/ && !quoted[i]) {
				if (x == "PIXI_GUARD=ask") ask = 1
				continue
			}
			if (x ~ /^[0-9]*[<>]/) { i++; continue }
			sub(/.*\//, "", x)
			if ((x == "command" || x == "builtin" || x == "type" || x == "hash") && words[i + 1] ~ /^-[vVp]+$/) return 0
			if (x ~ wraps) {
				wrapper = 1
				if (x == "sudo" || x == "doas") wrapper = 2
				continue
			}
			if (wrapper && x ~ /^-/) {
				if (wrapper == 2 && x ~ /^-[ugCDhpr]$/) i++
				continue
			}
			if (wrapper && x ~ /^[0-9.]+[smhd]?$/) continue
			break
		}
		if (i > nw) return 0
		base = words[i]; sub(/.*\//, "", base)
		if (base == "pixi") return 0
		if (base ~ shells) {
			for (j = i + 1; j <= nw; j++) {
				if (words[j] ~ /^-[A-Za-z]*c[A-Za-z]*$/) { if (j + 1 <= nw) push(words[j + 1]); return 0 }
				if (words[j] !~ /^-/) return 0
			}
			return 1
		}
		if (base == "eval") {
			rest = ""
			for (j = i + 1; j <= nw; j++) rest = rest " " words[j]
			push(rest); return 0
		}
		if (base ~ toolre || base ~ /\.py$/) { found(base); return 0 }
		if ((base == "source" || base == ".") && words[i + 1] ~ /(^|\/)bin\/activate/) { found("venv activate"); return 0 }
		if (base ~ pkgmgr) {
			for (j = i + 1; j <= nw; j++) if (words[j] ~ pypkg) { found(base " " words[j]); return 0 }
		}
		for (j = i + 1; j < nw; j++) {
			if (words[j] == "-exec" || words[j] == "-execdir" || words[j] == "-ok") {
				rest = ""
				for (k = j + 1; k <= nw && words[k] != ";" && words[k] != "+"; k++) rest = rest " " words[k]
				push(rest)
			}
		}
		return 0
	}
	function found(name) { if (hit == "") hit = name }
	'
}

out_decision() {
	printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"%s","permissionDecisionReason":%s}}\n' \
		"$1" "$(jstr "$2")"
}

case $mode in
session)
	msg="pixi-guard: on this machine Python runs only through pixi. Use \`pixi run ...\` in a pixi workspace, \`pixi exec [--spec pkg] python ...\` for one-offs, \`pixi add pkg\` (conda-forge) or \`pixi add --pypi pkg\` for packages. Never run python, pip, uv, conda, poetry and the like directly: Bash calls that do are refused. For a project that is not on pixi, load the skill pixi:project (convert it, or wrap it if it isn't the user's code)."
	if ! have_pixi; then
		msg="$msg pixi is NOT installed here. The first time Python comes up, tell the user and offer to install it ($PIXI_INSTALL)."
	fi
	printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' "$(jstr "$msg")"
	;;

pre)
	cmd=$(field command)
	[ -n "$cmd" ] || exit 0
	res=$(scan "$cmd")
	hit=$(printf '%s\n' "$res" | sed -n 's/^hit //p')
	[ -n "$hit" ] || exit 0

	if printf '%s\n' "$res" | grep -qx ask; then
		out_decision ask "pixi-guard: this runs \`$hit\` outside pixi. Claude asked to run it this way anyway; allow it?"
		exit 0
	fi

	cwd=$(field cwd)
	[ -n "$cwd" ] || cwd=$PWD
	why="pixi-guard: refused, \`$hit\` would run Python outside pixi."

	if ! have_pixi; then
		sid=$(field session_id | tr -c 'A-Za-z0-9_-' '_' | cut -c 1-128)
		if [ -n "$sid" ] && [ -e "$st/told/$sid" ]; then
			todo="pixi is still not installed (the user has been told). Wait for the user to install it, or, if they said to go ahead without pixi, re-run the command as \`PIXI_GUARD=ask <command>\` so they can approve it."
		else
			mkdir -p "$st/told" 2>/dev/null && [ -n "$sid" ] && : > "$st/told/$sid" 2>/dev/null
			todo="pixi is not installed on this machine. Stop and tell the user now: Python here is meant to run through pixi, and it can be installed with $PIXI_INSTALL. Ask whether to install it (you can run the install for them on a yes) or to go ahead without pixi this time; in that case re-run as \`PIXI_GUARD=ask <command>\` so they approve it."
		fi
		out_decision deny "$why $todo"
		exit 0
	fi

	# Installers and env managers map to `pixi add`; everything else to `pixi run`.
	case $hit in
	pip* | uv | uvx | easy_install* | conda | mamba | micromamba | poetry | pipenv | pyenv | virtualenv | hatch | pdm | rye | "venv activate" | *" "*)
		use="manage packages with \`pixi add <pkg>\` (conda-forge, preferred) or \`pixi add --pypi <pkg>\`, and the environment with \`pixi install\` / \`pixi run\`; no separate venv or installer" ;;
	*)
		use="run it as \`pixi run $hit ...\` (or a task from \`pixi task list\`); add missing packages with \`pixi add <pkg>\` or \`pixi add --pypi <pkg>\`" ;;
	esac

	classify "$cwd"
	case $proj_kind in
	pixi)
		todo="$proj_root is a pixi workspace: $use."
		;;
	wrapped)
		m=$(wrap_manifest "$proj_root")
		todo="$proj_root is wrapped by the pixi workspace $m (kept outside the folder). Use it with \`--manifest-path '$m'\` on every pixi command: $use."
		;;
	python)
		todo="$proj_root is a Python project that is not on pixi. Load the skill pixi:project and follow it: ask the user once whether to convert this project to pixi (their own code) or wrap it without changing tracked files (someone else's code). If nothing may be added to the folder at all, put the wrapper at $(wrap_manifest "$proj_root")."
		;;
	*)
		todo="No pixi workspace here. For a one-off use \`pixi exec --spec python --spec <pkg> python ...\`; for a script, \`pixi init --script file.py\` and \`pixi exec\`; for ongoing work, \`pixi init\` (ask the user before creating a workspace)."
		;;
	esac
	out_decision deny "$why $todo Only if the user explicitly wants it run outside pixi, re-run it as \`PIXI_GUARD=ask <command>\`: that shows them a permission prompt. Don't use that to get around pixi on your own."
	;;
esac
exit 0
