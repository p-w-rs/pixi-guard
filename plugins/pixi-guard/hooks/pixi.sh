#!/bin/sh
# Find and run pixi, also when Claude Code was started with a minimal PATH
# (the desktop app opened from the Dock gets /usr/bin:/bin:/usr/sbin:/sbin).
#
#   sh pixi.sh --where   print the pixi it would run; exit 1 if there is none
#   sh pixi.sh ARGS...   run that pixi with ARGS
#
# PIXI_GUARD_PIXI names one pixi to use and turns the search off (tests use it).

where() {
	if [ -n "${PIXI_GUARD_PIXI:-}" ]; then
		command -v "$PIXI_GUARD_PIXI" 2>/dev/null
		return
	fi
	command -v pixi 2>/dev/null && return 0
	for p in "${PIXI_HOME:-$HOME/.pixi}/bin/pixi" "$HOME/.pixi/bin/pixi" "$HOME/.local/bin/pixi" \
		"${XDG_DATA_HOME:-$HOME/.local/share}/pixi/bin/pixi" /opt/homebrew/bin/pixi /usr/local/bin/pixi \
		/opt/local/bin/pixi /home/linuxbrew/.linuxbrew/bin/pixi; do
		if [ -f "$p" ] && [ -x "$p" ]; then
			printf '%s\n' "$p"
			return 0
		fi
	done
	return 1
}

if [ "${1:-}" = --where ]; then
	where
	exit
fi
p=$(where) || {
	echo "pixi not found (looked on PATH and in the usual install folders); install it from https://pixi.sh" >&2
	exit 127
}
exec "$p" "$@"
