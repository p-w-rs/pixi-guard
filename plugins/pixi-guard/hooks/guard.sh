#!/bin/sh
# pixi-guard hooks. Usage: guard.sh session|pre   (hook payload JSON on stdin)
#                          guard.sh wrap-path [DIR]
#
#   session    SessionStart: print the Python-via-pixi policy as additionalContext,
#              and tell the user when pixi can't be found.
#   pre        PreToolUse (Bash, Monitor, the desktop app's terminal tool,
#              checkpoint's job_start, the gateway's job_start and
#              terminal_send): refuse
#              a command that runs Python or Python tooling outside pixi (or a
#              package installer in a pixi environment: pixi run pip install), with a
#              reason that tells Claude what to do instead. `PIXI_GUARD=ask
#              <command>` turns the refusal into a permission prompt, so running
#              it outside pixi needs the user's yes. A command too big or nested
#              to check within the scanner's budgets is refused the same way, and
#              so is one whose scan is still running after 7 s (watched_scan).
#   wrap-path  print the out-of-folder wrapper manifest for the Python project
#              containing DIR (default: the current folder). For the skill.
#
# POSIX sh + awk only: it has to work on a machine without pixi (or Python).
# Fails open on errors: anything unexpected means no output, and the call goes
# ahead (but a scan that runs out of time is refused, see watched_scan).

mode=$1
case $0 in */*) here=${0%/*} ;; *) here=. ;; esac
[ "$mode" = wrap-path ] || input=$(cat)
# Small state ("told" markers): the plugin's data dir when Claude Code gives one.
st=${PIXI_GUARD_HOME:-${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/pixi-guard}}
# Wrappers must outlive the plugin and be reachable from Claude's Bash tool.
wrapdir=${PIXI_GUARD_WRAP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pixi-guard/wrap}
told=$st/told

# "told" markers only matter within a session: drop those older than a week,
# also the ones 0.1.0 left in the XDG state dir (and that dir once empty).
prune_told() {
	old=${XDG_STATE_HOME:-$HOME/.local/state}/pixi-guard/told
	for d in "$told" "$old"; do
		[ -d "$d" ] && find "$d" -type f -mtime +7 -exec rm -f {} + 2>/dev/null
	done
	[ "$old" = "$told" ] || rmdir "$old" "${old%/told}" 2>/dev/null
	return 0
}

# awk, appended to the programs below that read their input with RS = "\001":
# R[1..NR] joined by the \001 that separated them, pairwise (n log n even with
# a \001 in every other byte; appending each record to the text is quadratic).
RJOIN='
	function rjoin(    n, k, m) {
		for (n = NR; n > 1; n = m) {
			m = 0
			for (k = 1; k <= n; k += 2) R[++m] = (k < n) ? R[k] "\001" R[k + 1] : R[k]
		}
		return NR ? R[1] : ""
	}'

# awk: does record r (of a text read with RS = "\001") hold a PIXI_GUARD=ask
# assignment at the start of a word? (The \001 in front of a later record stands
# for the one that ended the last.) For a command that is not scanned: over
# 256KB, or a scan that ran out of time.
ASKREC='
	function askrec(r) { return index(r, "PIXI_GUARD=") && (NR > 1 ? "\001" r : r) ~ /(^|[;&|( \t\n])PIXI_GUARD=("ask"|\047ask\047|ask)[ \t]/ }'

# The payload's keys the hook reads, and where their values start: "NAME
# OFFSET" items joined by ";", NAME tool_name, cwd or session_id for the
# payload's top level, tool_input.KEY for tool_input's (KEY command, argv,
# text, host or cwd). Only there: a "key" in a nested object, or in
# a string ({"meta": {"host": ...}}, "x\"host"), is another key, which the
# tools ignore. Keys compare decoded; the first one counts (Claude Code's JSON
# has no duplicate keys). One walk, run once (field() and argvcmd() look the
# offsets up), in linear time: split() at the quotes; in a string, a part that
# ends in an odd number of backslashes ends at an escaped quote; outside, a
# part that starts with ":" makes the string before it a key, at the depth
# the brackets before it give. Over 500,000 quotes (a command over 256KB is
# refused unread anyway), or 4KB between two strings with a bracket in it
# (Claude Code's tool inputs have neither), it prints "cut": the payload is
# refused as too large to read. Its time still grows with the payload's size
# (up to about 0.9 s per MB under gawk, half that in BWK awk), and it runs
# before the scan's watchdog starts: a payload over 1MB is not walked at all
# (see pre).
jindex() {
	printf '%s' "$input" | awk '
	BEGIN { RS = "\001"; split("command argv text host cwd", kt, " "); for (k in kt) TK[kt[k]] = 1 }
	{ R[NR] = $0 }
	END {
		text = rjoin()
		# (a one-character separator also splits at newlines in BWK awk)
		n = index(text, "\n") ? split(text, P, /"/) : split(text, P, "\"")
		if (n > 500000) { printf "cut"; exit }
		# off: where P[k] starts; d: the depth; ins: P[k] is in a string, which
		# started at P[ks] (at depth sd); ti: 1 in tool_input, 2 after it
		off = 1; d = ins = ti = lstr = 0
		for (k = 1; k <= n; k++) {
			x = P[k]
			if (ins) {
				m = length(x); off += m + 1
				if (substr(x, m) != "\\" || !oddtail(x)) { ins = 0; lstr = 1; lk = ks; le = k; ld = sd }
				continue
			}
			if (lstr && (ld == 1 || (ld == 2 && ti == 1)) && match(x, /^[ \t\r\n]*:[ \t\r\n]*/)) {
				key = jkey(lk, le); v = off + RLENGTH
				if (ld == 1) {
					if ((key == "tool_name" || key == "cwd" || key == "session_id") && !(key in JI)) JI[key] = v
					else if (key == "tool_input" && !ti && substr(x, RLENGTH + 1, 1) == "{") ti = 1
				}
				else if ((key in TK) && !(("tool_input." key) in JI)) JI["tool_input." key] = v
			}
			lstr = 0
			if (index(x, "{") || index(x, "}") || index(x, "[") || index(x, "]")) {
				# (gsub() is quadratic in its matches in BWK awk)
				if (length(x) > 4096) { printf "cut"; exit }
				y = x; d += gsub(/[{]/, "", y) + gsub(/[[]/, "", y) - gsub(/[}]/, "", y) - gsub(/[]]/, "", y)
				if (ti == 1 && d < 2) ti = 2
			}
			ins = 1; ks = k + 1; sd = d
			off += length(x) + 1
		}
		# (one line: BWK awk takes no newline in a -v value)
		for (k in JI) { printf "%s%s %s", sep, k, JI[k]; sep = ";" }
	}
	# Does x (which ends in a backslash) end in an odd number of them? Its last
	# 64 characters, else a binary search (substr() walks the whole string in
	# BWK awk: no loop over every character).
	function oddtail(x,    n, y, m, c, lo, hi, mid) {
		n = length(x); y = n > 64 ? substr(x, n - 63) : x; m = length(y)
		for (c = 0; c < m && substr(y, m - c, 1) == "\\"; c++) ;
		if (c < m || n <= 64) return c % 2
		# the first of the backslashes at the end: in lo..hi
		lo = 1; hi = n - 63
		while (lo < hi) { mid = int((lo + hi) / 2); if (substr(x, mid) ~ /^\\+$/) hi = mid; else lo = mid + 1 }
		return (n - lo + 1) % 2
	}
	# Key P[a..b] (joined at the escaped quotes between them), decoded; "" if
	# over 256 characters (no key looked for is that long).
	function jkey(a, b,    s, k, n, c, r, v, h, j) {
		s = P[a]; n = length(s)
		for (k = a + 1; k <= b; k++) { n += length(P[k]) + 1; if (n > 256) return ""; s = s "\"" P[k] }
		if (n > 256) return ""
		if (!index(s, "\\")) return s
		r = ""; h = "0123456789abcdef"
		for (k = 1; k <= n; k++) {
			c = substr(s, k, 1)
			if (c == "\\" && k < n) {
				c = substr(s, ++k, 1)
				if (c == "u") { v = 0; for (j = 1; j <= 4; j++) v = v * 16 + index(h, tolower(substr(s, k + j, 1))) - 1; k += 4; c = (v > 0 && v < 128) ? sprintf("%c", v) : "?" }
				else if (c == "n") c = "\n"
				else if (c == "t") c = "\t"
				else if (c == "r") c = "\r"
				else if (c == "b" || c == "f") c = " "
			}
			r = r c
		}
		return r
	}'"$RJOIN"
}
# awk: jat(name) is the offset of that value (see jindex, in $jix), or 0.
JAT='
	function jat(name,    n, k, L, F) {
		n = split(jix, L, ";")
		for (k = 1; k <= n; k++) if (split(L[k], F, " ") == 2 && F[1] == name) return F[2] + 0
		return 0
	}'

# Decoded value of "key": "string" at the payload's top level (with $2, at the
# top level of the object "$2": {...}, as in `field cwd tool_input`); see
# jindex, which must have listed it. Linear time: one match() isolates the
# value, one split() at its backslashes decodes it.
field() {
	printf '%s' "$input" | awk -v key="${2:+$2.}$1" -v jix="$jix" '
	BEGIN { RS = "\001" }
	# Records are kept and joined once (appending each to the text would take
	# quadratic time when the input is full of \001 characters).
	{ R[NR] = $0 }
	END {
		text = rjoin()
		if (!(off = jat(key)) || substr(text, off, 1) != "\"") exit
		rest = substr(text, off + 1)
		if (!match(rest, /^([^"\\]|\\.)*"/)) exit
		s = substr(rest, 1, RLENGTH - 1)
		# Over 6 x 256KB (an escape is at most 6 bytes) it decodes to more than
		# the 256KB scan() reads anyway: decode only that much. (A backstop: pre
		# reads no payload over 1MB.)
		if (RLENGTH > 1600000) s = substr(s, 1, 1600000)
		# A one-character separator is fast, but BWK awk also splits at newlines
		# with it; valid JSON has none in a string, else take the slow regex.
		n = index(s, "\n") ? split(s, p, /\\/) : split(s, p, "\\")
		hex = "0123456789abcdef"; o1 = o2 = o3 = ""; n1 = n2 = 0
		add(p[1])
		for (i = 2; i <= n; i++) {
			x = p[i]
			# An empty part is an escaped backslash; the part after it is plain text.
			if (x == "") { add("\\" p[++i]); continue }
			e = substr(x, 1, 1); r = substr(x, 2)
			if (e == "n") e = "\n"
			else if (e == "t") e = "\t"
			else if (e == "r") e = "\r"
			else if (e == "b" || e == "f") e = " "
			else if (e == "u") {
				h = tolower(substr(x, 2, 4)); v = 0
				for (j = 1; j <= 4; j++) { k = index(hex, substr(h, j, 1)); v = v * 16 + (k ? k - 1 : 0) }
				e = (v > 0 && v < 128) ? sprintf("%c", v) : "?"; r = substr(x, 6)
			}
			add(e r)
		}
		printf "%s", o3 o2 o1
	}
	function add(x) { o1 = o1 x; if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } } }
	'"$RJOIN$JAT"
}

# tool_input's "argv": [ "string", ... ] (a job_start's; see jindex) as
# the command line /bin/sh gets: checkpoint runs `/bin/sh -c
# "$(shlex.join(argv))"`, so each element becomes one word, quoted as
# Python's shlex.quote does it (bare if made of A-Za-z0-9_@%+=:,./- only, so
# a leading NAME=VALUE is an assignment there too; '' if empty; else in
# single quotes, each ' as '"'"'). Empty if there is no list; it ends at the
# first thing that is not a string. Linear time: the list (at most 1.6MB of
# JSON, as field()) is read character by character once, and words grow
# through small buffers. It stops once the line is over 256KB, which scan()
# refuses as too large, and a list cut at 1.6MB before its "]" is padded
# past that size too (nothing after the cut is read; a backstop: pre reads no
# payload over 1MB).
argvcmd() {
	printf '%s' "$input" | awk -v jix="$jix" '
	BEGIN { RS = "\001" }
	{ R[NR] = $0 }
	END {
		text = rjoin()
		if (!(off = jat("tool_input.argv")) || substr(text, off, 1) != "[") exit
		rest = substr(text, off + 1)
		cut = (length(rest) > 1600000)
		if (cut) rest = substr(rest, 1, 1600000)
		if (split("ab", t, "") == 2) n = split(rest, c, "")
		else { n = length(rest); for (k = 1; k <= n; k++) c[k] = substr(rest, k, 1) }
		Q = "\047"; QQ = Q "\"" Q "\"" Q; HEX = "0123456789abcdef"; LIM = 262200
		SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_@%+=:,./-"
		o1 = o2 = o3 = ""; n1 = n2 = 0; tot = 0; nw = 0; done = 0
		for (i = 1; i <= n && tot <= LIM; ) {
			x = c[i]
			if (x == " " || x == "\t" || x == "\n" || x == "\r" || x == ",") { i++; continue }
			if (x != "\"") { done = 1; break }
			# One string: decoded (as field() does) and quoted as it goes.
			w1 = w2 = w3 = ""; m1 = m2 = 0; wl = 0; sf = 1
			for (i++; i <= n && c[i] != "\"" && tot + wl <= LIM; i++) {
				e = c[i]
				if (e == "\\" && i < n) {
					e = c[++i]
					if (e == "n") e = "\n"
					else if (e == "t") e = "\t"
					else if (e == "r") e = "\r"
					else if (e == "b" || e == "f") e = " "
					else if (e == "u") {
						v = 0
						for (j = 1; j <= 4 && i < n; j++) { k = index(HEX, tolower(c[++i])); v = v * 16 + (k ? k - 1 : 0) }
						e = (v > 0 && v < 128) ? sprintf("%c", v) : "?"
					}
				}
				if (!index(SAFE, e)) sf = 0
				if (e == Q) e = QQ
				wl += length(e)
				w1 = w1 e
				if (++m1 >= 64) { w2 = w2 w1; w1 = ""; m1 = 0; if (++m2 >= 64) { w3 = w3 w2; w2 = ""; m2 = 0 } }
			}
			i++
			w = (wl == 0) ? (Q Q) : sf ? (w3 w2 w1) : (Q w3 w2 w1 Q)
			if (nw++) w = " " w
			tot += length(w)
			add(w)
		}
		printf "%s", o3 o2 o1
		# (a list cut short: past 256KB, so scan() refuses it unread)
		if (cut && !done && tot <= LIM) printf "%" (LIM - tot + 1) "s", ""
	}
	function add(x) { o1 = o1 x; if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } } }
	'"$RJOIN$JAT"
}

# JSON string literal of $1. No gsub() replacement strings (backslashes in them
# differ between awks); control characters become \u00XX.
jstr() {
	printf '%s' "$1" | awk '
	BEGIN {
		RS = "\001"
		for (k = 1; k < 32; k++) e[sprintf("%c", k)] = sprintf("\\u%04x", k)
		e["\n"] = "\\n"; e["\t"] = "\\t"; e["\r"] = "\\r"; e["\\"] = "\\\\"; e["\""] = "\\\""
	}
	{ R[NR] = $0 }
	END {
		text = rjoin()
		if (split("ab", t, "") == 2) n = split(text, c, "")
		else { n = length(text); for (k = 1; k <= n; k++) c[k] = substr(text, k, 1) }
		o = ""
		for (k = 1; k <= n; k++) o = o ((c[k] in e) ? e[c[k]] : c[k])
		printf "\"%s\"", o
	}'"$RJOIN"
}

# Where pixi is (PATH first, then the usual install folders); fails if nowhere.
pixi_where() { sh "$here/pixi.sh" --where 2>/dev/null; }

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

# Sets proj_kind (pixi | wrapped | python | none), proj_root and, when wrapped,
# proj_manifest for the physical directory $1. Walks up to (not into) $HOME or /.
# A pixi workspace anywhere on the way wins. Python markers and wrappers count
# only inside the project: up to the git top level when there is one. A wrapper
# at any level counts (first found wins); otherwise the root is the outermost
# marker folder in a git checkout, or the nearest one outside git.
classify() {
	proj_kind=none proj_root= proj_manifest=
	d=$1 inner= outer= wroot= top=
	hp=$(cd "$HOME" 2>/dev/null && pwd -P) || hp=
	while [ -n "$d" ] && [ -d "$d" ]; do
		if is_pixi_dir "$d"; then proj_kind=pixi proj_root=$d; return; fi
		[ "$d" = / ] || [ "$d" = "$hp" ] || [ "$d" = "$HOME" ] && break
		if [ -z "$top" ]; then
			if is_py_dir "$d"; then
				[ -n "$inner" ] || inner=$d
				outer=$d
			fi
			[ -z "$wroot" ] && [ -f "$(wrap_manifest "$d")" ] && wroot=$d
			[ -e "$d/.git" ] && top=$d
		fi
		d=$(dirname "$d")
	done
	if [ -n "$wroot" ]; then
		proj_kind=wrapped proj_root=$wroot proj_manifest=$(wrap_manifest "$wroot")
	elif [ -n "$inner" ]; then
		proj_kind=python
		if [ -n "$top" ]; then proj_root=$outer; else proj_root=$inner; fi
	fi
}

# Out-of-tree wrapper manifest for a project root (used when nothing may be
# added to the project's folder). The name keeps its historic form, with the
# trailing "_" that tr makes of basename's newline, so existing wrappers are
# still found.
wrap_manifest() {
	sum=$(printf '%s' "$1" | cksum | cut -d ' ' -f 1)
	printf '%s/%s-%s/pixi.toml' "$wrapdir" "$(basename "$1" | tr -c 'A-Za-z0-9._-' '_')" "$sum"
}

# Scan a shell command for programs that run Python outside pixi. Prints
# "hit <name>" for the first one found, "complex" if the command is too big
# or nested to check within the work limits, and "ask" if a PIXI_GUARD=ask
# prefix was used. A small shell tokenizer: quotes, comments, $(...),
# backticks, arithmetic, heredocs (also inside $(...)), heredocs and
# echo/printf text piped into a shell, here-strings, redirections, glob
# patterns, sh -c, eval, trap, tmux and screen, wrappers and their options,
# find -exec. Text in quotes or heredocs is not a command, so `git commit -m
# "pip install"` passes. It reads the command as bash does, and again as
# zsh, dash or bash 3.2 do where they would read it differently.
#
# Linear time even in BWK awk (whose substr() and length() walk the whole
# string): the text is split into ch[] once, and words grow through small
# buffers. $(...) and backtick bodies are queued as index ranges of ch[];
# strings that need their own pass (sh -c, eval) go to a second queue. The
# queues and the look-ahead scans have budgets proportional to the input, so
# a command built to blow them up is "complex" rather than a hook timeout
# (which would let it through). It runs in the background: $! is its awk (see
# watched_scan).
scan() {
	printf '%s' "$1" | awk -v tools="${PIXI_GUARD_TOOLS:-}" -v b32="${b32:-0}" -v b52="${b52:-0}" -v vb="${vb:-0}" -v tcd="${tcd:-0}" -v gwp="${gwp:-0}" '
	BEGIN {
		RS = "\001"
		# TCD: the command runs in a folder not known here (a gateway
		# terminal'"'"'s), so a relative *.py path is not looked up (as after cd).
		CDX = (tcd == 1)
		# GWP: the gateway runs it with a PATH of its own (its folders, then
		# /usr/bin:/bin...), not the one bare names are looked up in here, as
		# if it set PATH (see deftool).
		PATHSET = (gwp == 1)
		# (pip-NAME, or pip-VERSION: not pip-check-deps.sh)
		if (tools == "") tools = "python|python[0-9][0-9.]*|pythonw|pypy|pypy[0-9][0-9.]*|pip|pip[0-9][0-9.]*|pipx|pip-[a-z0-9-]+|pip-[0-9][0-9.]*|uv|uvx|easy_install|easy_install-[0-9.]+|conda|mamba|micromamba|poetry|pipenv|pyenv|virtualenv|hatch|pdm|rye|flit|twine|tox|nox|ipython|ipython[0-9]|jupyter|jupyter-[a-z-]+|jupytext|pytest|py\\.test|coverage|coverage-?[0-9][0-9.]*|mypy|black|isort|flake8|pylint|uvicorn|gunicorn|flask|django-admin|streamlit|alembic|celery|torchrun|accelerate|deepspeed|sphinx-build|mkdocs"
		toolre = "^(" tools ")$"
		# Installers and env managers: refused even from a pixi environment (the
		# names the refusal sends to `pixi add`, see the shell).
		instre = "^(pip.*|uv|uvx|easy_install.*|conda|mamba|micromamba|poetry|pipenv|pyenv|virtualenv|hatch|pdm|rye)$"
		# B32: Claude Code runs commands with bash 3.x, which ends some $(...)
		# elsewhere (see cpar32); B52: with bash 5.2 or later, which ends some
		# heredocs in $(...) early (view 4, see body_end). VB: a virtual or
		# conda env is active, so the bare program names are listed for the
		# shell to look up in it (PG).
		B32 = (b32 == 1); B52 = (b52 == 1); VB = (vb == 1)
		# Where pixi keeps tools (see pixibin): ~/.pixi and $PIXI_HOME.
		PXH = ENVIRON["HOME"]; sub(/\/+$/, "", PXH)
		PXP = ENVIRON["PIXI_HOME"]; sub(/\/+$/, "", PXP)
		# Commands that write files (see wargs).
		split("tee sed gsed perl cp gcp mv gmv ln gln install ginstall curl wget", t, " ")
		for (k in t) WRT[t[k]] = 1
		# A console script in a venv or conda env: .venv/bin/uvicorn, ~/miniconda3/bin/flask.
		venvre = "(^|/)(\\.?venv[^/]*|\\.virtualenvs/[^/]+|(mini|ana)conda[0-9]*(/envs/[^/]+)?|(miniforge|mambaforge)[0-9]*(/envs/[^/]+)?)/bin/[^/]+$"
		pypkg = "^(python.*|py[0-9].*|pip[0-9.]*|pipx|pipenv|uv|poetry|pdm|hatch|rye|virtualenv|pyenv(-.*)?|conda|miniconda.*|anaconda.*|miniforge.*|mambaforge.*|micromamba|mamba)$"
		verbs = "^(install|reinstall|upgrade|update|add|in)$"
		STDIN = "^(/dev/stdin|/dev/fd/0|/proc/self/fd/0)$"
		safe = "^[A-Za-z0-9_./=:@%+,-]+$"
		CS = (split("ab", t, "") == 2)
		split("sh bash zsh dash ksh mksh ash yash fish", t, " ")
		for (k in t) SHELL[t[k]] = 1
		split("brew port apt apt-get aptitude dnf yum microdnf pacman zypper apk nix-env winget choco scoop pkg", t, " ")
		for (k in t) PKGM[t[k]] = 1
		# Keywords that come before a command.
		split("then do else elif if while until ! nocorrect noglob coproc -", t, " ")
		for (k in t) KW[t[k]] = 1
		# Wrappers that run the rest of the line as a command. OS = letters of
		# their short options that take an argument, OL = long options (and odd
		# single-dash ones) that take a separate argument.
		split("command exec builtin env time nohup sudo doas xargs nice ionice timeout gtimeout caffeinate stdbuf arch busybox setsid flock lockf strace ltrace xvfb-run taskset chrt script sandbox-exec screen numactl unbuffer entr xcrun", t, " ")
		for (k in t) WR[t[k]] = 1
		OS["exec"] = "a"; OS["env"] = "uCSPLU"; OS["time"] = "fo"; OS["sudo"] = "aCcDgpRrTtUu"; OS["doas"] = "uCa"
		OS["xargs"] = "IdELnPsaJRS"; OS["nice"] = "n"; OS["ionice"] = "cnpPu"; OS["timeout"] = "sk"; OS["gtimeout"] = "sk"
		OS["caffeinate"] = "tw"; OS["stdbuf"] = "ioe"; OS["arch"] = "de"; OS["watch"] = "nq"
		OS["flock"] = "wE"; OS["lockf"] = "t"; OS["strace"] = "abeEIoOpPsSuUX"; OS["ltrace"] = "aAeEFlnopsuxX"
		OS["xvfb-run"] = "efnpsw"; OS["chrt"] = "TPD"; OS["script"] = "cEIOBTmotF"; OS["sandbox-exec"] = "fnpD"
		OS["screen"] = "cehpsStTX"; OS["numactl"] = "ipmNCPw"
		OS["parallel"] = "aCdEIjJLnNPSsW"; OS["watchexec"] = "wWFosdEefji"
		OS["apt"] = "oct"; OS["apt-get"] = "oct"; OS["aptitude"] = "oct"; OS["dnf"] = "cxdeR"; OS["yum"] = "cxdeR"; OS["microdnf"] = "cxdeR"
		OL["env"] = " --unset --chdir --split-string "; OL["time"] = " --format --output "
		OL["sudo"] = " --auth-type --close-from --login-class --chdir --group --host --prompt --chroot --role --type --command-timeout --other-user --user "
		OL["xargs"] = " --arg-file --delimiter --max-args --max-procs --max-chars --process-slot-var "
		OL["nice"] = " --adjustment "; OL["ionice"] = " --class --classdata --pid --pgid --uid "
		OL["timeout"] = " --signal --kill-after "; OL["gtimeout"] = OL["timeout"]
		OL["stdbuf"] = " --input --output --error "; OL["arch"] = " -arch "; OL["watch"] = " --interval --equexit "
		OL["flock"] = " --timeout --conflict-exit-code "; OL["strace"] = " --output "; OL["ltrace"] = " --output "
		OL["xvfb-run"] = " --error-file --auth-file --server-num --xauth-protocol --server-args --wait "
		OL["chrt"] = " --sched-runtime --sched-period --sched-deadline "; OL["screen"] = " -Logfile "
		OL["script"] = " --command --echo --log-in --log-out --log-io --log-timing --logging-format --output-limit "
		OL["numactl"] = " --interleave --preferred --membind --cpunodebind --physcpubind --preferred-many --weighted-interleave "
		OL["parallel"] = " --arg-file --colsep --delimiter --jobs --max-procs --profile --max-args --max-replace-args --sshlogin --sshloginfile --slf --max-chars --workdir --wd --joblog --results --res --tmpdir --timeout --delay --retries --halt --load --memfree --nice --tagstring --tag-string --header --env --basefile --bf --return --transferfile --tf --arg-sep --arg-file-sep --block --block-size --recstart --recend --rpl --ssh --sshdelay --termseq --limit --group-by --semaphore-timeout --semaphorename --id --filter "
		OL["watchexec"] = " --watch --watch-non-recursive --watch-file --on-busy-update --signal --stop-signal --stop-timeout --map-signal --debounce --delay-run --shell --emit-events-to --env --wrap-process --color --project-origin --workdir --exts --filter --filter-file --filter-prog --ignore --ignore-file --fs-events --completions "
		OL["xcrun"] = " -sdk --sdk -toolchain --toolchain "
		# pixi run and pixi exec (see pixi()): their options that take a value.
		OS["pixi run"] = "epmws"; OS["pixi exec"] = "swcp"
		# pip and uv under pixi (see pipargs, uvargs): their commands (uv pip'"'"'s:
		# UVPC), and options that take a value (PIPV, UVV); npm, pnpm, yarn and
		# bun (see npmexec, npx): options that take a value (NPV).
		split("install download uninstall freeze inspect list show check config search cache index wheel hash completion debug help lock", t, " ")
		for (k in t) PIPC[t[k]] = 1
		split("run init add remove version sync lock export tree format tool python pip venv build publish cache self auth help generate-shell-completion", t, " ")
		for (k in t) UVC[t[k]] = 1
		split("compile sync install uninstall freeze list show tree check", t, " ")
		for (k in t) UVPC[t[k]] = 1
		PIPV = " --python --log --log-file --local-log --keyring-provider --proxy --retries --timeout --exists-action --trusted-host --cert --client-cert --cache-dir --src --use-feature --use-deprecated --resume-retries "
		UVV = " --directory --project --config-file --cache-dir --color --python-preference --allow-insecure-host --python -p "
		NPV = " -p --package -w --workspace --prefix -C --dir --filter -F --registry --cache --userconfig --globalconfig --loglevel --location --cwd --node-options --resume-from --workspace-concurrency --reporter --modules-folder --cache-folder --global-folder --network-timeout "
		OL["pixi run"] = " --environment --platform --manifest-path --workspace --script --config-file --auth-file --concurrent-downloads --concurrent-solves --pinning-strategy --pypi-keyring-provider --tls-root-certs --color "
		OL["pixi exec"] = " --spec --with --channel --platform --auth-file --concurrent-downloads --concurrent-solves --pinning-strategy --pypi-keyring-provider --tls-root-certs --color "
		# Options whose value is shell code (script -c CMD).
		CO["script"] = " -c --command "
		# Words between the options and the command: timeout DURATION, flock
		# FILE, taskset MASK, chrt PRIORITY, script FILE.
		split("timeout gtimeout flock lockf taskset chrt script", t, " ")
		for (k in t) NP[t[k]] = 1
		# tmux commands that take a shell command, and the letters of their
		# options that take a value.
		TV["new-session"] = TV["new"] = "cefFnstxy"; TV["new-window"] = TV["neww"] = "ceFnt"
		TV["split-window"] = TV["splitw"] = "celtF"; TV["respawn-pane"] = TV["respawnp"] = "cet"
		TV["respawn-window"] = TV["respawnw"] = "cet"; TV["run-shell"] = TV["run"] = "cdt"
		TV["if-shell"] = TV["if"] = "t"; TV["pipe-pane"] = TV["pipep"] = "t"
		TV["display-popup"] = TV["popup"] = "bcdehsStTwxy"; TV["send-keys"] = TV["send"] = "cNt"
		# Characters that mean something outside quotes.
		n = chars(" \t\n\\\047\"$`#<>{};&|()", t)
		for (k = 1; k <= n; k++) SP[t[k]] = 1
		# Characters of plain arithmetic that mean nothing else to a shell either
		# (no blanks: they split words).
		n = chars("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-*/%^!~=,.:?[]", t)
		for (k = 1; k <= n; k++) AR[t[k]] = 1
		NM1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"; NM = NM1 "0123456789"; HEX = "0123456789abcdef"
		# Escapes of control characters: \n is 10.
		CE["a"] = 7; CE["b"] = 8; CE["t"] = 9; CE["n"] = 10; CE["v"] = 11; CE["f"] = 12; CE["r"] = 13; CE["e"] = CE["E"] = 27
		# Printable ASCII, for {a..e} brace ranges.
		ASC = ""; for (k = 32; k < 127; k++) ASC = ASC sprintf("%c", k)
		hit = ""; ask = 0; nsq = 0
		# printf: the flags, widths and precisions of a directive (see pfmt).
		PFL = "-+ #0123456789.*\047"
		# ECKD: the depth of check() whose echo / printf prints into the
		# command'"'"'s pipeline (1; deeper for what pixi run runs, see pixi()).
		ECKD = 1
		# hit is set to CX when a budget runs out: that stops every loop.
		CX = "\001complex"
	}
	# Over 256KB: "complex" unread, so the worst case stays well inside the
	# hook timeout. Records (split at \001) are kept only up to that size and
	# joined once in END. A PIXI_GUARD=ask anywhere in the text still asks (the
	# \001 in front of a later record stands for the one that ended the last).
	{
		if (!BIG) { if ((TL += length($0) + 1) > 262145) BIG = 1; else R[NR] = $0 }
		if (askrec($0)) BASK = 1
	}
	END {
		nsq = 1; SQ[1] = BIG ? "" : rjoin(); tot = 0
		if (BIG) { toomuch(); ask = BASK }
		for (si = 1; si <= nsq && hit == ""; si++) {
			n = chars(SQ[si], ch); NORB = NOAR = n + 1; NORBH = NOARH = 0
			# Budgets: queued strings (bytes), look-ahead work (characters), and
			# parsing (characters of the ranges parsed, nested ones counted again,
			# and 8 more for each string).
			if (si == 1) { PBMAX = 4 * n + 100000; PWMAX = n + 400000 }
			PW += 8
			tot += n; WKMAX = 8 * tot + 200000
			nr = VIEW = 0; PXM = SQM[si]; rpush(1, n)
			for (ri = 1; ri <= nr && hit == ""; ri++) {
				if ((PW += RHI[ri] - RLO[ri] + 1) > PWMAX) toomuch()
				else {
					VIEW = RV[ri]; FEED = RF[ri]; CSUB = RC[ri]; PXM = PXR = RM[ri]
					# (what runs under pixi sets no PATH, nor a folder, for the rest)
					if (PXM == "") parse(RLO[ri], RHI[ri])
					else { PXS = PATHSET; PXC = CDX; parse(RLO[ri], RHI[ri]); PATHSET = PXS; CDX = PXC }
				}
			}
		}
		VIEW = 0; PXM = ""
		# A *.py script by a relative path in a command that may change folders
		# (cd, env -C, ...) is not looked up: it is refused.
		if (hit == "" && CDX) for (k = 1; k <= NPY; k++) if (PYP[k] !~ /^(\/|~\/)/) { x = PYP[k]; sub(/.*\//, "", x); found(x); PYCD = 1; break }
		# Nor is one the command writes (its #! line is read before it runs).
		if (hit == "" && (NWT || WTA)) for (k = 1; k <= NPY; k++) if (wrote(PYP[k])) { x = PYP[k]; sub(/.*\//, "", x); found(x); PYW = 1; break }
		# PATH set in the command (PATH=... pytest, export PATH=...): where a
		# bare name resolves is not known here (see check1).
		if (hit == "" && NTN && PATHSET) { found(TN[1]); PYP0 = 1; HV = TNS[TN[1]] == "b32" ? 3 : TNS[TN[1]] == "b52" ? 4 : 0 }
		# (a tool by its bare name seen before a budget ran out is still looked up)
		if (hit == CX) { print "complex"; for (k = 1; k <= NTN; k++) print "tool " TN[k] " " TNS[TN[k]] }
		else if (hit != "") {
			print "hit " hit
			if (B32 && HV == 3) print "b32"
			if (HV == 4) print "b52"
			if (PYCD) print "pycd"
			if (PYW) print "written"
			if (PYP0) print "pathset"
		}
		else {
			# Left to the shell: *.py scripts by their paths (let through if their
			# #! line runs pixi), tools by their bare names (let through if they
			# resolve into a pixi environment; with the view that alone saw them,
			# for the note) and bare program names (refused if one is a Python
			# script in the active env), see istool.
			for (k = 1; k <= NPY; k++) print "py " PYP[k]
			for (k = 1; k <= NTN; k++) print "tool " TN[k] " " TNS[TN[k]]
			for (k = 1; k <= NPG; k++) print "prog " PG[k]
		}
		if (ask) print "ask"
	}

	function chars(s, a,    n, k) {
		if (CS) return split(s, a, "")
		n = length(s); split("", a)
		for (k = 1; k <= n; k++) a[k] = substr(s, k, 1)
		return n
	}
	function toomuch() { if (hit == "") hit = CX }
	# Queue string s to be parsed, as code that runs the way the current code
	# does (SQM: under pixi run or pixi exec, see PXM).
	function push(s) {
		if ((PB += length(s) + 1) > PBMAX) toomuch()
		else { SQ[++nsq] = s; SQM[nsq] = PXM }
	}
	# Queue ch[a..b] to be parsed (in the current view and pixi mode, RM).
	# Returns 1 if it did.
	# RC: it is (in) the body of a $(...), <(...) or >(...), where bash 5.2
	# ends heredocs its own way (view 4, see body_end); the caller sets it.
	function rpush(a, b) {
		if (a > b) return 0
		if (nr >= 200000) { toomuch(); return 0 }
		nr++; RLO[nr] = a; RHI[nr] = b; RV[nr] = VIEW; RF[nr] = RC[nr] = 0; RM[nr] = PXM
		return 1
	}
	function spent(n) { if ((WK += n) > WKMAX) toomuch() }
	# PXM: "" for code the shell runs, "run" or "exec" for code that pixi run
	# or pixi exec runs in a pixi environment (where only package installers
	# are refused, see pixi()). As a number (for CMDSH, PSH, RF): 1, 2, 3.
	# PXR: the mode of the range being parsed (in pixi(), PXM is pixi run'"'"'s).
	function mcode() { return PXM == "" ? 1 : PXM == "run" ? 2 : 3 }
	function rcode() { return PXR == "" ? 1 : PXR == "run" ? 2 : 3 }
	function mname(c) { return c == 2 ? "run" : c == 3 ? "exec" : "" }
	function nx(k) { return k <= HI ? ch[k] : "" }

	# The word being built: w, wm, wb are buffers (so a long word costs linear
	# time), wn counts what went in, wq says part of it was quoted, wp is the
	# part before the first quote (to tell NAME="v" assignments).
	function wadd(x) {
		wb = wb x; wn++
		if (++wbn >= 64) { wm = wm wb; wb = ""; wbn = 0; if (++wmn >= 64) { w = w wm; wm = ""; wmn = 0 } }
	}
	function wget() { return w wm wb }
	function wreset() { w = wm = wb = ""; wn = wbn = wmn = wq = WBR = WDX = WNA = 0 }
	function qstart() { if (!wq) { wp = wget(); wq = 1 } }
	# WBR counts the "{" in the word not yet matched by a "}". PR[k]: the range
	# queued for word k when it is just one $(...), <(...) or >(...) (see
	# feed() and shin()).
	# DX[k]: word k has a ${...} that is not in single quotes (WDX, see brace()).
	# WNA: a "(" went into the word (a glob group, an array, =(...)), so it can
	# no longer be "=", "!" or NAME= (parse() then need not look at it again).
	function endword(    x) {
		if (wn || wq) {
			nw++; WD[nw] = x = w wm wb; PRE[nw] = wq ? wp : x; OP[nw] = 0; DX[nw] = WDX; w = wm = wb = ""; wn = wbn = wmn = wq = WBR = WDX = WNA = 0
			PR[nw] = (PSPEND && (x == "$()" || x == "<()" || x == ">()")) ? PSPEND : 0; PSPEND = 0
			cpos(nw > 1 && OP[nw - 1])
			if (cw || cs || dbr || x == "case" || x == "[[") casew()
			# zsh: a command starts after the "]]" of [[ ... ]] (if [[ x ]] cmd).
			if (DBE) { DBE = 0; endcmd() }
		}
	}
	# Is a "{" or "}" after word nw in command position? AK[k]: words 1..k are
	# all keywords (a "}" there closes a group). LX[k]: 1 keywords or
	# redirections, 2 after time, coproc, function or repeat (a "{" there
	# opens one), 0 neither. o: the word is a redirection target.
	function cpos(o,    x, p) {
		x = WD[nw]; p = nw > 1 ? LX[nw - 1] : 1
		AK[nw] = (nw == 1 || AK[nw - 1]) && !o && !OP[nw] && PRE[nw] == x && (x in KW)
		if (p != 1) LX[nw] = p
		else if (o || OP[nw]) LX[nw] = 1
		else if (PRE[nw] != x) LX[nw] = 0
		else if (x == "time" || x == "coproc" || x == "function" || x == "repeat" || x == "always") LX[nw] = 2
		else LX[nw] = (x in KW) ? 1 : 0
	}
	# case WORD in PATTERN) ... ;; esac: the patterns are not commands. cs counts
	# open case statements, cw is 1 after "case" and 2 after its word, and pat
	# is set while a pattern is read (its words are dropped at ")" or "|").
	# Also [[ ... ]]: dbr is set inside, where ( ) | < > are part of words
	# ([[ $x =~ ^(pip|uv)$ ]] runs nothing); dbp counts open "(" there.
	function casew(    x) {
		x = WD[nw]
		if (PRE[nw] == x) {
			if (x == "[[" && !pat && (nw == 1 || WD[nw - 1] in KW)) { dbr = 1; dbp = 0 }
			else if (x == "]]" && dbr) { dbr = 0; DBE = 1 }
		}
		if (cw == 1) { cw = 2; return }
		if (cw == 2) { cw = 0; if (x == "in" && PRE[nw] == x) { nw = 0; pat = 1 } return }
		if (PRE[nw] != x) return
		if (x == "esac" && nw == 1) { cs--; pat = 0 }
		else if (!pat && x == "case" && (nw == 1 || WD[nw - 1] in KW)) { cw = 1; cs++ }
	}
	function opword(x) { endword(); nw++; WD[nw] = x; PRE[nw] = ""; OP[nw] = 1; PR[nw] = DX[nw] = 0; cpos(0) }
	function lit(a, b) { for (; a <= b; a++) wadd(ch[a]) }
	# End the simple command being built and check it. CMDSH: is it a shell
	# reading stdin, or does it print into one (>(bash), PSUB): 0, else that
	# shell'"'"'s mode (see check); PIPE: its pipeline. p is 1 at a
	# pipe (the next command is in the same pipeline), 2 at a "(" "{" ")" "}" (a
	# group is a stage of the pipeline it is in), else 0. pp: the last one was a
	# pipe. gd: open groups.
	function endcmd(p,    r) {
		if (wn || wq) endword()
		ncmd++; ECHOI = PSUB = 0
		r = nw ? check(1, nw) : 0
		CMDSH[ncmd] = r; PIPE[ncmd] = curpipe
		# echo / printf (or a command reading a here-string: cat <<< "pip list")
		# whose output may reach a shell: keep its words for flush().
		# (r: echo x > >(bash), tee >(bash) <<< x, see check1; a shell reading
		# stdin takes its here-string as code itself, see stdin)
		if (p == 1 || gd || FEED || r) { if (ECHOI) echoargs(ECHOI); else if (HSN && (!r || PSUB)) hsargs() }
		HSN = 0
		# A group followed by a pipe: (cat <<EOF; ls) | bash.
		if (GL) { if (p == 1) relabel(GL, curpipe); GL = 0 }
		if (!p) curpipe = ncmd + 1
		pp = (p == 1); nw = 0
		if (!p && !gd && nb) flush()
	}
	# ( or { opening a group. GIN: it reads a pipe (... | (bash)).
	function gopen(    gin) {
		gin = pp && !nw && !wn && !wq
		endcmd(2); gd++; GS[gd] = ncmd + 1; GIN[gd] = gin
	}
	# ) or } closing one: its commands go in the pipeline the group is in.
	function gclose(    g) {
		endcmd(2)
		if (!gd) return
		g = GS[gd]
		if (GIN[gd]) { curpipe = PIPE[g - 1]; relabel(g, curpipe) }
		gd--; GL = g
	}
	function relabel(g, v,    k) { spent(ncmd - g + 1); for (k = g; k <= ncmd && hit == ""; k++) PIPE[k] = v }

	# Tokenize ch[lo..hi] into simple commands and check each one, the way
	# shell VIEW reads them: 0 bash 5 (and the others where they agree),
	# 1 zsh, 2 dash, 3 bash 3.2. Where one of them would read the text
	# differently (see brace_end, $[), a view-0 pass queues it for that one.
	function parse(lo, hi,    i, c, d, j, k, sq, dq, x) {
		HI = hi; nw = 0; nhd = 0; ncmd = 0; curpipe = 1; wreset(); sq = dq = 0; cs = cw = pat = dbr = 0
		gd = GL = pp = nb = NEA = ALT1 = ALT2 = ALT3 = ALT4 = PSPEND = HSN = DBE = ARS = 0
		for (i = lo; i <= hi && hit == ""; i++) {
			c = ch[i]
			# (wadd() inlined on the two hot paths)
			if (sq) {
				if (c == "\047") sq = 0
				else { wb = wb c; wn++; if (++wbn >= 64) { wm = wm wb; wb = ""; wbn = 0; if (++wmn >= 64) { w = w wm; wm = ""; wmn = 0 } } }
				continue
			}
			if (dq) {
				if (c == "\"") { dq = 0; continue }
				if (c == "\\") {
					d = nx(i + 1)
					if (d == "\n") { i++; continue }
					if (d == "$" || d == "`" || d == "\"" || d == "\\") { wadd(d); i++; continue }
				} else if (c == "$") {
					d = nx(i + 1)
					if (d == "(") { i = dollar_paren(i); continue }
					if (d == "{") { i = brace(i, 1); continue }
				} else if (c == "`") { i = backtick(i); continue }
				wadd(c); continue
			}
			if (!(c in SP)) { wb = wb c; wn++; if (++wbn >= 64) { wm = wm wb; wb = ""; wbn = 0; if (++wmn >= 64) { w = w wm; wm = ""; wmn = 0 } } continue }
			if (c == " " || c == "\t") { endword(); continue }
			if (c == "\\") { if (nx(i + 1) == "\n") { i++; continue } qstart(); if (i < hi) wadd(ch[++i]); continue }
			if (c == "\047") { qstart(); sq = 1; continue }
			if (c == "\"") { qstart(); dq = 1; continue }
			if (c == "$") {
				d = nx(i + 1)
				if (d == "(") { i = dollar_paren(i); continue }
				if (d == "{") { i = brace(i); continue }
				# $'"'"'...'"'"': its escapes decoded ($'"'"'\x70ip'"'"' is pip).
				if (d == "\047") { qstart(); j = ansi_end(i + 1); if (j > hi) j = hi + 1; wadd(dec(ch, i + 2, j - 1, 1)); i = j; continue }
				if (d == "\"") { qstart(); dq = 1; i++; continue }
				# $[ ... ]: old arithmetic in bash and zsh, where "<<" is a shift, not a
				# heredoc. dash has no $[ (nor does anyone without its "]"): to it, it
				# is plain text (view 2).
				if (d == "[" && VIEW != 2 && (j = bracket_end(i + 1)) <= hi) {
					substscan(i + 2, j - 1)
					if (!VIEW && !BAR) ALT2 = 1
					lit(i, j); i = j; continue
				}
				wadd(c); continue
			}
			if (c == "`") { i = backtick(i); continue }
			if (c == "#") { if (wn || wq) { wadd(c); continue } while (i < hi && ch[i + 1] != "\n") i++; continue }
			if (dbr && (c == "(" || c == ")" || c == "<" || c == ">" || c == "|")) {
				# An unquoted "]]" right before one still ends [[ ... ]]: [[ x ]]|cmd,
				# ([[ x ]]). So does a ")" with no "(" in it, or a "|" outside ( )
				# next to a blank: dash has no [[ (to it, [[ x | pip ]] is a pipe),
				# and bash and zsh refuse them ([[ $x =~ a|b ]] is a regex).
				if ((wn == 2 && !wq && wget() == "]]") || (c == ")" && !dbp) ||
				    (c == "|" && !dbp && nx(i + 1) != "|" && ((!wn && !wq) || nx(i + 1) == " " || nx(i + 1) == "\t" || nx(i + 1) == "\n"))) { endword(); dbr = 0 }
				else if (c != "|" || nx(i + 1) != "|") { if (c == "(") dbp++; else if (c == ")") dbp--; wadd(c); continue }
			}
			if (c == "<" || c == ">") { i = redir(i); continue }
			if (c == "{") {
				# A group opens at a "{" that is a whole word in command position (also
				# after time, coproc, function NAME, repeat N). zsh: also at one that
				# starts a command word ({python3 z}): view 1.
				if (!wn && !wq && !pat && !dbr && (!nw || LX[nw])) {
					if (bound(i + 1) || VIEW == 1) { gopen(); continue }
					if (!VIEW) ALT1 = 1
				}
				WBR++; wadd(c); continue
			}
			if (c == "}") {
				# It closes one where it is a whole word in command position; elsewhere
				# it is a plain character ("echo }#" prints "}#"). zsh: a "}" before a
				# blank or an operator also closes an open group anywhere (echo a}).
				if (bound(i + 1) && !pat && !dbr) {
					if (!wn && !wq && (!nw || AK[nw])) { gclose(); continue }
					if (gd && !WBR) { if (VIEW == 1) { endword(); gclose(); continue } if (!VIEW) ALT1 = 1 }
				}
				if (WBR) WBR--
				wadd(c); continue
			}
			# Left: newline ; & | ( )
			if (pat) {
				# In a case pattern: "|" separates, ")" ends it, "(" leads it or is extglob.
				if (c == "(") { if (wn) i = xglob(i); continue }
				endword()   # (may be "esac", which ends the case)
				if (pat) {
					if (c == "\n") { if (nhd) i = bodies(i); continue }
					if (c == "|" || c == ")") { nw = 0; if (c == ")") pat = 0; continue }
					pat = 0
				}
			}
			if (c == ";" && cs && (nx(i + 1) == ";" || nx(i + 1) == "&")) {
				# ;; ;& ;;& end a case item: a pattern comes next.
				endcmd(); i++; if (nx(i + 1) == "&") i++
				pat = 1; continue
			}
			# A "(" in a word, or after a word in a simple command: an extglob
			# pattern (bash: @(a|b), !(*.py)), a zsh glob ((a|b).py, *(e:CODE:)) or
			# a syntax error, never a subshell. Not name() (a function), nor zsh'"'"'s
			# =(CMD) (it runs CMD) and for NAME (WORDS); nor "!" in command position:
			# !(cmd) is "!" and a subshell in bash without extglob and in dash
			# (zsh, view 1, reads a pattern).
			if (c == "(" && (wn || wq || (nw && !LX[nw] && !(nw == 2 && WD[1] ~ /^(for|foreach|select)$/ && PRE[1] == WD[1])))) {
				# (not the whole word again at each group of @(a)@(b)...: WNA)
				x = (wq || WNA) ? "" : wget()
				if (x == "=") { j = close_paren(i + 1); rpush(i + 1, j - 1); wadd("()"); WNA = 1; i = j; continue }
				if (x == "!" && wn == 1 && (!nw || LX[nw]) && VIEW != 1) { if (!VIEW) ALT1 = 1 }
				# (a function name is the first word: f() or f ())
				else if (x !~ /^[A-Za-z_][A-Za-z0-9_]*\+?=$/ && !((wn ? !nw || LX[nw] : nw == 1 || LX[nw - 1]) && fdef(i))) { i = xglob(i); continue }
			}
			if (c == "(" && !wq) {
				# name=(array words). An operator or a "(" in them is a syntax error:
				# bash drops the rest of that line (even a quote opened after it) and
				# goes on with the next one. <(...) runs. So read the words as
				# commands too, and the text from the next line on (once per range).
				if (wn && !WNA && wget() ~ /^[A-Za-z_][A-Za-z0-9_]*\+?=$/) {
					j = close_paren(i + 1); substscan(i + 1, j - 1, 1)
					if (SOP && rpush(i + 1, j - 1)) RC[nr] = CSUB
					if (SOPP && !ARS) {
						for (k = SOPP; k < HI && ch[k] != "\n"; k++) ;
						ARS = 1; spent(k - SOPP)
						if (k < HI && rpush(k + 1, HI)) RC[nr] = CSUB
					}
					wadd("()"); WNA = 1; i = j
					# zsh ends the word at its ")": a=(1)cmd runs cmd (view 1).
					if (!bound(i + 1)) { if (VIEW == 1) endword(); else if (!VIEW) ALT1 = 1 }
					continue
				}
				# (( arithmetic ))
				if (!wn && nx(i + 1) == "(" && (j = arith_end(i + 2))) { endcmd(); substscan(i + 2, j - 2); i = j; continue }
			}
			if (c == "|") {
				d = nx(i + 1)
				if (d == "|") { endcmd(); i++; continue }   # ||
				endcmd(1)                                   # a pipe: same pipeline
				if (d == "&") i++                           # |&
				continue
			}
			if (c == "(") { gopen(); continue }
			if (c == ")") { gclose(); continue }
			# [[ ... ]] does not go past ; & or a newline where [[ is not a keyword
			# (dash), even if no "]]" was seen.
			if (c != "&" || (nx(i + 1) != "&" && ch[i - 1] != "&")) dbr = 0
			# A pipe at the end of a line: the pipeline goes on after the heredoc bodies.
			if (c == "\n" && pp && !nw && !wn && !wq) { if (nhd) i = bodies(i); continue }
			endcmd()
			if (c == "\n" && nhd) i = bodies(i)
		}
		# A case pattern with no ")" (bash 3.2 ends $(case x in a) ... there) is
		# a syntax error: its words are no command.
		if (pat) { endword(); nw = 0 }
		endcmd()
		if (nb) flush()
		# Another shell reads part of it differently: read it all again its way.
		if (!VIEW) {
			if (ALT1) { VIEW = 1; if (rpush(lo, hi)) { RF[nr] = FEED; RC[nr] = CSUB } }
			if (ALT2) { VIEW = 2; if (rpush(lo, hi)) { RF[nr] = FEED; RC[nr] = CSUB } }
			if (ALT3) { VIEW = 3; if (rpush(lo, hi)) { RF[nr] = FEED; RC[nr] = CSUB } }
			if (ALT4) { VIEW = 4; if (rpush(lo, hi)) { RF[nr] = FEED; RC[nr] = CSUB } }
			VIEW = 0
		}
	}

	# $( ... ) or $(( ... )) at i (the "$"). Returns the index of the closing ")".
	# PSPEND: the range queued for it, if it starts a word (see feed()).
	function dollar_paren(i,    j, w0) {
		w0 = !wn; j = subst(i); if (w0 && SR) PSPEND = SR
		wadd("$()"); return j
	}
	# Queue the $( ... ) at k (the "$"), or the substitutions in $(( ... )).
	# Returns the index of its last character. SR: the range queued for it.
	function subst(k,    j) {
		SR = 0
		if (nx(k + 2) == "(" && (j = arith_end(k + 3))) { substscan(k + 3, j - 2); SR = 0; return j }
		j = close_sub(k + 2, 1); if (rpush(k + 2, j - 1)) { SR = nr; RC[nr] = 1 } return j
	}
	# The ")" closing the $( (cm 1) or <( >( whose body starts at k. When
	# Claude Code runs bash 3.x (B32), view 3 reads it as bash 3.2 does
	# (cpar32), and view 0 asks for view 3 where that ends it elsewhere (only
	# looked at if cpar saw something bash 3.2 reads otherwise: D32). Not for
	# everyone: bash 5 and zsh users write such text in commit messages.
	function close_sub(k, cm,    j, e) {
		if (!B32) return close_paren(k)
		if (VIEW == 3) { e = cpar32(k, cm); spent(e - k); return e }
		D32 = 0; j = close_paren(k)
		if (!VIEW && !ALT3 && D32 && hit == "") { e = cpar32(k, cm); spent(e - k); if (e != j) ALT3 = 1 }
		return j
	}
	# Where bash 3.2 (macOS /bin/bash) ends the $( ... ) whose body starts at k
	# (HI + 1 if it does not), as it reads it when it runs the command
	# (extract_delimited_string): quotes (also $'"'"'...'"'"'), backslashes,
	# backticks, nested ( ) and (cm) comments count; heredocs, case patterns
	# and ${...} do not. So a quote character in a quoted heredoc body opens a
	# quote, and after a pair of them a ")" can end the $( early: the rest is
	# read as the rest of the word (in "...", where backticks and $(...) run)
	# or as commands.
	function cpar32(k, cm,    d, c, r, wd) {
		if (hit != "") return HI + 1
		if (++CPD > 200) { toomuch(); CPD--; return HI + 1 }
		d = 1; r = HI + 1; wd = 0
		for (; k <= HI && hit == ""; k++) {
			c = ch[k]
			if (c == "\\") k++
			else if (c == "(") d++
			else if (c == ")") { if (--d == 0) { r = k; break } }
			else if (c == "\047") { if (wd) k = ansi_end(k); else for (k++; k <= HI && ch[k] != "\047"; k++) ; }
			else if (c == "`") { for (k++; k <= HI && ch[k] != "`"; k++) if (ch[k] == "\\") k++ }
			else if (c == "\"") k = dq32(k + 1)
			else if (cm && c == "#" && (ch[k - 1] == " " || ch[k - 1] == "\t" || ch[k - 1] == "\n")) { while (k < HI && ch[k + 1] != "\n") k++ }
			wd = (c == "$")
		}
		CPD--
		return hit == "" ? r : HI + 1
	}
	# The closing quote of "..." from k, as cpar32 skips it (skip_double_quoted).
	function dq32(k,    c) {
		for (; k <= HI && hit == ""; k++) {
			c = ch[k]
			if (c == "\\") k++
			else if (c == "`") { for (k++; k <= HI && ch[k] != "`"; k++) if (ch[k] == "\\") k++ }
			else if (c == "$" && nx(k + 1) == "(") k = cpar32(k + 2, 1)
			else if (c == "$" && nx(k + 1) == "{") k = brace_end(k, 1, 0, 0)
			else if (c == "\"") return k
		}
		return HI + 1
	}
	# ${ ... } at i (the "$"): kept as text, but command substitutions inside
	# run. If another shell would end it elsewhere, the text is read again
	# that shell'"'"'s way (ALT1..3, see parse).
	function brace(i, indq,    e, j) {
		e = brace_end(i, indq, VIEW, 1)
		j = e > HI ? HI : e
		if (!VIEW && BALT && brace_end(i, indq, 1, 0) != e) { ALT1 = 1; if (indq) ALT2 = 1 }
		WDX = 1; lit(i, j)
		return j
	}
	# Where ${ ... } at i ends (HI + 1 if it does not), as shell view v reads
	# it (see parse); q: queue the command substitutions in it. Quotes inside
	# count: ${x:-"}"} is one expansion, and in bash so is "${x:-'"'"'}'"'"'}", where
	# $(...) and `...` in the single quotes still run, and $'"'"'...'"'"' is read too.
	# zsh and dash (lsq) take those as plain characters in "${...}". Only the
	# opening "{" and a nested "${" outside quotes nest (${x:-{} is one
	# expansion), but zsh (zb) nests a bare "{" outside "...". BALT: it holds
	# something read differently by another view. BOP: it holds a ( ) # or <<
	# that bash 3.2 would act on in $(...), where it does not skip ${...}.
	# Like every look-ahead, it stops once a budget is spent (hit set: nested
	# ${...$(${...})} read in two views would take exponential time).
	function brace_end(i, indq, v, q,    j, depth, c, d, sq, dq, lsq, zb, balt, bop) {
		lsq = indq && (v == 1 || v == 2); zb = !indq && v == 1
		depth = 0; sq = dq = balt = bop = 0
		for (j = i + 1; j <= HI && hit == ""; j++) {
			c = ch[j]
			if (sq && !indq) { if (c == "\047") sq = 0; continue }
			if (c == "`") { j = q ? tick(j, HI) : tick_end(j, HI); continue }
			if (c == "$") {
				d = nx(j + 1)
				if (d == "(") {
					if (q) j = subst(j)
					else {
						# (awk recursion: cpar may come back here)
						if (++CPD > 200) { toomuch(); return HI + 1 }
						j = close_paren(j + 2); CPD--
					}
				}
				else if (d == "{") { if (!sq && !dq) depth++; j++ }
				else if (!sq && !dq && !lsq && d == "\047") { j = ansi_end(j + 1); if (indq) balt = 1 }
				continue
			}
			if (sq) { if (c == "\047") sq = 0; continue }
			if (c == "\\") { j++; continue }
			if (dq) { if (c == "\"") dq = 0; continue }
			if (c == "\047") { if (!lsq) { sq = 1; if (indq) balt = 1 } }
			else if (c == "\"") dq = 1
			else if (c == "{") { if (j == i + 1 || zb) depth++; else if (!indq) balt = 1 }
			else if (c == "}") { if (--depth == 0) break }
			else if (c == "(" || c == ")" || (c == "#" && wstart(j)) || (c == "<" && nx(j + 1) == "<")) bop = 1
		}
		if (!q) spent(j - i)
		BALT = balt; BOP = bop
		return hit == "" ? j : HI + 1
	}
	# Index of the "'"'"'" closing the $'"'"'...'"'"' quote that opens at k (or HI + 1).
	function ansi_end(k) {
		if (hit != "") return HI + 1
		for (k++; k <= HI; k++) { if (ch[k] == "\\") k++; else if (ch[k] == "\047") return k }
		return HI + 1
	}
	# Index of the "]" closing the "[" at k (or HI + 1). BAR: all plain arithmetic.
	# NORB..NORBH holds no "]" (so "echo $[ ;" repeated stays linear).
	function bracket_end(k,    depth, c, s, rb) {
		if (hit != "" || (k >= NORB && HI <= NORBH)) return HI + 1
		depth = rb = 0; s = k; BAR = 1
		for (; k <= HI; k++) {
			c = ch[k]
			if (!(c in AR)) BAR = 0
			if (c == "\\") { k++; continue }
			if (c == "[") depth++
			else if (c == "]") { rb = 1; if (--depth == 0) break }
		}
		spent(k - s)
		if (!rb) { NORB = s; NORBH = HI }
		return k
	}
	function backtick(i,    j) { j = tick(i, HI); wadd("``"); return j }
	# A pattern "( ... )" at i (an extglob or zsh glob group): read to its
	# ")" as bash reads @( ... ) (quotes count, ${ does not: @(${x:-) ends at
	# that ")"), its $(...) and `...` queued, and zsh'"'"'s glob qualifiers that
	# run code ((e:CODE:), (+CMD)) queued as code. Returns the index of its ")".
	# With no ")" it is no pattern (bash: a syntax error), and the rest of the
	# text is read as commands too (queued), not taken as part of the word.
	function xglob(i,    j, c, d, s) {
		if (hit != "") return HI
		d = 0; s = i
		for (j = i; j <= HI; j++) {
			c = ch[j]
			if (c == "\\") j++
			else if (c == "\047") { for (j++; j <= HI && ch[j] != "\047"; j++) ; }
			else if (c == "\"") { for (j++; j <= HI && ch[j] != "\""; j++) if (ch[j] == "\\") j++ }
			else if (c == "`") j = tick_end(j, HI)
			else if (c == "$" && nx(j + 1) == "\047") j = ansi_end(j + 1)
			else if (c == "(") d++
			else if (c == ")" && --d == 0) break
		}
		spent(j - s); WNA = 1
		if (j > HI) { if (rpush(i + 1, HI)) { RF[nr] = FEED; RC[nr] = CSUB } lit(i, i); return HI }
		substscan(i + 1, j - 1, 1)
		if (bound(j + 1)) zqual(i + 1, j - 1)
		lit(i, j)
		return j
	}
	# zsh glob qualifiers in ch[a..b] that run code: e and a delimiter that is
	# not a letter, digit or "|" (e:CODE:, e[CODE]), and +NAME.
	function zqual(a, b,    k, c, d, e, s) {
		for (k = a; k <= b && hit == ""; k++) {
			c = ch[k]
			if (c == "e" && k < b && !index(NM "|", ch[k + 1])) {
				d = ch[k + 1]; e = d == "[" ? "]" : d == "{" ? "}" : d == "<" ? ">" : d == "(" ? ")" : d
				for (s = k + 2; s <= b && ch[s] != e; s++) ;
				if (s > k + 2) push(unq(k + 2, s - 1))
				k = s
			}
			else if (c == "+" && k < b && index(NM1, ch[k + 1])) {
				for (s = k + 1; s <= b && index(NM, ch[s]); s++) ;
				push(unq(k + 1, s - 1)); k = s - 1
			}
		}
	}
	# ch[a..b] without its quote characters.
	function unq(a, b,    c, s1, s2, s3, n1, n2) {
		s1 = s2 = s3 = ""; n1 = n2 = 0
		for (; a <= b; a++) {
			c = ch[a]; if (c == "\047" || c == "\"") continue
			s1 = s1 c
			if (++n1 >= 64) { s2 = s2 s1; s1 = ""; n1 = 0; if (++n2 >= 64) { s3 = s3 s2; s2 = ""; n2 = 0 } }
		}
		return s3 s2 s1
	}
	# Is the "(" at k the "()" of a function definition (name () ...)?
	function fdef(k,    s) {
		if (hit != "") return 0
		s = k
		for (k++; k <= HI && (ch[k] == " " || ch[k] == "\t"); k++) ;
		spent(k - s)
		return k <= HI && ch[k] == ")"
	}
	# Queue the body of the backtick substitution at k; returns its closing index.
	# A body with \` or \$ is unescaped first: bash runs `echo \`pip -V\`` and
	# `echo \$(pip -V)` there.
	function tick(k, lim,    j) {
		j = tick_end(k, lim)
		if (TB) push(unesc(k + 1, j - 1)); else rpush(k + 1, j - 1)
		return j
	}
	# Index of the backtick closing the one at i (or lim + 1). TB: saw \` or \$.
	function tick_end(i, lim) {
		TB = 0
		if (hit != "") return lim + 1
		for (i++; i <= lim; i++) {
			if (ch[i] == "\\") { i++; if (ch[i] == "`" || ch[i] == "$") TB = 1 }
			else if (ch[i] == "`") return i
		}
		return lim + 1
	}
	# ch[a..b] with the backslash dropped from \\, \` and \$ (as bash reads a
	# backtick body).
	function unesc(a, b,    c, d, s1, s2, s3, n1, n2) {
		s1 = s2 = s3 = ""; n1 = n2 = 0
		for (; a <= b; a++) {
			c = ch[a]
			if (c == "\\" && a < b) { d = ch[a + 1]; if (d == "\\" || d == "`" || d == "$") { c = d; a++ } }
			s1 = s1 c
			if (++n1 >= 64) { s2 = s2 s1; s1 = ""; n1 = 0; if (++n2 >= 64) { s3 = s3 s2; s2 = ""; n2 = 0 } }
		}
		return s3 s2 s1
	}
	# Index of the "))" ending arithmetic that starts at k (just after "(("),
	# or 0 if the parens do not close as "))" (then it is a subshell in a subshell).
	# NOAR..NOARH holds no ")" (so "echo $(( ;" or "((" repeated stays linear).
	function arith_end(k,    depth, c, s, r, l) {
		if (hit != "" || (k >= NOAR && HI <= NOARH)) return 0
		depth = 2; s = k; r = l = 0
		for (; k <= HI; k++) {
			c = ch[k]
			if (c == "\\") { if (ch[++k] == ")") l = k; continue }
			if (c == "(") depth++
			else if (c == ")") { l = k; if (--depth == 1) { if (nx(k + 1) == ")") r = k + 1; break } }
		}
		spent(k - s)
		# Ran out of text: none of the starts after the last ")" closes either.
		if (k > HI) { NOAR = l ? l + 1 : s; NOARH = HI }
		return r
	}
	# Index of the ")" closing the "(" just before k (or HI + 1).
	function close_paren(k,    j) { j = cpar(k); spent(j - k); return j }
	# cpar: close_paren without the budget. Aware of quotes ('"'"'...'"'"', "...",
	# $'"'"'...'"'"'), comments, backticks, arithmetic and heredocs, whose bodies may
	# hold any characters (Claude Code writes commit messages as
	# "$(cat <<'"'"'EOF'"'"' ... EOF\n)").
	# A case statement'"'"'s pattern ")" does not close it: cs counts open case
	# statements, cw waits for "in", pat is set in a pattern (pst: at its start,
	# where a "(" may lead it; pd: extglob parens in it). Heredocs waiting for
	# their bodies are PDL[pb+1..NPD] (a stack shared with nested calls). D32
	# is set where bash 3.2 reads the text otherwise (see close_sub).
	function cpar(k,    depth, c, sq, dq, j, pb, cs, cw, pat, pst, pd) {
		depth = 1; sq = dq = 0; pb = NPD; cs = cw = pat = pst = pd = 0
		for (; k <= HI && hit == ""; k++) {
			c = ch[k]
			if (sq) { if (c == "\047") sq = 0; continue }
			if (c == "\\") { k++; continue }
			if (dq) {
				if (c == "\"") dq = 0
				else if (c == "`") k = tick_end(k, HI)
				else if (c == "$" && nx(k + 1) == "(") {
					# (awk recursion is bounded: a deeper nest is "complex")
					if (++CPD > 200) { toomuch(); NPD = pb; return HI + 1 }
					k = cpar(k + 2); CPD--
				}
				else if (c == "$" && nx(k + 1) == "{") {
					# "${x:-")"}": skip it whole (a ")" or quote in it is not ours).
					D32 = 1; j = brace_end(k, 1, VIEW, 0)
					if (!VIEW && BALT && brace_end(k, 1, 1, 0) != j) ALT1 = ALT2 = 1
					k = j
				}
				continue
			}
			if (pst && c != " " && c != "\t" && c != "\n") { pst = 0; if (c == "(") continue }
			if (pat && (c == "(" || c == ")")) {
				if (c == "(") pd++; else if (pd) pd--; else pat = 0
				continue
			}
			if (c == ";" && cs && (nx(k + 1) == ";" || nx(k + 1) == "&")) { k++; if (nx(k + 1) == "&") k++; pat = pst = 1; pd = 0; continue }
			if (c == "c" && nx(k + 1) == "a" && wstart(k) && rng(k, k + 3) == "case" && bound(k + 4)) { D32 = 1; cs++; cw = 1; k += 3; continue }
			if (c == "i" && cw && nx(k + 1) == "n" && wstart(k) && bound(k + 2)) { cw = 0; pat = pst = 1; pd = 0; k++; continue }
			if (c == "e" && cs && nx(k + 1) == "s" && wstart(k) && rng(k, k + 3) == "esac" && bound(k + 4)) { cs--; pat = 0; k += 3; continue }
			if (c == "\047") sq = 1
			else if (c == "\"") dq = 1
			else if (c == "`") k = tick_end(k, HI)
			else if (c == "$" && nx(k + 1) == "\047") k = ansi_end(k + 1)
			else if (c == "$" && nx(k + 1) == "{" && VIEW != 3) {
				# ${x:-)}: bash 5, zsh and dash skip it whole, bash 3.2 does not.
				D32 = 1; j = brace_end(k, 0, VIEW, 0)
				if (!VIEW) { if (BOP) ALT3 = 1; if (BALT && brace_end(k, 0, 1, 0) != j) ALT1 = 1 }
				k = j
			}
			else if (c == "$" && nx(k + 1) == "[" && VIEW != 2) {
				# (to dash, plain text: see parse)
				if ((j = bracket_end(k + 1)) <= HI) { if (!VIEW && !BAR) ALT2 = 1; D32 = 1; k = j }
			}
			else if (c == "#" && wstart(k)) { D32 = 1; while (k < HI && ch[k + 1] != "\n") k++ }
			else if (c == "<" && nx(k + 1) == "<") {
				if (nx(k + 2) == "<") k += 2
				else { D32 = 1; k = read_delim(k + 2); NPD++; PDL[NPD] = DL; PDS[NPD] = DS }
			}
			else if (c == "\n" && NPD > pb) {
				for (j = pb + 1; j <= NPD; j++) k = body_end(k, PDL[j], PDS[j], VIEW == 4 ? 2 : 1)
				NPD = pb
			}
			else if (c == "(") { if (nx(k + 1) == "(" && (j = arith_end(k + 2))) { D32 = 1; k = j } else depth++ }
			else if (c == ")" && --depth == 0) { NPD = pb; return k }
		}
		NPD = pb
		return HI + 1
	}
	# Does a word end before index k?
	function bound(k,    c) {
		c = nx(k)
		return c == "" || c == " " || c == "\t" || c == "\n" || c == ";" || c == "&" || c == "|" || c == "(" || c == ")" || c == "<" || c == ">"
	}
	function wstart(k,    p) {
		if (k <= 1) return 1
		p = ch[k - 1]
		return p == " " || p == "\t" || p == "\n" || p == ";" || p == "&" || p == "|" || p == "(" || p == ")"
	}
	# Queue the command substitutions in ch[a..b] (heredoc bodies, arithmetic;
	# q: quotes and comments count, as in name=(array words); SOP is then set
	# if it holds an unquoted ; & | < > ( or ), and SOPP is the first one that
	# is not part of a <(...) or >(...)).
	function substscan(a, b, q,    k, c, sq, dq) {
		sq = dq = SOP = SOPP = 0
		for (k = a; k <= b && hit == ""; k++) {
			c = ch[k]
			if (sq) { if (c == "\047") sq = 0; continue }
			if (c == "\\") { k++; continue }
			if (c == "`") { k = tick(k, b); continue }
			if (c == "$") {
				# $(( ... )) is read on, unless it is $( (subshell) ... ).
				if (nx(k + 1) == "(") { if (nx(k + 2) == "(" && arith_end(k + 3)) k += 2; else k = subst(k) }
				else if (q && !dq && nx(k + 1) == "\047") k = ansi_end(k + 1)
				continue
			}
			if (q) {
				if (c == "\"") dq = !dq
				else if (dq) continue
				else if (c == "\047") sq = 1
				else if (c == "#" && (k == a || wstart(k))) { while (k < b && ch[k + 1] != "\n") k++ }
				else if ((c == "<" || c == ">") && nx(k + 1) == "(") { SOP = 1; k = close_paren(k + 2) }
				else if (c == ";" || c == "&" || c == "|" || c == "<" || c == ">" || c == "(" || c == ")") { SOP = 1; if (!SOPP) SOPP = k }
			}
		}
	}
	# A redirection at i. Returns the index of its last character.
	function redir(i,    c, d, j, x) {
		c = ch[i]; d = nx(i + 1)
		if (d == "(") {
			j = close_sub(i + 2, 0)
			if (rpush(i + 2, j - 1)) { RC[nr] = 1; if (!wn && !wq) PSPEND = nr }
			wadd(c "()"); return j
		}
		fdprefix()
		if (c == "<" && d == "<") {
			if (nx(i + 2) == "<") { opword("<<<"); HSN++; return i + 2 }
			endword()
			j = read_delim(i + 2)
			nhd++; HD[nhd] = DL; HS[nhd] = DS; HQ[nhd] = DQ; HC[nhd] = ncmd + 1
			return j
		}
		x = c
		if (c == ">" && (d == ">" || d == "|" || d == "&")) { x = x d; i++ }
		else if (c == "<" && (d == "&" || d == ">")) { x = x d; i++ }
		# zsh: >! and >>! (also &>!, 2>!) clobber; to bash, "!" is the target (view 1).
		if (c == ">" && nx(i + 1) == "!") { if (VIEW == 1) { x = x "!"; i++ } else if (!VIEW) ALT1 = 1 }
		opword(x)
		return i
	}
	# "2>x", "{fd}>x": the word before the operator is a file descriptor.
	function fdprefix(    x) {
		if (wq || !wn) return
		x = wget()
		if (x ~ /^[0-9]+$/ || x ~ /^\{[A-Za-z_][A-Za-z0-9_]*\}$/) wreset()
	}
	# Heredoc delimiter after "<<" at k. Sets DL (the word), DS (1 for "<<-")
	# and DQ (1 if quoted: the body is not expanded). Returns its last index.
	# A delimiter longer than 256 characters is "complex" (a cut one would
	# never end its body, and hide the rest of the command). So is one with
	# $'"'"'...'"'"', $"...", $(...), ${...}, $[...] or `...` in it, or a "(" (bash and zsh
	# read those as other words: <<$'"'"'EOF'"'"' ends at EOF, <<${x:-a b} is one word).
	function read_delim(k,    c, q, n) {
		DS = DQ = 0; DL = ""; q = ""; n = 0
		if (nx(k) == "-") { DS = 1; k++ }
		while (k <= HI && (ch[k] == " " || ch[k] == "\t")) k++
		for (; k <= HI && hit == ""; k++) {
			c = ch[k]
			if (q != "") { if (c == q) q = ""; else if (n++ < 256) DL = DL c; else toomuch(); continue }
			if (c == "\047" || c == "\"") { q = c; DQ = 1; continue }
			if (c == "\\") { DQ = 1; k++; if (n++ < 256) DL = DL nx(k); else toomuch(); continue }
			if (c == "`" || c == "(" || (c == "$" && index("\047\"({[", nx(k + 1)) && k < HI)) { toomuch(); return k }
			if (c == " " || c == "\t" || c == "\n" || c == ";" || c == "&" || c == "|" || c == "<" || c == ">" || c == ")") break
			if (n++ < 256) DL = DL c; else toomuch()
		}
		return k - 1
	}
	# Skip a heredoc body that starts after the newline at k. Sets BS..BE to the
	# body and returns the index of the newline ending the delimiter line. In
	# $(...) (cp > 0), "EOF)" also ends it, as in bash; then the ")" comes next.
	# bash 5.2 and later (B52) end it there at any line that starts with the
	# delimiter and has a ")" after it, and read the rest of that line as
	# commands ("EOFError :)" in a commit message ends the $(...) early). That
	# is view 4: cp 2 in cpar, 3 in parse() of a $(...) body (RC), where a line
	# cut by the end of the range has the ")" that ends it. View 0 asks for it
	# (cp 1) where it sees such a line.
	function body_end(k, delim, strip, cp,    s, e, ls, dl, p) {
		BS = s = k + 1; dl = length(delim)
		while (s <= HI && hit == "") {
			e = s
			while (e <= HI && ch[e] != "\n") e++
			ls = s
			if (strip) while (ls < e && ch[ls] == "\t") ls++
			if (e - ls == dl && rng(ls, e - 1) == delim) { BE = s - 1; return e }
			if (cp && e - ls > dl && rng(ls, ls + dl - 1) == delim) {
				if (ch[ls + dl] == ")") { BE = s - 1; return ls + dl - 1 }
				if (B52 && (cp > 1 || !VIEW)) {
					for (p = ls + dl; p < e && ch[p] != ")"; p++) ;
					if (p < e || (cp == 3 && e > HI)) { if (cp == 1) ALT4 = 1; else { BE = s - 1; return ls + dl - 1 } }
				}
			}
			s = e + 1
		}
		BE = HI; return HI + 1
	}
	function rng(a, b,    s) { s = ""; for (; a <= b; a++) s = s ch[a]; return s }
	# Heredoc bodies after the newline at i: skipped here, and decided by
	# flush() once their pipeline is complete (cat <<EOF |\n...\nEOF\nbash).
	function bodies(i,    h, e) {
		for (h = 1; h <= nhd && hit == ""; h++) {
			e = body_end(i, HD[h], HS[h], (VIEW == 4 && CSUB) ? 3 : 0)
			defer(HC[h], BS, BE, HQ[h] ? "q" : "h")
			i = e
		}
		nhd = 0
		if (!pp && !gd) flush()
		return i
	}
	# What command c feeds to its stdin, decided later: a heredoc body ch[a..b]
	# (t "h", or "q" if quoted) or echo / printf words EA[a..b] (t "e").
	function defer(c, a, b, t) {
		nb++; IC[nb] = c; IA[nb] = a; IB[nb] = b; IT[nb] = t
		if (nb == 1 || c < IMIN) IMIN = c
	}
	# Fed to a shell (directly, or by a later stage of the pipeline: cat <<EOF
	# | bash, echo "pip list" | sh) it is code, run the way that shell runs
	# (pixi run bash reads it under pixi); otherwise only an unquoted body is
	# looked at, for the $(...) and `...` it expands.
	function flush(    k, h, c, pm) {
		# PSH[k]: command k or a later stage of its pipeline is a shell reading
		# stdin (its mode, see mcode). In a range whose output is code (feed()),
		# every command'"'"'s is.
		for (k = ncmd; k >= IMIN; k--) PSH[k] = FEED ? FEED : CMDSH[k] ? CMDSH[k] : (k < ncmd && PIPE[k + 1] == PIPE[k]) ? PSH[k + 1] : 0
		pm = PXM
		for (h = 1; h <= nb; h++) {
			c = IC[h] <= ncmd ? PSH[IC[h]] : 0
			if (c) PXM = mname(c)
			if (IT[h] == "e") { if (c) for (k = IA[h]; k <= IB[h]; k++) pushw(EA[k]) }
			else if (c) rpush(IA[h], IB[h])
			else if (IT[h] == "h") substscan(IA[h], IB[h])
			PXM = pm
		}
		nb = NEA = 0
	}
	# The words echo / printf at WD[i] prints (printf -v prints nothing),
	# each one, and then (for more than one) the text it prints: echo'"'"'s words
	# joined by blanks, printf'"'"'s format with its arguments put in (see pfmt).
	# Under pixi only an installer is refused, and it takes its verb: echo pip
	# install x | pixi run bash.
	function echoargs(i,    j, x, b, lo) {
		b = WD[i]; sub(/.*\//, "", b); lo = NEA + 1
		for (j = i + 1; j <= nw; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (lo > NEA) {
				if (b == "echo" && x ~ /^-[neE]+$/) continue
				if (b == "printf" && x == "-v") return
				if (b == "printf" && x == "--") continue
			}
			EA[++NEA] = x
		}
		if (NEA > lo) { x = b == "printf" ? pfmt(lo, NEA) : ejoin(lo, NEA); if (hit == "" && x != "") EA[++NEA] = x }
		if (NEA >= lo) defer(ncmd, lo, NEA, "e")
	}
	# EA[a..b] joined by blanks.
	function ejoin(a, b,    o1, o2, o3, n1, n2) {
		spent(b - a + 1)
		o1 = EA[a]; o2 = o3 = ""; n1 = n2 = 0
		for (a++; a <= b; a++) {
			o1 = o1 " " EA[a]
			if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } }
		}
		return o3 o2 o1
	}
	# What printf FORMAT ARGS... prints, roughly: FORMAT is EA[a], ARGS
	# EA[a+1..b]. Each % directive takes the next argument (and each * in it
	# one more; %c its first character, %% is a %), and FORMAT is used again
	# while arguments are left. Its escapes are left for pushw(), which
	# decodes them. "" when that adds nothing to the words: FORMAT takes no
	# argument (it is printed once), or it is %s\n (each argument on a line).
	# The work (the text it makes) is counted (spent) before it is done.
	function pfmt(a, b,    n, k, c, p, d, s, o1, o2, o3, n1, n2) {
		if (EA[a] == "%s\\n") return ""
		# d: its directives, found as below (a pass takes at least d arguments)
		n = chars(EA[a], PF); d = s = 0
		for (k = 1; k < n; k++) if (PF[k] == "%" && PF[++k] != "%") { for (; k < n && index(PFL, PF[k]); k++) ; d++ }
		if (!d) return ""
		for (k = a + 1; k <= b; k++) s += length(EA[k])
		spent(int((b - a + d - 1) / d) * n + s)
		if (hit != "") return ""
		o1 = o2 = o3 = ""; n1 = n2 = 0; p = a + 1
		do {
			for (k = 1; k <= n; k++) {
				c = PF[k]
				if (c == "%" && k < n) {
					if (PF[++k] == "%") c = "%"
					else {
						for (; k < n && index(PFL, PF[k]); k++) if (PF[k] == "*") p++
						c = p <= b ? EA[p++] : ""
						if (PF[k] == "c") c = substr(c, 1, 1)
					}
				}
				o1 = o1 c
				if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } }
			}
		} while (p <= b)
		return o3 o2 o1
	}
	# The here-strings of the command.
	function hsargs(    j, lo) {
		lo = NEA + 1
		for (j = 1; j < nw; j++) if (OP[j] && WD[j] == "<<<") EA[++NEA] = WD[j + 1]
		if (NEA >= lo) defer(ncmd, lo, NEA, "e")
	}
	# Queue a printed word as code, a line at a time ("\n" ends a line).
	# (printf and echo -e decode escapes: printf '"'"'pyth\157n3'"'"' prints python3.)
	function pushw(x,    n, k) {
		n = split(x, EP, /\\n/)
		for (k = 1; k <= n; k++) if (EP[k] != "") push(index(EP[k], "\\") ? dec(EP, k, k, 0, 1) : EP[k])
	}
	# The escapes in A[a..b] decoded: \xHH \NNN \t \n \\ ..., as in $'"'"'...'"'"' (q:
	# also \uHHHH; an unknown escape drops its backslash) or as printf and echo
	# -e decode them (\0NNN is octal too; an unknown escape is kept, for the
	# shell reading it). w: A[a] is a word to split into characters first.
	function dec(A, a, b, q, w,    k, c, v, m, j, d, o1, o2, o3, n1, n2) {
		if (w) { b = chars(A[a], DC); return dec(DC, 1, b, q, 0) }
		o1 = o2 = o3 = ""; n1 = n2 = 0
		for (k = a; k <= b; k++) {
			c = A[k]
			if (c == "\\" && k < b) {
				c = A[++k]
				if (c == "x" || (q && (c == "u" || c == "U"))) {
					m = c == "x" ? 2 : c == "u" ? 4 : 8; v = j = 0
					while (j < m && k < b && (d = index(HEX, tolower(A[k + 1])))) { v = v * 16 + d - 1; k++; j++ }
					c = j ? chr(v) : q ? c : "\\" c
				}
				else if (index("01234567", c)) {
					m = (!q && c == "0") ? 4 : 3; v = c + 0
					for (j = 1; j < m && k < b && index("01234567", A[k + 1]); j++) v = v * 8 + A[++k]
					c = chr(v)
				}
				else if (c in CE) c = chr(CE[c])
				else if (!q && c != "\\") c = "\\" c
			}
			o1 = o1 c
			if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } }
		}
		return o3 o2 o1
	}
	# The ASCII character with code v ("?" for others, nothing for NUL).
	function chr(v) { return (v > 0 && v < 128) ? sprintf("%c", v) : v ? "?" : "" }

	# Words a..b joined by spaces. q: quote them, so the result parses back
	# into the same words.
	function join(a, b, q,    x, s1, s2, s3, n1, n2) {
		s1 = s2 = s3 = ""; n1 = n2 = 0
		for (; a <= b; a++) {
			x = WD[a]
			if (q && !OP[a] && x !~ safe) x = shq(x)
			s1 = s1 " " x
			if (++n1 >= 64) { s2 = s2 s1; s1 = ""; n1 = 0; if (++n2 >= 64) { s3 = s3 s2; s2 = ""; n2 = 0 } }
		}
		return s3 s2 s1
	}
	function shq(x,    n, p, i, r) {
		n = split(x, p, "[\047]"); r = p[1]
		for (i = 2; i <= n; i++) r = r "\047\\\047\047" p[i]
		return "\047" r "\047"
	}

	# Skip wrapper b'"'"'s options from WD[i]. Returns the index after them.
	# Sets ENVS to the string of env -S, XR to the replace string of xargs
	# -I / -i / -J, SCX to the command of screen -X, ESH for entr -s, PQ for
	# parallel -q, CDX for an option that changes folders (env -C, ...), and
	# NOEX where nothing is run (sudo -e / -l, taskset -p, chrt -p, script
	# -p, xcrun -f). Each option word is split into characters once (BWK
	# awk'"'"'s substr() walks the whole word).
	function skipopts(b, i, i1,    x, sl, ll, k, m, c) {
		sl = OS[b]; ll = OL[b]
		while (i <= i1 && !OP[i] && WD[i] ~ /^-/) {
			x = WD[i]
			if (x == "--") return i + 1
			if (index(ll, " " x " ")) { optv(b, x, i < i1 ? WD[i + 1] : ""); i += 2; continue }
			# (xcrun'"'"'s options are words: -find, -show-sdk-path, -log)
			if (b == "xcrun") { if (x == "-f" || x == "-find" || x == "--find") NOEX = 1; i++; continue }
			if (x ~ /^--/) {
				if (k = index(x, "=")) optv(b, substr(x, 1, k - 1), substr(x, k + 1))
				else if ((b == "sudo" && (x == "--edit" || x == "--list")) || ((b == "taskset" || b == "chrt") && x == "--pid")) NOEX = 1
				else if (b == "xargs" && x == "--replace") XR = "{}"
				else if (b == "parallel" && x == "--quote") PQ = 1
				i++; continue
			}
			m = chars(x, OC)
			for (k = 2; k <= m; k++) {
				c = OC[k]
				if ((b == "sudo" && (c == "e" || c == "l")) || (c == "p" && (b == "taskset" || b == "chrt" || b == "script"))) NOEX = 1
				if (b == "entr" && c == "s") ESH = 1
				if (b == "parallel" && c == "q") PQ = 1
				# xargs -i[R]: an optional replace string, attached.
				if (b == "xargs" && c == "i") { XR = k < m ? substr(x, k + 1) : "{}"; break }
				if (index(sl, c)) {
					if (k < m) optv(b, "-" c, substr(x, k + 1))
					else { optv(b, "-" c, i < i1 ? WD[i + 1] : ""); i++ }
					break
				}
			}
			i++
		}
		return i
	}
	function optv(b, o, v) {
		if ((b == "env" && (o == "-C" || o == "--chdir")) || (b == "sudo" && (o == "-D" || o == "--chdir")) ||
		    (b == "parallel" && (o == "-W" || o == "--workdir" || o == "--wd")) || (b == "watchexec" && o == "--workdir")) CDX = 1
		if (b == "env" && (o == "-S" || o == "--split-string")) ENVS = v
		else if (b == "xargs" && (o == "-I" || o == "-J" || o == "--replace")) XR = v
		else if (b == "screen" && o == "-X") SCX = v
		else if (index(CO[b], " " o " ")) push(v)
	}

	# Is a program (b: its base name, x: as written, wd: the word it was read
	# from) Python or Python tooling? Not a tool from a pixi environment
	# (pixibin), unless it is an installer or env manager (instre). Three
	# cases are left to the shell (see END): a tool by its bare name (up to 16,
	# TN) is let through if it resolves into a pixi environment; a *.py script
	# by a literal path (up to 16, PYP) if its #! line runs pixi; and when a
	# virtual or conda env is active (VB), a bare name (up to 256, PG) that is
	# a Python script in it is refused.
	function istool(b, x, wd) {
		if (b ~ toolre) {
			if (b ~ instre) return 1
			if (!index(x, "/")) return x == wd ? deftool(b) : 1
			return !pixibin(x, wd)
		}
		if (index(x, "/bin/") && x ~ venvre) return !pixibin(x, wd)
		if (index(b, ".py") && b ~ /\.py$/) {
			# (no expansions, globs or braces: the path as it is run)
			if (!index(x, "/") || x != wd || x ~ /[^ -~]|[$`*?[{\\]/) return 1
			if (x in PYS) return 0
			if (NPY >= 16) return 1
			PYS[x] = 1; PYP[++NPY] = x
			return 0
		}
		if (VB && !index(x, "/") && NPG < 256 && b ~ /^[A-Za-z0-9._+-]+$/ && !(b in PGS)) { PGS[b] = 1; PG[++NPG] = b }
		return 0
	}
	# Tool b by its bare name: list it for the shell (TN), which looks where
	# it resolves. TNS: "b32" or "b52" if only that view (Claude Code'"'"'s bash,
	# see close_sub and body_end) saw it, for the note; else "-". Returns 1 (a
	# hit) when that can'"'"'t be told here: more than 16 names, or odd characters.
	function deftool(b,    t) {
		if (b !~ /^[A-Za-z0-9._+-]+$/) return 1
		t = (B32 && VIEW == 3) ? "b32" : VIEW == 4 ? "b52" : "-"
		if (b in TNS) { if (TNS[b] != t) TNS[b] = "-"; return 0 }
		if (NTN >= 16) return 1
		TNS[b] = t; TN[++NTN] = b
		return 0
	}
	# Is program x (read from word wd) in a pixi environment'"'"'s bin folder:
	# ~/.pixi/bin, $PIXI_HOME/bin (PXH, PXP), a .pixi/envs/NAME/bin (a
	# workspace'"'"'s, or pixi global'"'"'s under ~/.pixi) or $PIXI_HOME/envs/NAME/bin?
	# x must be a literal path, or wd one that starts with $HOME/ or
	# $PIXI_HOME/ (when set; or ${PIXI_HOME:-WORD}/, whose WORD reading is
	# checked on its own, see defw) and is literal after that, or (for a
	# .pixi/envs one) from .pixi on.
	function pixibin(x, wd,    p, r) {
		# any folder'"'"'s .pixi/envs/NAME/bin, literal from .pixi on ("$PWD/.pixi/envs/default/bin/pytest")
		if (match(wd, /(^|\/)\.pixi\/envs\/[^\/]+\/bin\/[^\/]+$/) && substr(wd, RSTART) !~ /[^ -~]|[$`*?[{\\]|\/\.\.\//) return 1
		if (match(wd, /^[$]([{]HOME[}]|HOME)\//)) { if (PXH == "") return 0; p = PXH substr(wd, RLENGTH) }
		else if (match(wd, /^[$]([{]PIXI_HOME(:?[-=][^}]*)?[}]|PIXI_HOME)\//)) {
			if (PXP == "") return substr(wd, 1, 12) != "${PIXI_HOME}" && substr(wd, 2, 1) == "{"
			p = PXP substr(wd, RLENGTH)
		}
		else if (x == wd) p = x
		else return 0
		if (p ~ /[^ -~]|[$`*?[{\\]/ || p ~ /(^|\/)\.\.(\/|$)/) return 0
		if (substr(p, 1, 2) == "~/") { if (PXH == "") return 0; p = PXH substr(p, 2) }
		if (p ~ /(^|\/)\.pixi\/envs\/[^\/]+\/bin\/[^\/]+$/) return 1
		if (PXH != "" && substr(p, 1, length(PXH) + 11) == PXH "/.pixi/bin/") { r = substr(p, length(PXH) + 12); return r != "" && !index(r, "/") }
		if (PXP != "" && substr(p, 1, length(PXP) + 1) == PXP "/") return substr(p, length(PXP) + 2) ~ /^(bin|envs\/[^\/]+\/bin)\/[^\/]+$/
		return 0
	}

	# Files the command writes (a redirection target, tee FILE, sed -i FILE,
	# cp / mv DEST, curl -o FILE, ...): a *.py script run by its path in the
	# same command is refused (its #! line is read before the command runs,
	# so it would be the old one). Only *.py names count; up to 64 (WT), and
	# more, or an odd one, counts as all of them (WTA).
	function wtgt(x) {
		if (!index(x, ".py") || x !~ /\.py$/) return
		if (NWT >= 64 || length(x) > 1024 || x ~ /[^ -~]/) WTA = 1
		else WT[++NWT] = x
	}
	# The output redirections of WD[i0..i1].
	function wredir(i0, i1,    j) {
		for (j = i0; j < i1; j++) if (OP[j] && WD[j] ~ /^(&?>>?[|!]?|<>|>&)$/) wtgt(WD[j + 1])
	}
	# The files command b writes, from its arguments WD[i..i1]: tee FILE...,
	# sed / perl -i FILE..., cp / mv / ln / install SRC... DEST (DEST/NAME when
	# DEST is a folder; -t DIR), curl -o FILE, wget -O FILE.
	function wargs(b, i, i1,    j, x, n, k, o, d, ip) {
		n = ip = 0; d = ""
		for (j = i; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (b == "curl" || b == "wget") {
				o = b == "curl" ? "o" : "O"
				if (x == "--output" || x == "--output-document") { if (j < i1) wtgt(WD[j + 1]); j++ }
				else if (x ~ /^--output(-document)?=/) wtgt(substr(x, index(x, "=") + 1))
				else if (x ~ /^-[A-Za-z]/ && (k = index(x, o))) { if (k == length(x)) { if (j < i1) wtgt(WD[j + 1]); j++ } else wtgt(substr(x, k + 1)) }
				continue
			}
			if (x ~ /^-./) {
				if (x ~ /^-[A-Za-z]*i/ || x ~ /^--in-place/) ip = 1
				if (x == "-t" || x == "--target-directory") { if (j < i1) d = WD[j + 1]; j++ }
				else if (x ~ /^--target-directory=/) d = substr(x, 20)
				continue
			}
			WA[++n] = x
		}
		if (b == "tee" || ((b == "sed" || b == "gsed" || b == "perl") && ip)) { for (k = 1; k <= n; k++) wtgt(WA[k]); return }
		if (b == "tee" || b == "sed" || b == "gsed" || b == "perl") return
		if (d == "") { if (n < 2) return; d = WA[n--] }
		if (d ~ /\.py$/) { wtgt(d); return }
		sub(/\/+$/, "", d)
		for (k = 1; k <= n; k++) if (WA[k] ~ /\.py$/) { x = WA[k]; sub(/.*\//, "", x); wtgt(d "/" x) }
	}
	# Does the command write *.py path p (see wtgt)? Paths compare without
	# "." parts and with ~/ expanded; a relative one matches the end of an
	# absolute one. A written path with an expansion or ".." in it, or a long
	# one, matches by its name (or, with an expansion in the name, any).
	function wrote(p,    m, t, q, b)  {
		if (WTA || length(p) > 4096) return 1
		q = p; sub(/.*\//, "", q); p = npath(p)
		for (m = 1; m <= NWT; m++) {
			t = WT[m]; b = t; sub(/.*\//, "", b)
			if (b ~ /[$`*?[{\\]/) return 1
			if (t ~ /[$`*?[{\\]/ || index(t, "..") || index(p, "..")) { if (b == q) return 1; continue }
			t = npath(t)
			if (t == p || (t ~ /^\// && p !~ /^\// && sfx(t, "/" p)) || (p ~ /^\// && t !~ /^\// && sfx(p, "/" t))) return 1
		}
		return 0
	}
	function npath(p,    n, k, s, a) {
		if (substr(p, 1, 2) == "~/" && PXH != "") p = PXH substr(p, 2)
		a = substr(p, 1, 1) == "/"; s = ""
		n = split(p, NS, "/")
		for (k = 1; k <= n; k++) if (NS[k] != "" && NS[k] != ".") s = s (s == "" ? "" : "/") NS[k]
		return (a ? "/" : "") s
	}
	function sfx(a, b) { return length(a) >= length(b) && substr(a, length(a) - length(b) + 1) == b }
	# The program name word i stands for, as far as it can be told: unquoted
	# expansions dropped (${x:+}sh is sh, $PREFIX/bin/python3 is /bin/python3;
	# nothing is left of $SUDO, $(true), `true` or "$@", which can be empty),
	# zsh'"'"'s "=" dropped (=python3), and $SHELL or $BASH taken for sh. PDW:
	# the word read with each ${NAME:-WORD} in it (also -, :=, =, :+ and +) as
	# its WORD, quoted or not: that may be the program too (see defw).
	function pname(i,    x) {
		x = WD[i]; PDW = ""
		if (x ~ /^[$][{]?(SHELL|BASH)[}]?$/) return "sh"
		if ((PRE[i] == x || DX[i]) && index(x, "${")) PDW = defw(x, PRE[i] == x)
		if (PRE[i] != x) return (PRE[i] == "" && x ~ /^[$]([@]|[{]([@]|[A-Za-z_][A-Za-z0-9_]*[[][@][]])(:[^}]*)?[}])$/) ? "" : x
		if (index(x, "$") || index(x, "`")) x = noexp(x)
		if (substr(x, 1, 1) == "=") x = substr(x, 2)
		return x
	}
	# x without the expansions bash may drop: $(), ``, ${...}, $NAME, $1, $@ ...
	# One pass (a regex gsub() retries "${" up to the end of the word at each
	# "$", which is quadratic on "${${${...").
	function noexp(x,    n, p, c, d, b, s1, s2, s3, n1, n2) {
		n = chars(x, PC); b = 0; s1 = s2 = s3 = ""; n1 = n2 = 0
		for (p = 1; p <= n; p++) {
			c = PC[p]; d = p < n ? PC[p + 1] : ""
			if (c == "`" && d == "`") { p++; continue }
			if (c == "$") {
				if (d == "(" && p + 2 <= n && PC[p + 2] == ")") { p += 2; continue }
				if (d == "{") {
					# b: the first "}" after the last one looked for (each looked at once)
					if (b <= p) for (b = p + 2; b <= n && PC[b] != "}"; b++) ;
					if (b <= n) { p = b; continue }
				}
				else if (d != "" && index(NM1, d)) { for (p += 2; p <= n && index(NM, PC[p]); p++) ; p--; continue }
				else if (d != "" && index("0123456789@*#?$!-", d)) { p++; continue }
			}
			s1 = s1 c
			if (++n1 >= 64) { s2 = s2 s1; s1 = ""; n1 = 0; if (++n2 >= 64) { s3 = s3 s2; s2 = ""; n2 = 0 } }
		}
		return s3 s2 s1
	}
	# Program word x read with each ${NAME:-WORD} in it (or -, :=, =, :+, +) as
	# its WORD (quotes removed) and the other expansions dropped, as noexp()
	# drops them: ${VENV:-.venv}/bin/uvicorn is .venv/bin/uvicorn, and
	# ${PY:-python}${V} is python. "" if no such WORD is in it (or a ${ is not
	# closed). uq: x is unquoted, so only its first field counts. PDN: a :- or
	# := has a WORD, so x is never empty. One pass over x.
	function defw(x, uq,    n, p, c, d, e, k, j, a, o1, o2, o3, n1, n2) {
		PDN = a = 0; o1 = o2 = o3 = ""; n1 = n2 = 0
		n = chars(x, PC)
		for (p = 1; p <= n; p++) {
			c = PC[p]; d = p < n ? PC[p + 1] : ""
			if (c == "`" && d == "`") { p++; continue }
			if (c == "$" && d == "(" && p + 2 <= n && PC[p + 2] == ")") { p += 2; continue }
			if (c == "$" && d == "{") {
				# e: its closing "}" (quotes in it count; nested ${ nest)
				j = 1
				for (e = p + 2; e <= n; e++) {
					c = PC[e]
					if (c == "\\") e++
					else if (c == "\047") { for (e++; e <= n && PC[e] != "\047"; e++) ; }
					else if (c == "\"") { for (e++; e <= n && PC[e] != "\""; e++) if (PC[e] == "\\") e++ }
					else if (c == "$" && e < n && PC[e + 1] == "{") { j++; e++ }
					else if (c == "}" && !--j) break
				}
				if (e > n) return ""
				# NAME, a number, @ or * (not ${#x} or ${!x}), [SUBSCRIPT], then the operator
				k = p + 2; c = PC[k]
				if (index(NM1, c)) { for (k++; k < e && index(NM, PC[k]); k++) ; }
				else if (index("0123456789", c)) { for (k++; k < e && index("0123456789", PC[k]); k++) ; }
				else if (c == "@" || c == "*") k++
				else k = e
				if (k < e && PC[k] == "[") { for (k++; k < e && PC[k] != "]"; k++) ; k++ }
				j = k < e && PC[k] == ":"; k += j
				if (k < e && index("-=+", PC[k])) {
					c = PC[k]
					for (k++; k < e; k++) {
						if (index("\\\"\047", PC[k])) continue
						if (j && c != "+") PDN = 1
						a = 1; o1 = o1 PC[k]
						if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } }
					}
				}
				p = e; continue
			}
			if (c == "$" && d != "" && index(NM1, d)) { for (p += 2; p <= n && index(NM, PC[p]); p++) ; p--; continue }
			if (c == "$" && d != "" && index("0123456789@*#?$!-", d)) { p++; continue }
			o1 = o1 c
			if (++n1 >= 64) { o2 = o2 o1; o1 = ""; n1 = 0; if (++n2 >= 64) { o3 = o3 o2; o2 = ""; n2 = 0 } }
		}
		if (!a) return ""
		x = o3 o2 o1
		if (uq) { sub(/^[ \t\n]+/, "", x); sub(/[ \t\n].*/, "", x) }
		PDN = PDN && x != ""
		return x
	}
	# Brace expansion of word x into BX[1..NBX], in bash'"'"'s order: nested
	# ({{python3,x},y}), several ({python3,}{,x}) and ranges ({a..c}, {1..3}).
	# A word over 4096 characters or making over 256 words is "complex", and
	# so is more than 200k characters of this work in all (BXW).
	function bexp(x,    k, j) {
		NBX = 1; BX[1] = x; k = 1
		if (length(x) > 4096) { toomuch(); return }
		while (k <= NBX && hit == "") {
			if (!index(BX[k], "{") || !bgroup(BX[k])) { k++; continue }
			if (NBX + BGN - 1 > 256) { toomuch(); return }
			for (j = NBX; j > k; j--) BX[j + BGN - 1] = BX[j]
			for (j = 1; j <= BGN; j++) BX[k + j - 1] = BGP BG[j] BGS
			NBX += BGN - 1
		}
	}
	# The first brace group in w (as bash finds it: the leftmost "{" with a
	# matching "}" and a comma or a range in it, not "${"): sets BGP (before
	# it), BG[1..BGN] (its words) and BGS (after it). Returns 0 if none.
	function bgroup(w,    n, p, q, c, sp, s, d) {
		n = chars(w, BC); sp = 0; split("", BM)
		if ((BXW += n) > 200000) { toomuch(); return 0 }
		for (p = 1; p <= n; p++) {
			c = BC[p]
			if (c == "{") { BST[++sp] = p; BCM[sp] = 0 }
			else if (c == "," && sp) BCM[sp] = 1
			else if (c == "}" && sp) { BM[BST[sp]] = p; BHC[BST[sp]] = BCM[sp]; sp-- }
		}
		for (p = 1; p <= n; p++) {
			if (BC[p] != "{" || !(p in BM) || (p > 1 && BC[p - 1] == "$")) continue
			q = BM[p]; BGN = 0
			if (BHC[p]) {
				s = p + 1; d = 0
				for (c = p + 1; c < q; c++) {
					if (BC[c] == "{") d++
					else if (BC[c] == "}") d--
					else if (BC[c] == "," && !d) { BG[++BGN] = substr(w, s, c - s); s = c + 1 }
				}
				BG[++BGN] = substr(w, s, q - s)
			} else if (!brange(substr(w, p + 1, q - p - 1))) continue
			BGP = substr(w, 1, p - 1); BGS = substr(w, q + 1)
			return 1
		}
		return 0
	}
	# {1..5}, {5..1..2}, {a..e}: sets BG[1..BGN]; 0 if r is not a range.
	function brange(r,    m, a, b, s, k, t) {
		m = split(r, t, /[.][.]/)
		if (m < 2 || m > 3 || (m == 3 && t[3] !~ /^-?[0-9]+$/)) return 0
		s = (m == 3 && t[3] + 0) ? t[3] + 0 : 1; if (s < 0) s = -s
		if (t[1] ~ /^-?[0-9]+$/ && t[2] ~ /^-?[0-9]+$/) { a = t[1] + 0; b = t[2] + 0; k = 0 }
		else if (t[1] ~ /^[A-Za-z]$/ && t[2] ~ /^[A-Za-z]$/) { a = index(ASC, t[1]); b = index(ASC, t[2]); k = 1 }
		else return 0
		if ((a > b ? a - b : b - a) / s >= 256) { toomuch(); return 0 }
		if (a > b) s = -s
		for (BGN = 0; s > 0 ? a <= b : a >= b; a += s) BG[++BGN] = k ? substr(ASC, a, 1) : a
		return 1
	}
	# Look at one simple command, WD[i0..i1]. Returns 0, or the mode (see
	# mcode) of the shell that runs what goes into its stdin as code: it is a
	# shell reading its script from stdin (so a heredoc body fed to it is
	# code; pixi run bash: 2), or what it prints goes into a >(bash) (run in
	# the mode of the code that has it, rcode; PSUB). It recurses for find
	# -exec, su, ...: deeper than 10 is "complex".
	function check(i0, i1,    r, d) {
		if (++CKD > 10) { toomuch(); CKD--; return 0 }
		if (CKD == 1) wredir(i0, i1)
		# DSH: a reading of a ${NAME:-WORD} program word was a shell reading
		# stdin (its mode).
		d = DSH; DSH = 0
		r = check1(i0, i1); if (!r && DSH) r = DSH
		DSH = d; CKD--
		return r
	}
	function check1(i0, i1,    i, x, b, j, k, rb, y, z, xa, xr, rr) {
		ENVS = ""; NOEX = 0; xa = 0
		i = i0
		while (i <= i1) {
			if (OP[i]) { i += 2; continue }
			x = WD[i]
			if (index(PRE[i], "=") && PRE[i] ~ /^[A-Za-z_][A-Za-z0-9_]*\+?=/) { if (x == "PIXI_GUARD=ask") ask = 1; else if (PRE[i] ~ /^PATH\+?=/ || x ~ /^path\+?=\(\)$/) PATHSET = 1; i++; continue }
			b = x; if (index(b, "/")) sub(/.*\//, "", b)
			# zsh: repeat N cmd
			if (b == "repeat" && PRE[i] == x) { i += 2; continue }
			if (b in KW) { i++; continue }
			if (b == "command") {
				# command -v / -V only looks a name up; command [-p] runs it.
				for (j = i + 1; j <= i1 && WD[j] ~ /^-./ && WD[j] != "--"; j++) if (WD[j] ~ /[vV]/) return 0
				i = (j <= i1 && WD[j] == "--") ? j + 1 : j
				continue
			}
			# direnv exec DIR CMD, asdf exec CMD, mise exec [TOOL...] -- CMD (or -c CMD),
			# gate with NAME -- CMD (the gateway runs CMD with an account'"'"'s key)
			if (b == "direnv" && i < i1 && WD[i + 1] == "exec") { i += 3; continue }
			if (b == "gate" && i + 3 <= i1 && WD[i + 1] == "with" && WD[i + 3] == "--") { i += 4; continue }
			if (b == "asdf" && i < i1 && WD[i + 1] == "exec") { i += 2; continue }
			if ((b == "mise" || b == "rtx") && i < i1 && (WD[i + 1] == "exec" || WD[i + 1] == "x")) {
				for (j = i + 2; j <= i1 && WD[j] != "--"; j++) {
					if ((WD[j] == "-c" || WD[j] == "--command") && j < i1) { code(j + 1); return 0 }
					if (WD[j] ~ /^--command=/) { push(substr(WD[j], 11)); return 0 }
				}
				i = j + 1; continue
			}
			if (b in WR) {
				XR = SCX = ""; ESH = 0
				# sudo, doas, env -i, env -u PATH: the program is looked up in
				# another PATH (see deftool); xcrun looks in Xcode first
				# (xcrun python3 is Xcode'"'"'s)
				if (b == "sudo" || b == "doas") PATHSET = 1
				else if (b == "env") for (j = i + 1; j <= i1 && !OP[j] && WD[j] ~ /^-/; j++)
					if (WD[j] == "-" || WD[j] ~ /^-[A-Za-z]*i/ || WD[j] == "--ignore-environment" || WD[j] ~ /^(-[A-Za-z]*u|--unset=)PATH$/ ||
					    (WD[j] ~ /^(-[A-Za-z]*u|--unset)$/ && j < i1 && WD[j + 1] == "PATH")) PATHSET = 1
				i = skipopts(b, i + 1, i1)
				if (NOEX) return 0
				# xcrun with a program left (not -f or --show-sdk-path alone)
				if (b == "xcrun" && i <= i1 && !OP[i]) PATHSET = 1
				# entr -s CMD: CMD is shell code.
				if (ESH) { if (i <= i1 && !OP[i]) code(i); return 0 }
				if (b == "xargs") { xa = 1; xr = XR }
				# screen -X stuff TEXT: TEXT is typed into the shell in the window.
				if (SCX != "") { if (SCX == "stuff") for (; i <= i1; i++) if (OP[i]) i++; else pushw(WD[i]); return 0 }
				if (b in NP) i += NP[b]
				# flock FILE -c CMD
				if (b == "flock" && i <= i1 && (WD[i] == "-c" || WD[i] == "--command")) { if (i < i1) code(i + 1); return 0 }
				if (ENVS != "") { push(ENVS join(i, i1, 1)); return 0 }
				continue
			}
			break
		}
		if (i > i1) return 0
		for (;;) {
			x = pname(i)
			# ${PYTHON:-python3} x, ${VENV:-.venv}/bin/uvicorn: the word read with
			# its default (or alternate) values is looked at as the program too;
			# then the word as it is. A word that is all expansions, one of them :-
			# or := with a value, is never empty: then the program is its default or
			# a value not known here (${EDITOR:-vim} f.py), and that is all.
			if (PDW != "") {
				spent(i1 - i + 1); rr = PDN
				y = WD[i]; z = PRE[i]; WD[i] = PRE[i] = PDW
				if ((k = check(i, i1))) DSH = k
				WD[i] = y; PRE[i] = z
				if (hit != "" || (rr && x == "")) return 0
			}
			# Brace expansion: {python,pip} x runs python, the first word it makes
			# ({,python3} x too: an empty word is dropped). Every word it makes is
			# looked at as a program name. Quotes are removed after it: {'"'"'pip'"'"',}
			# is expanded too, but not '"'"'{pip,}'"'"' (no "{" before the first quote).
			if (x != "" && index(PRE[i], "{") && index(x, "{") && (index(x, ",") || index(x, ".."))) {
				bexp(x)
				if (hit != "") return 0
				for (k = 1; k <= NBX; k++) {
					y = BX[k]; if (k == 1 || x == "") x = y
					b = y; if (index(b, "/")) sub(/.*\//, "", b)
					if (PXM) { if (pxinst(b, i, i1)) return 0 }
					else if (istool(b, y, WD[i])) { found(b); return 0 }
				}
			}
			if (x != "") break
			# A word that can expand to nothing ($SUDO pip ..., "$@" python3 ...):
			# the next one may be the program, so it is looked at as one.
			for (i++; i <= i1 && OP[i]; i += 2) ;
			if (i > i1) return 0
		}
		b = x; if (index(b, "/")) sub(/.*\//, "", b)
		if (b == "pixi") return pixi(i, i1)
		if (b in WRT) wargs(b, i + 1, i1)
		# export PATH=... (and zsh'"'"'s path=(...) array, tied to it): see deftool
		if (b ~ /^(export|declare|typeset|local|readonly)$/) for (j = i + 1; j <= i1; j++) if (WD[j] ~ /^PATH\+?=/ || WD[j] ~ /^path\+?=\(\)$/) PATHSET = 1
		# (a relative *.py path is then not looked up, see END)
		if (b == "cd" || b == "pushd" || b == "popd") CDX = 1
		# echo '"'"'pip list'"'"' | tee >(bash), echo '"'"'pip list'"'"' > >(bash): what goes into a
		# >(...) that starts with a shell reading stdin is code, for that shell:
		# the one of this code, also after pixi run (pixi run echo x > >(bash)).
		for (j = i + 1; j <= i1; j++)
			if (WD[j] == ">()" && PR[j] && (b == "tee" || (OP[j - 1] && WD[j - 1] ~ /^&?>[>|]?$/)) && shin(PR[j])) {
				if ((b == "echo" || b == "printf") && CKD == ECKD) ECHOI = i
				PSUB = 1; return rcode()
			}
		if ((b == "echo" || b == "printf") && CKD == ECKD) { ECHOI = i; return 0 }
		if (b in SHELL) {
			if (!shell(i, i1, b, xa, xr)) return 0
			stdin(i0, i1)
			return mcode()
		}
		if (b == "eval") { push(join(i + 1, i1, 0)); for (j = i + 1; j <= i1; j++) if (WD[j] == "$()") feed(j); return 0 }
		# alias NAME=VALUE...: VALUE runs where NAME is typed as a command (in an
		# interactive shell, such as a gateway terminal; zsh: alias -s py=python
		# runs x.py with python), so it is code.
		if (b == "alias") {
			for (j = i + 1; j <= i1; j++) {
				if (OP[j]) { j++; continue }
				if ((k = index(WD[j], "=")) > 1) push(substr(WD[j], k + 1))
			}
			return 0
		}
		if (b == "watch") { j = skipopts(b, i + 1, i1); if (j <= i1) push(join(j, i1, 0)); return 0 }
		# watchexec [OPTIONS] [--] COMMAND...: run by a shell, joined.
		if (b == "watchexec") { j = skipopts(b, i + 1, i1); if (j <= i1) push(join(j, i1, 0)); return 0 }
		if (b == "parallel" || b == "sem") return parallel(i, i1) ? mcode() : 0
		if (b == "hyperfine") { hyperfine(i, i1); return 0 }
		if (b == "nodemon") { nodemon(i, i1); return 0 }
		if (b == "npx" || b == "bunx") { npx(i, i1); return 0 }
		if (b == "npm" || b == "pnpm" || b == "yarn" || b == "bun") { if (j = npmexec(b, i, i1)) npx(j, i1); return 0 }
		# trap [-lpP] [--] CODE SIGNAL...: CODE runs later.
		if (b == "trap") {
			for (j = i + 1; j <= i1 && WD[j] ~ /^-[lpP]+$/; j++) ;
			if (j <= i1 && WD[j] == "--") j++
			if (j <= i1 && !OP[j]) code(j)
			return 0
		}
		if (b == "tmux") { tmux(i, i1); return 0 }
		if (b == "su" || b == "runuser") { PATHSET = 1; su(i, i1); return 0 }
		# Under pixi (PXM) a program comes from the pixi environment: only a
		# package installer is refused there.
		if (PXM) { if (pxinst(b, i, i1)) return 0 }
		else if (istool(b, x, WD[i])) { found(b); return 0 }
		if (b == "source" || b == ".") {
			# source FILE: a venv; /dev/stdin (reads stdin) or <(...) (code).
			for (j = i + 1; j <= i1 && OP[j]; j += 2) ;
			if (j > i1) return 0
			if (WD[j] ~ /(^|\/)bin\/activate/) { if (!PXM) found("venv activate"); return 0 }
			if (WD[j] ~ STDIN) { stdin(i0, i1); return mcode() }
			if (WD[j] == "<()") feed(j)
			return 0
		}
		if (b in PKGM) { pkg(i, i1, b); return 0 }
		if (b == "find" || b == "gfind") {
			for (j = i + 1; j <= i1; j++) if (WD[j] ~ /^-(exec|execdir|ok|okdir)$/) {
				if (WD[j] ~ /dir$/) CDX = 1
				k = j + 1
				while (k <= i1 && WD[k] != ";" && WD[k] != "+") k++
				check(j + 1, k - 1); j = k
			}
			return 0
		}
		if (b == "fd" || b == "fdfind") {
			for (j = i + 1; j <= i1; j++) if (WD[j] ~ /^(-x|-X|--exec|--exec-batch)$/) {
				k = j + 1
				while (k <= i1 && WD[k] != ";") k++
				check(j + 1, k - 1); j = k
			}
			return 0
		}
		if (b == "git") {
			for (j = i + 1; j <= i1; j++) {
				x = WD[j]
				if (x == "rebase") rb = 1
				# (they may run at the top of the working tree: CDX)
				else if (x == "bisect" && j < i1 && WD[j + 1] == "run") { CDX = 1; check(j + 2, i1); return 0 }
				else if (rb && (x == "-x" || x == "--exec") && j < i1) { CDX = 1; code(j + 1); j++ }
				else if (rb && x ~ /^--exec=/) { CDX = 1; push(substr(x, 8)) }
			}
		}
		return 0
	}

	# A shell at WD[i]: queue its -c string. Returns 1 if it reads stdin. xa:
	# xargs runs it, so a missing -c string comes from stdin, and so does a
	# command that starts with xargs'"'"'s replace string xr in it: printf '"'"'pip
	# list'"'"' | xargs -I{} sh -c '"'"'{}'"'"' (not sh -c '"'"'wc -l {}'"'"').
	function shell(i, i1, b, xa, xr,    j, x, m, k, c, cf, sf, need) {
		cf = sf = 0
		for (j = i + 1; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (x == "-" || x == "--") { j++; break }
			if (x ~ /^--/) {
				if (x == "--rcfile" || x == "--init-file") j++
				else if (x == "--command" || x == "--init-command") { if (j < i1) code(j + 1); j++; if (x == "--command") cf = 2 }
				else if (x ~ /^--(init-)?command=/) { push(substr(x, index(x, "=") + 1)); if (x ~ /^--command=/) cf = 2 }
				continue
			}
			if (x ~ /^[-+][A-Za-z]/) {
				if (b == "fish" && x == "-C") { if (j < i1) code(j + 1); j++; continue }
				m = chars(x, OC); need = 0
				for (k = 2; k <= m; k++) {
					c = OC[k]
					if (c == "c") cf = 1
					else if (c == "s") sf = 1
					else if (c == "o" || c == "O") need++
				}
				j += need
				continue
			}
			break
		}
		if (cf == 1) {
			if (j > i1 || OP[j]) return xa
			code(j); return xa && xr != "" && cmdat(WD[j], xr)
		}
		if (cf == 2) return 0
		if (sf || j > i1) return 1
		# The script: stdin again (bash /dev/stdin), or code (bash <(...)).
		if (WD[j] ~ STDIN) return 1
		if (WD[j] == "<()") feed(j)
		return 0
	}
	# tmux [-c CODE] COMMAND [ARGS] [\; COMMAND ...]: the shell command of
	# new-session, new-window, split-window, respawn-*, run-shell, if-shell,
	# pipe-pane and display-popup (one word is run by sh -c, more are the
	# command and its arguments), and the keys of send-keys, are code.
	function tmux(i, i1,    j, a, x, v, c, m) {
		for (j = i + 1; j <= i1 && WD[j] ~ /^-/; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (x == "--") { j++; break }
			if (x ~ /^-[^-]*c$/) { if (j < i1) code(j + 1); j++ }
			else if (x ~ /^-[^-]*[fLST]$/) j++
		}
		while (j <= i1 && !OP[j]) {
			c = WD[j]; v = (c in TV) ? TV[c] : ""
			for (j++; j <= i1 && !OP[j] && WD[j] ~ /^-./ && WD[j] != "--"; j++) {
				# an option that takes a value: -t name (not -tname)
				m = chars(WD[j], OC)
				if (m == 2 && index(v, OC[2])) j++
			}
			if (j <= i1 && WD[j] == "--") j++
			for (a = j; j <= i1 && !OP[j] && WD[j] != ";"; j++) ;
			if (v != "" && a < j) {
				if (c == "send-keys" || c == "send") { for (m = a; m < j; m++) pushw(WD[m]) }
				else if (c == "if-shell" || c == "if" || j - a == 1) code(a)
				else push(join(a, j - 1, 1))
			}
			if (j > i1 || OP[j]) break
			j++
		}
	}
	# GNU parallel (or sem) [OPTIONS] [COMMAND [ARGS]] [::: ARGS | :::: FILES]...:
	# COMMAND and its arguments are shell code (quoted with -q). Without a
	# COMMAND, or with one that starts with a replacement string ({}, {1}, ...),
	# the arguments after ::: are the commands, and so are the lines read on
	# stdin: returns 1 then (like a shell reading stdin). (moreutils parallel
	# has -- for :::.)
	function parallel(i, i1,    j, e, s, ac) {
		PQ = 0
		j = skipopts("parallel", i + 1, i1)
		for (e = j; e <= i1 && !OP[e] && WD[e] !~ /^(::::?\+?|--)$/; e++) ;
		ac = e == j
		if (!ac) { s = join(j, e - 1, PQ); push(s); ac = cmdat(s, "{") }
		if (!ac) return 0
		for (; e <= i1 && !OP[e]; e++) if (WD[e] ~ /^(:::\+?|--)$/) {
			for (e++; e <= i1 && !OP[e] && WD[e] !~ /^(::::?\+?|--)$/; e++) push(WD[e])
			e--
		}
		return 1
	}
	# hyperfine [OPTIONS] COMMAND...: each COMMAND is shell code, and so are the
	# values of --setup, --prepare, --conclude, --cleanup, --reference and
	# --shell (the shell is run with -c COMMAND: --shell python3).
	function hyperfine(i, i1,    j, x, o, v, n, k) {
		for (j = i + 1; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (x !~ /^-./) { code(j); continue }
			if (x == "--") { for (j++; j <= i1 && !OP[j]; j++) code(j); return }
			# o: the option; v: its value when attached (--setup=CMD, -sCMD); n: 1 if not
			o = x; v = ""; n = 1
			if (x ~ /^--/) { if (k = index(x, "=")) { o = substr(x, 1, k - 1); v = substr(x, k + 1); n = 0 } }
			else if (length(x) > 2) { o = substr(x, 1, 2); v = substr(x, 3); n = 0 }
			if (o ~ /^(-[spcS]|--(setup|prepare|cleanup|conclude|reference|shell))$/) {
				if (!n) push(v)
				else { if (j < i1 && !OP[j + 1]) code(j + 1); j++ }
			}
			else if (o == "-P" || o == "--parameter-scan") j += 2 + n
			else if (o == "-L" || o == "--parameter-list") j += 1 + n
			else if (o ~ /^(-[wmMrDun]|--(warmup|min-runs|max-runs|runs|parameter-step-size|style|sort|time-unit|export-[a-z]+|output|input|command-name|reference-name|min-benchmarking-time))$/) j += n
		}
	}
	# nodemon [OPTIONS] [SCRIPT]: --exec CMD (-x) is shell code; without it, a
	# *.py SCRIPT runs with python (its default execMap).
	function nodemon(i, i1,    j, x) {
		for (j = i + 1; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (x == "-x" || x == "--exec") { if (j < i1) code(j + 1); return }
			if (x ~ /^--exec=/) { push(substr(x, 8)); return }
			if (x ~ /^-([weidP]|-(watch|ext|ignore|delay|config|signal|cwd|polling-interval))$/) { j++; continue }
			if (x == "--" || x !~ /^-/) break
		}
		for (; j <= i1 && !OP[j]; j++) if (WD[j] !~ /^-/) { if (WD[j] ~ /\.py$/ && !PXM) found("python"); return }
	}
	# npx (npm exec, bunx, bun x, pnpm exec / dlx, yarn exec / dlx) [OPTIONS]
	# [--] COMMAND...: -c CMD (or --call, pnpm'"'"'s --shell-mode) is shell code;
	# else COMMAND is a command (a package'"'"'s bin, any program on PATH with -p
	# PKG, and in npm 6'"'"'s npx one on PATH anyway). npx [-y]
	# @modelcontextprotocol/inspector [OPTIONS] [--] COMMAND...: the MCP
	# inspector starts COMMAND (the server).
	function npx(i, i1,    j) {
		for (j = i + 1; j <= i1 && !OP[j] && WD[j] ~ /^-/; j++) {
			# (after --, an option is the command: npm reads none)
			if (WD[j] == "--") { j++; break }
			if (WD[j] == "-c" || WD[j] == "--call" || WD[j] == "--shell-mode") { if (j < i1) code(j + 1); return }
			if (WD[j] ~ /^--call=/) { push(substr(WD[j], 8)); return }
			if (index(NPV, " " WD[j] " ")) j++
		}
		if (j > i1) return
		if (WD[j] !~ /^@modelcontextprotocol\/inspector(@[^\/]*)?$/) { check(j, i1); return }
		for (j++; j <= i1 && !OP[j] && WD[j] ~ /^-/; j++) {
			if (WD[j] == "--") { j++; break }
			if (WD[j] ~ /^(-e|--(config|server|transport|server-url|header|method|tool-name|tool-arg|uri|prompt-name|prompt-args|log-level|target))$/) j++
		}
		if (j <= i1) check(j, i1)
	}
	# npm [OPTIONS] exec|x, pnpm [OPTIONS] exec|dlx, yarn [OPTIONS] exec|dlx,
	# bun x: the index of that word (the rest is read as npx'"'"'s), else 0.
	# Options are skipped (NPV: those that take a value); the first other word
	# is the command.
	function npmexec(b, i, i1,    j, x) {
		for (j = i + 1; j <= i1 && !OP[j]; j++) {
			x = WD[j]
			if (x ~ /^-/) { if (index(NPV, " " x " ")) j++; continue }
			if ((x == "exec" && b != "bun") || (x == "x" && (b == "npm" || b == "bun")) || (x == "dlx" && (b == "pnpm" || b == "yarn"))) return j
			return 0
		}
		return 0
	}
	# pixi [-v|--color C|...] run|exec [OPTIONS] [--] COMMAND...: what pixi runs
	# comes from its environment, so only a package installer is refused there:
	# pip install, python -m pip install, uv pip install and uv add change the
	# environment behind pixi.lock'"'"'s back (pixi exec: its cached environment,
	# behind its specs'"'"'). COMMAND is read by the scanner in pixi mode (PXM,
	# see pxinst), wrappers, shells and all. One word is a shell line to pixi
	# (pixi run "cd sub && pip install x"), queued as code; more are a program
	# and its arguments, each one word. Returns what check() does for it (a
	# shell reading stdin: pixi run bash <<EOF, 2 or 3, see mcode), and what
	# pixi run echo prints goes into the command'"'"'s pipeline (ECKD). What it
	# sets (PATH, a folder) is not the outer command'"'"'s.
	function pixi(i, i1,    j, k, s, pm, ps, pc, pe, r) {
		for (j = i + 1; j <= i1 && !OP[j] && WD[j] ~ /^-/; j++) if (WD[j] == "--color") j++
		if (j > i1 || OP[j]) return 0
		s = WD[j]; if (s == "r") s = "run"; else if (s == "x") s = "exec"
		if (s != "run" && s != "exec") return 0
		j = skipopts("pixi " s, j + 1, i1)
		for (; j <= i1 && OP[j]; j += 2) ;
		if (j > i1) return 0
		for (k = j + 1; k <= i1 && OP[k]; k += 2) ;
		pm = PXM; ps = PATHSET; pc = CDX; pe = ECKD; PXM = s; r = 0
		if (k > i1 && WD[j] !~ safe) push(WD[j])
		else { if (ECKD == CKD) ECKD = CKD + 1; r = check(j, i1) }
		PXM = pm; PATHSET = ps; CDX = pc; ECKD = pe
		return r
	}
	# Under pixi (PXM): is WD[i] (base name b) with its arguments a package
	# installer: pip install, python -m pip install (python'"'"'s options skipped,
	# -Im too), uv pip install, uv add? Then found() and 1.
	function pxinst(b, i, i1,    k, x, m, c, mod) {
		if (b ~ /^pip[0-9.]*$/) return pipargs(b, i + 1, i1)
		if (b == "uv") return uvargs("uv", i + 1, i1)
		if (b !~ /^(python|pypy)[0-9.]*$/) return 0
		spent(i1 - i)
		for (k = i + 1; k <= i1; k++) {
			if (OP[k]) { k++; continue }
			x = WD[k]
			# (a script, or - for stdin: not -m)
			if (x !~ /^-./ || x == "--") return 0
			if (x ~ /^--/) { if (x == "--check-hash-based-pycs") k++; continue }
			m = chars(x, OC); mod = "-"
			for (c = 2; c <= m; c++) {
				if (OC[c] == "c") return 0
				if (OC[c] == "m") {
					if (c < m) mod = substr(x, c + 1)
					else { for (k++; k <= i1 && OP[k]; k += 2) ; mod = k <= i1 ? WD[k] : "" }
					break
				}
				if (OC[c] == "W" || OC[c] == "X") { if (c == m) k++; break }
			}
			if (mod == "-") continue
			if (mod == "pip" || mod == "pip.__main__") return pipargs(b " -m pip", k + 1, i1)
			if (mod == "uv") return uvargs(b " -m uv", k + 1, i1)
			return 0
		}
		return 0
	}
	# pip'"'"'s arguments WD[k..i1] (w: how it was run): its options skipped
	# (PIPV: those that take a value), the first word that is a pip command
	# decides (another may be the value of an option not known here).
	function pipargs(w, k, i1,    x) {
		spent(i1 - k + 1)
		for (; k <= i1; k++) {
			if (OP[k]) { k++; continue }
			x = WD[k]
			if (x ~ /^-/) { if (index(PIPV, " " x " ")) k++; continue }
			if (!(x in PIPC)) continue
			if (x != "install") return 0
			found("pixi " PXM " " w " install"); return 1
		}
		return 0
	}
	# uv'"'"'s arguments WD[k..i1], the same way: uv add, uv pip install.
	function uvargs(w, k, i1,    x, p) {
		spent(i1 - k + 1); p = 0
		for (; k <= i1; k++) {
			if (OP[k]) { k++; continue }
			x = WD[k]
			if (x ~ /^-/) { if (index(UVV, " " x " ")) k++; continue }
			if (!p) {
				if (!(x in UVC)) continue
				if (x == "add") { found("pixi " PXM " " w " add"); return 1 }
				if (x != "pip") return 0
				p = 1; continue
			}
			if (!(x in UVPC)) continue
			if (x != "install") return 0
			found("pixi " PXM " " w " pip install"); return 1
		}
		return 0
	}
	# A rough look at range r (a >(...) body): does it start with a shell
	# reading stdin (bash, sh -s; not sh -c CODE or sh FILE)?
	function shin(r,    k, e, s, x, m, ok) {
		if (hit != "") return 0
		k = RLO[r]; e = RHI[r]; s = k; ok = -1
		while (k <= e) {
			while (k <= e && (ch[k] == " " || ch[k] == "\t" || ch[k] == "\n")) k++
			if (k > e || (ch[k] in SP)) break
			for (x = ""; k <= e && !(ch[k] in SP); k++) if (++m <= 64) x = x ch[k]
			m = 0
			if (ok < 0) { sub(/.*\//, "", x); if (!(ok = (x in SHELL))) break }
			else if (x !~ /^-/ || x ~ /^-[^-]*c/) { ok = 0; break }
		}
		spent(k - s)
		return ok > 0
	}
	# Does a command in s start with t? (A rough look: t at the start or after
	# ; & | ( ` or a newline. A t with other characters than these counts
	# anywhere.)
	function cmdat(s, t,    n, k, c, re) {
		n = chars(t, OC); re = ""
		for (k = 1; k <= n; k++) {
			c = OC[k]
			if (index(NM, c)) re = re c
			else if (index("{}%@#+,.!?=/~-", c)) re = re "[" c "]"
			else return index(s, t) > 0
		}
		return s ~ ("(^|[;&|(\n`])[ \t]*" re)
	}
	# Shell code in word j: queue it. If it is one $(...), what that prints is
	# code too: bash -c "$(cat <<EOF ... EOF)".
	function code(j) { push(WD[j]); if (WD[j] == "$()") feed(j) }
	# What the range queued for word j ($(...) or <(...), see endword) prints
	# is code: its heredocs, echo / printf words and here-strings are read as
	# commands, as if piped into a shell.
	function feed(j) { if (PR[j]) RF[PR[j]] = mcode() }
	# A shell (or source) reading stdin at WD[i0..i1]: a here-string, or a
	# <(...) redirected in, is code.
	function stdin(i0, i1,    j) {
		for (j = i0; j < i1; j++) if (OP[j]) {
			if (WD[j] == "<<<") code(j + 1)
			else if (WD[j] == "<" && WD[j + 1] == "<()") feed(j + 1)
		}
	}
	# su / runuser: queue the -c command. runuser -u USER [--] CMD runs CMD.
	function su(i, i1,    j, x, k, m, c, ru, u) {
		ru = (WD[i] ~ /runuser$/); u = 0
		for (j = i + 1; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (x == "--") { if (ru) { check(j + 1, i1); return } continue }
			if (x ~ /^--(session-)?command=/) { push(substr(x, index(x, "=") + 1)); return }
			if (x == "--command" || x == "--session-command") { if (j < i1) code(j + 1); return }
			if (x ~ /^--user(=|$)/) { u = 1; if (x == "--user") j++; continue }
			if (x ~ /^--(group|supp-group|shell|whitelist-environment)$/) { j++; continue }
			if (x ~ /^--/) continue
			if (x ~ /^-[A-Za-z]/) {
				m = chars(x, OC)
				for (k = 2; k <= m; k++) {
					c = OC[k]
					if (c == "c") { if (k < m) push(substr(x, k + 1)); else if (j < i1) code(j + 1); return }
					# Options that take a value: -u USER (runuser), -g, -G, -s, -w.
					if (index("ugGsw", c)) { if (c == "u") u = 1; if (k == m) j++; break }
				}
				continue
			}
			if (ru && u) { check(j, i1); return }
		}
	}
	# A package manager: refuse installing Python or Python tooling with it.
	function pkg(i, i1, b,    j, x, v, nm) {
		v = ""
		for (j = i + 1; j <= i1; j++) {
			if (OP[j]) { j++; continue }
			x = WD[j]
			if (v == "") {
				if (b == "pacman") { if (x == "--sync" || (x ~ /^-[A-Za-z]*S[A-Za-z]*$/ && x !~ /^-[A-Za-z]*[silgpcw]/)) v = x; continue }
				if (b == "nix-env") { if (x == "--install" || x ~ /^-[A-Za-z]*i/) v = x; continue }
				if (x ~ /^-/) { if (length(x) == 2 && index(OS[b], substr(x, 2, 1))) j++; continue }
				if (x !~ verbs) return
				v = x; continue
			}
			if (x ~ /^-/) continue
			nm = tolower(x); sub(/.*\//, "", nm); sub(/^nixpkgs\./, "", nm); sub(/[=:].*/, "", nm)
			if (nm ~ pypkg) { found(b " " v " " x); return }
		}
	}
	function found(name) {
		if (hit != "") return
		gsub(/[^ -~]/, "?", name); gsub(/`/, "'"'"'", name)
		if (length(name) > 60) name = substr(name, 1, 60) "..."
		hit = name; HV = VIEW
	}
	'"$RJOIN$ASKREC" &
}

# "ask" if a PIXI_GUARD=ask is in $1 (as scan() looks for one in a command it
# does not read).
askany() {
	printf '%s' "$1" | awk 'BEGIN { RS = "\001" } askrec($0) { print "ask"; exit }'"$ASKREC"
}

# "ask" if a PIXI_GUARD=ask assignment is in JSON text $1 (a payload too big to
# read, see jindex): as askany, also after a quote and before one, or an
# escaped one (["PIXI_GUARD=ask", ...], "PIXI_GUARD=\"ask\" ...").
askjson() {
	printf '%s' "$1" | awk 'BEGIN { RS = "\001" } index($0, "PIXI_GUARD=") && $0 ~ /(^|[;&|( \t"])PIXI_GUARD=(\\"ask\\"|\047ask\047|ask)([ \t"]|\\[nt])/ { print "ask"; exit }'
}

# scan()'s output, or "complex" (and "ask", see askany) if the scan is still
# running after PIXI_GUARD_WATCHDOG seconds (1-7, default 7; for tests). Claude
# Code's hook timeout is 10 s, and a hook that times out lets the command
# through, so a scan that takes too long is refused like one over budget. A
# sleeper times it; the scan's side kills the sleeper as soon as the scan ends
# (no sleeper is left behind), and kills the scan's awk when told the time is up
# (and waits for it, so nothing is left running either).
watched_scan() {
	wd=${PIXI_GUARD_WATCHDOG:-7}
	case $wd in [1-7]) ;; *) wd=7 ;; esac
	sleep "$wd" >/dev/null 2>&1 &
	zp=$!
	{
		trap 'kill -s KILL $! 2>/dev/null; wait; exit 1' TERM
		scan "$1"
		wait $!
		kill $zp 2>/dev/null
	} &
	gp=$!
	# (the sleeper's status: 0 if it slept its time out, else it was killed)
	if wait $zp 2>/dev/null; then
		kill $gp 2>/dev/null
		wait $gp 2>/dev/null
		echo complex
		askany "$1"
	else
		wait $gp 2>/dev/null
	fi
}

# Is Claude Code's shell bash 3.x (b32=1) or bash 5.2 and later (b52=1)? It
# runs commands with $SHELL (or CLAUDE_CODE_SHELL) when that is bash or zsh.
# macOS's /bin/bash is 3.2, which ends some $(...) elsewhere than bash 5 and
# zsh do (see cpar32); bash 5.2 and later end a heredoc in $(...) at a line
# that starts with its delimiter and has a ")" after it (see body_end). With
# $1, the same for that shell whatever its name: /bin/sh, which runs
# checkpoint's jobs (bash 3.2 on macOS; dash, which has no --version, on
# Debian).
bashver() {
	if [ -n "${1:-}" ]; then
		bsh=$1
	else
		bsh=${CLAUDE_CODE_SHELL:-${SHELL:-}}
		case ${bsh##*/} in bash*) ;; *) return 0 ;; esac
	fi
	case $("$bsh" --version </dev/null 2>/dev/null) in
	*"version "[0-3].*) b32=1 ;;
	*"version 5."[2-9]* | *"version 5."[1-9][0-9]* | *"version "[6-9].* | *"version "[1-9][0-9]*) b52=1 ;;
	esac
	return 0
}

# The active virtual and conda envs, one per line (not a pixi one: pixi shell
# sets CONDA_PREFIX too, and a detached pixi env lives outside .pixi/envs but
# has pixi's conda-meta/pixi file).
envdirs() {
	for d in "${VIRTUAL_ENV:-}" "${CONDA_PREFIX:-}"; do
		case $d in "" | */.pixi/envs/*) continue ;; esac
		[ -f "$d/conda-meta/pixi" ] && continue
		[ -d "$d/bin" ] && printf '%s\n' "$d"
	done
}

# First KB of file $1 (a script's #! line, without reading a big file whole).
head1k() { dd if="$1" bs=1024 count=1 2>/dev/null; }

# Is file $1 a Python script: a #! line that names python or pypy, or pip's
# #!/bin/sh with '''exec' on the next line (used for long paths)?
is_pyscript() {
	head1k "$1" | awk '
		NR == 1 { f = ($0 ~ /^#!.*(python|pypy)/); s = ($0 ~ /^#![ \t]*\/bin\/sh/); next }
		NR == 2 { if (s && $0 ~ /^\047\047\047exec\047/) f = 1; exit }
		END { exit !f }'
}

# Does file $1 start with a #! line that runs pixi (#!/usr/bin/env -S pixi exec
# --spec rich -- python, or a path to pixi)?
pixi_shebang() {
	head1k "$1" | awk '
		NR == 1 {
			if (substr($0, 1, 2) == "#!") {
				$0 = substr($0, 3)
				for (k = 1; k <= NF; k++) {
					b = $k; sub(/.*\//, "", b)
					if (k == 1 && b == "env") continue
					if (k > 1 && ($k ~ /^-/ || $k ~ /^[A-Za-z_][A-Za-z0-9_]*=/)) continue
					f = (b == "pixi"); break
				}
			}
			exit
		}
		END { exit !f }'
}

# Is folder $1 a pixi environment's bin: ~/.pixi/bin, $PIXI_HOME/bin, a
# .pixi/envs/NAME/bin (a workspace's; pixi global's under ~/.pixi),
# $PIXI_HOME/envs/NAME/bin, or that of any env with pixi's conda-meta/pixi
# file (detached environments)?
pixibin() {
	case $1 in /*/bin) ;; *) return 1 ;; esac
	e=${1%/bin}
	[ -n "${HOME:-}" ] && [ "$1" = "${HOME%/}/.pixi/bin" ] && return 0
	[ -n "${PIXI_HOME:-}" ] && { [ "$e" = "${PIXI_HOME%/}" ] || [ "${e%/*}" = "${PIXI_HOME%/}/envs" ]; } && return 0
	case ${e%/*} in */.pixi/envs) return 0 ;; esac
	[ -f "$e/conda-meta/pixi" ]
}

# The scan left these to the shell (see its END), in $res. pyscript: the base
# name of the first *.py script run by its path ("py PATH", a relative one
# from $cwd) that does not start with a #! line that runs pixi. toolpath:
# "NAME TAG" for the first tool run by its bare name ("tool NAME TAG") that
# does not resolve into a pixi environment's bin: where the hook's PATH finds
# it, or if nowhere, ~/.pixi/bin or $PIXI_HOME/bin (Claude Code's Bash may
# have them on its PATH when Claude Code itself does not). envbin: "NAME
# DIR" for the first bare program name ("prog NAME") that is a Python script
# in DIR/bin, DIR an active virtual or conda env.
pyscript() {
	printf '%s\n' "$res" | sed -n 's/^py //p' | while IFS= read -r p; do
		case $p in /*) f=$p ;; "~/"*) f=$HOME/${p#"~/"} ;; *) f=$cwd/$p ;; esac
		[ -f "$f" ] && pixi_shebang "$f" && continue
		printf '%s\n' "${p##*/}"
		break
	done
}
toolpath() {
	# (names of the characters A-Za-z0-9._+- only)
	printf '%s\n' "$res" | sed -n 's/^tool //p' | while read -r p t; do
		f=$(command -v "$p" 2>/dev/null) || f=
		case $f in
		/*) pixibin "${f%/*}" && continue ;;
		"")
			for d in "${PIXI_HOME:+${PIXI_HOME%/}/bin}" "${HOME:+${HOME%/}/.pixi/bin}"; do
				[ -n "$d" ] && [ -f "$d/$p" ] && [ -x "$d/$p" ] && continue 2
			done ;;
		esac
		printf '%s %s\n' "$p" "$t"
		break
	done
}
envbin() {
	envdirs | while IFS= read -r d; do
		# (names of the characters A-Za-z0-9._+- only: no globbing or splitting)
		for p in $(printf '%s\n' "$res" | sed -n 's/^prog //p'); do
			if [ -f "$d/bin/$p" ] && is_pyscript "$d/bin/$p"; then
				printf '%s %s\n' "$p" "$d"
				exit 0
			fi
		done
	done
}

# cwd: the payload's, as a physical path, so the wrapper key matches `guard.sh
# wrap-path` (/tmp vs /private/tmp). A job (checkpoint's or the gateway's)
# runs in its own cwd (tool_input's $jc, relative to the session's) when it
# gives one that exists (checkpoint and the gateway refuse the job otherwise).
# A gateway job with none runs in the project: $CLAUDE_PROJECT_DIR (the
# gateway's MCP server has it too), else the session's folder; also one in a
# workspace (the MCP server fills in the cwd before the workspace applies, and
# the gateway refuses a cwd outside it). A gateway terminal's folder is not
# known: the session's stands in for the advice (and relative *.py paths are
# not looked up there, see TCD).
getcwd() {
	[ -n "${cwd:-}" ] && return
	cwd=$(field cwd)
	[ -n "$cwd" ] || cwd=$PWD
	cwd=$(cd "$cwd" 2>/dev/null && pwd -P) || cwd=$PWD
	[ -z "$jc" ] && [ "$job" = gw ] && jc=${CLAUDE_PROJECT_DIR:-}
	if [ -n "$jc" ]; then
		case $jc in /*) ;; *) jc=$cwd/$jc ;; esac
		jc=$(cd "$jc" 2>/dev/null && pwd -P) && cwd=$jc
	fi
	return 0
}

out_decision() {
	printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"%s","permissionDecisionReason":%s}}\n' \
		"$1" "$(jstr "$2")"
}

case $mode in
wrap-path)
	d=$(cd "${2:-.}" 2>/dev/null && pwd -P) || exit 1
	classify "$d"
	if [ -n "$proj_manifest" ]; then printf '%s\n' "$proj_manifest"; else wrap_manifest "${proj_root:-$d}"; echo; fi
	;;

session)
	msg="pixi-guard: on this machine Python runs only through pixi. Use \`pixi run ...\` in a pixi workspace, \`pixi exec [--spec pkg] python ...\` for one-offs, \`pixi add pkg\` (conda-forge) or \`pixi add --pypi pkg\` for packages. Never run python, pip, uv, conda, poetry and the like directly, nor \`pixi run pip install\`: commands that do are refused (Bash, Monitor, the desktop app's terminal tool, checkpoint's job_start, and the gateway's job_start and terminal_send: make a job's command \`pixi run python train.py\`, not \`python train.py\`). For a project that is not on pixi, load the skill pixi-guard:project (convert it, or wrap it if it isn't the user's code)."
	if p=$(pixi_where); then
		command -v pixi >/dev/null 2>&1 ||
			msg="$msg pixi is installed at $p, which may not be on PATH: if \`pixi\` is not found in Bash, run it by that path."
		printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' "$(jstr "$msg")"
	else
		msg="$msg pixi was not found on this machine (not on PATH, nor in ~/.pixi/bin, ~/.local/bin, Homebrew or MacPorts). Check with \`command -v pixi\`; if it is really missing, the first time Python comes up, tell the user and offer to install it ($PIXI_INSTALL)."
		sys="pixi-guard: pixi not found, so Claude can't run Python here until it is installed. Get it from https://pixi.sh"
		printf '{"systemMessage":%s,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' \
			"$(jstr "$sys")" "$(jstr "$msg")"
	fi
	prune_told
	;;

pre)
	# A command string (Bash, Monitor, the terminal tool, a job_start), else
	# a job_start's argv list (checkpoint takes the command when it is not
	# empty; the gateway refuses a job with both), or the text typed into a
	# gateway terminal (terminal_send). checkpoint runs a job detached with
	# /bin/sh, in the job's cwd (job=check); the gateway runs a job in the
	# job's cwd (job=gw) and a terminal's text (term=1) in its own bash.
	# A payload over 1MB is not read at all: jindex's walk takes up to about
	# 0.9 s per MB (under gawk), and it runs before the scan's watchdog starts.
	# (A command the scan reads is at most 256KB, which JSON writes in well
	# under 1MB unless most of it is control characters.)
	n=$(printf '%s' "$input" | wc -c)
	# (unquoted: wc may print blanks before the number)
	if [ ${n:-0} -gt 1048576 ]; then jix=cut; else jix=$(jindex); fi
	# (see jindex: a payload too big to read is refused, as a command too big to
	# scan is; PIXI_GUARD=ask still asks)
	if [ "$jix" = cut ]; then
		if [ -n "$(askjson "$input")" ]; then
			out_decision ask "pixi-guard: this tool call is too large for pixi-guard to read. Claude asked to run it anyway; allow it?"
		else
			out_decision deny "pixi-guard: refused, this tool call's input is too large (or too deeply nested) for pixi-guard to read, so it can't check whether it runs Python outside pixi. Split it into smaller commands. Only if the user explicitly wants it run as it is, put \`PIXI_GUARD=ask\` in front of its command: that shows them a permission prompt. Don't use that to get around pixi on your own."
		fi
		exit 0
	fi
	job= term= hostid= jc=
	case $(field tool_name) in
	mcp__plugin_check_jobs__job_start) job=check ;;
	mcp__plugin_gate_gw__job_start)
		job=gw
		# A job on another host runs there: not checked (like ssh HOST python).
		# No host, "" and localhost are this machine, and so (to the gateway)
		# is its own m_ id, which is not known here: an m_ id is checked.
		h=$(field host tool_input)
		case $h in
		"" | localhost) ;;
		# (m_ and 26 of a-z2-7; no ranges: their meaning depends on the locale)
		m_*) case ${h#m_} in *[!abcdefghijklmnopqrstuvwxyz234567]*) exit 0 ;; esac; [ ${#h} -eq 28 ] || exit 0; hostid=1 ;;
		*) exit 0 ;;
		esac ;;
	mcp__plugin_gate_gw__terminal_send) term=1 ;;
	esac
	# (tool_input's top-level keys only, see jindex)
	if [ -n "$term" ]; then
		cmd=$(field text tool_input)
	else
		cmd=$(field command tool_input)
		[ -n "$cmd" ] || { cmd=$(argvcmd); [ -n "$job" ] || job=check; }
	fi
	[ -n "$cmd" ] || exit 0
	[ -n "$job" ] && jc=$(field cwd tool_input)
	# (read by scan: see B32, B52, VB, TCD and GWP there)
	b32=0 b52=0 vb=0 tcd=0 gwp=0 cwd=
	case $job$term in
	check) bashver "${PIXI_GUARD_JOB_SH:-/bin/sh}" ;;
	# The gateway's own bash: bash 5 (its pixi.toml pins "5.*", locked at
	# 5.2.37), with the gateway's own PATH (gwp).
	gw) b52=1 gwp=1 ;;
	1) b52=1 gwp=1 tcd=1 ;;
	*) bashver ;;
	esac
	[ -n "${VIRTUAL_ENV:-}${CONDA_PREFIX:-}" ] && [ -n "$(envdirs)" ] && vb=1
	# (stderr off: bash reports a killed background job there)
	res=$(exec 2>/dev/null; watched_scan "$cmd")
	has() { printf '%s\n' "$res" | grep -qx "$1"; }
	hit=$(printf '%s\n' "$res" | sed -n 's/^hit //p')
	complex= tag=-
	has complex && complex=1
	has b32 && tag=b32
	has b52 && tag=b52
	note=
	if [ -z "$hit" ]; then
		[ -z "$complex" ] && case "
$res" in *"
py "*) getcwd; hit=$(pyscript) ;; esac
		# (a tool that runs outside pixi says more than "too large")
		if [ -z "$hit" ] && t=$(toolpath) && [ -n "$t" ]; then
			hit=${t%% *} tag=${t#* } complex=
		fi
		if [ -z "$hit$complex" ] && [ "$vb" = 1 ] && e=$(envbin) && [ -n "$e" ]; then
			hit=${e%% *}
			note=" (\`$hit\` is a Python script in ${e#* }/bin, the active environment's.)"
		fi
	fi
	[ -n "$hit$complex" ] || exit 0
	case $hit in
	*.py)
		if has written; then
			note="$note This same command writes \`$hit\`, and pixi-guard reads a script's #! line from disk before the command runs, so it would go by the old file (or none). Write the file first (with the Write tool, or in a separate command), then run it by its path in a command of its own: a *.py script whose #! line runs pixi, such as \`#!/usr/bin/env -S pixi exec --spec <pkg> -- python\`, is let through then."
		else
			note="$note A *.py script whose #! line runs pixi, such as \`#!/usr/bin/env -S pixi exec --spec <pkg> -- python\`, may be run by its path."
			if [ -n "$term" ]; then
				has pycd && note="$note pixi-guard doesn't know a terminal's folder, so a relative path is not looked up there: type its absolute path."
			else
				has pycd && note="$note A relative path is not looked up after \`cd\` (or another change of folder) in the same command: run it by its absolute path, or from the current folder."
			fi
		fi ;;
	esac
	# How to say it: for a job, its command and the shell that runs it (see
	# bashver); for a terminal, what is typed into it.
	case $job$term in
	check | gw)
		what=job shis="checkpoint runs the job with /bin/sh, which here is"
		[ "$job" = gw ] && shis="the gateway runs the job with its own bash, which is"
		again="start the job again with \`PIXI_GUARD=ask\` in front of its command (with \`argv\`, as its first element)" ;;
	1)
		what="terminal input" shis="the gateway's terminal runs its own bash, which is"
		again="send it again as \`PIXI_GUARD=ask <command>\`" ;;
	*)
		what=command shis="Claude Code's shell here is"
		again="re-run it as \`PIXI_GUARD=ask <command>\`" ;;
	esac
	[ -n "$hostid" ] && note="$note (If \`host\` is another machine's id, name that host as gateway.toml does: pixi-guard doesn't check jobs on other hosts.)"
	if has pathset; then
		if [ "$gwp" = 1 ]; then
			gws="The gateway runs the job"; [ -n "$term" ] && gws="The gateway's terminal runs it"
			note="$note $gws with a PATH of its own (the gateway's folders, gateway.toml's [shell] import_path if set, then /usr/bin, /bin, /usr/sbin and /sbin), not the one pixi-guard sees, so pixi-guard can't tell where \`$hit\` would come from there."
		else
			note="$note This command runs it with a PATH of its own (it sets PATH, or uses sudo, su, env -i or xcrun), so pixi-guard can't tell where \`$hit\` would come from."
		fi
		note="$note One installed with pixi global or in a pixi environment can be run by its path (\`~/.pixi/bin/$hit\`, \`.pixi/envs/default/bin/$hit\`)."
	fi
	[ "$tag" = b32 ] &&
		note="$note Note: $shis bash 3.2 (macOS's /bin/bash). It ends a \`\$(...)\` at the first \`)\` outside the quotes it sees, even one in a quoted heredoc or a case pattern, and there apostrophes pair up as quotes (Don't ... it's). What follows that \`)\` is read as the rest of the command, where backticks and \`\$(...)\` run. For a commit message or PR body, write it to a file and use \`git commit -F FILE\` (\`gh pr create --body-file FILE\`)."
	[ "$tag" = b52 ] &&
		note="$note Note: $shis bash 5.2 or later. Inside \`\$(...)\` it ends a heredoc at a line that starts with the delimiter and has a \`)\` after it (such as \`EOFError ... :)\`), and reads the rest of that line as commands; that \`)\` can end the \`\$(...)\`, and backticks and \`\$(...)\` after it run. For a commit message or PR body, write it to a file and use \`git commit -F FILE\` (\`gh pr create --body-file FILE\`)."
	# A package installer run by pixi (pixi run pip install, see pixi() in
	# scan) is in pixi, but behind its lock file's back.
	case $hit in
	"pixi "*) px=1 runs="runs \`$hit\`, which installs packages behind pixi's back" how="this way" ;;
	*) px= runs="runs \`$hit\` outside pixi" how="outside pixi" ;;
	esac
	override="Only if the user explicitly wants it run $how, $again: that shows them a permission prompt. Don't use that to get around pixi on your own."

	if printf '%s\n' "$res" | grep -qx ask; then
		if [ -n "$complex" ]; then
			out_decision ask "pixi-guard: this $what is too large or nested for pixi-guard to check for Python. Claude asked to run it anyway; allow it?"
		else
			out_decision ask "pixi-guard: this $what $runs. Claude asked to run it this way anyway; allow it?"
		fi
		exit 0
	fi
	if [ -n "$complex" ]; then
		out_decision deny "pixi-guard: refused, this $what is too large or too deeply nested for pixi-guard to check whether it runs Python outside pixi. Split it into smaller commands. $override"
		exit 0
	fi

	getcwd
	case $hit in
	"pixi exec "*) why="pixi-guard: refused, \`$hit\` installs into a cached \`pixi exec\` environment, which later \`pixi exec\` runs with the same specs reuse.$note" ;;
	"pixi "*) why="pixi-guard: refused, \`$hit\` changes the pixi environment behind pixi.lock's back (pixi warns about that, and the next \`pixi install\` can undo it).$note" ;;
	*) why="pixi-guard: refused, \`$hit\` would run Python outside pixi.$note" ;;
	esac

	# (a pixi run ... refusal: pixi is there, or the command fails anyway)
	if [ -z "$px" ] && ! pixi_where >/dev/null; then
		sid=$(field session_id | tr -c 'A-Za-z0-9_-' '_' | cut -c 1-128)
		if [ -n "$sid" ] && [ -e "$told/$sid" ]; then
			todo="pixi is still not found (the user has been told). Wait for the user to install it, or, if they said to go ahead without pixi, $again so they can approve it."
		else
			if [ -n "$sid" ] && mkdir -p "$told" 2>/dev/null; then
				: > "$told/$sid" 2>/dev/null
				prune_told
			fi
			todo="pixi was not found on this machine (not on PATH, nor in ~/.pixi/bin, ~/.local/bin, Homebrew or MacPorts). Check with \`command -v pixi\`: if Bash finds it, use it (\`pixi run ...\`). Otherwise stop and tell the user now: Python here is meant to run through pixi, and it can be installed with $PIXI_INSTALL. Ask whether to install it (you can run the install for them on a yes) or to go ahead without pixi this time; in that case $again so they approve it."
		fi
		out_decision deny "$why $todo"
		exit 0
	fi

	# Installers and env managers map to `pixi add`; everything else to `pixi run`.
	case $hit in
	"pixi exec "*)
		use="name the packages a one-off needs with \`pixi exec --spec <pkg> ...\` (\`--with <pkg>\` keeps the one guessed from the command); for ongoing work, a workspace and \`pixi add <pkg>\`" ;;
	"pixi "*)
		use="add packages with \`pixi add <pkg>\` (conda-forge, preferred) or \`pixi add --pypi <pkg>\` (the project itself: \`pixi add --pypi --editable \"<name> @ file://\$PWD\"\`), so they go in pixi.lock; then \`pixi run ...\`" ;;
	pip* | uv | uvx | easy_install* | conda | mamba | micromamba | poetry | pipenv | pyenv | virtualenv | hatch | pdm | rye | "venv activate" | *" "*)
		use="manage packages with \`pixi add <pkg>\` (conda-forge, preferred) or \`pixi add --pypi <pkg>\`, and the environment with \`pixi install\` / \`pixi run\`; no separate venv or installer" ;;
	*)
		run="run it as \`pixi run $hit ...\`"
		[ -n "$job" ] && run="make the job's command \`pixi run $hit ...\` (with \`argv\`, put \"pixi\", \"run\" first)"
		use="$run (or a task from \`pixi task list\`); add missing packages with \`pixi add <pkg>\` or \`pixi add --pypi <pkg>\`" ;;
	esac

	classify "$cwd"
	case $proj_kind in
	pixi)
		todo="$proj_root is a pixi workspace: $use."
		;;
	wrapped)
		todo="$proj_root is wrapped by the pixi workspace $proj_manifest (kept outside the folder). Use it with \`--manifest-path '$proj_manifest'\` on every pixi command: $use."
		;;
	python)
		todo="$proj_root is a Python project that is not on pixi. Load the skill pixi-guard:project and follow it: ask the user once whether to convert this project to pixi (their own code) or wrap it without changing tracked files (someone else's code). If nothing may be added to the folder at all, put the wrapper at $(wrap_manifest "$proj_root")."
		;;
	*)
		todo="No pixi workspace here. For a one-off use \`pixi exec --spec python --spec <pkg> python ...\`; for a script, \`pixi init --script file.py\` and \`pixi exec\`; for ongoing work, \`pixi init\` (ask the user before creating a workspace)."
		;;
	esac
	# (a job: the pixi command goes in the job, not in Bash)
	case ${job:+1}$proj_kind in
	1python) todo="$todo Then $use." ;;
	1none) todo="$todo The job's command is then that pixi command, such as \`pixi exec --spec python python ...\` (with \`argv\`, \"pixi\" first)." ;;
	esac
	out_decision deny "$why $todo $override"
	;;
esac
exit 0
