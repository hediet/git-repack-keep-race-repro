#!/bin/sh

set -eu

if test "$1" = pack-objects; then
	test ! -e "$RACE_EVENTS"
	printf 'pack-objects arguments: %s\n' "$*" >"$RACE_EVENTS"
	test ! -e "$RACE_KEEP"

	GIT_EXEC_PATH="$REAL_EXEC_PATH" "$REAL_GIT" --git-dir="$RACE_REPO" \
		index-pack --stdin --keep="controlled duplicate reception" \
		<"$RACE_PACK" >"$RACE_RECEPTION_LOG"

	test -f "$RACE_KEEP"
	printf 'duplicate index-pack completed; keep file exists\n' >>"$RACE_EVENTS"
fi

GIT_EXEC_PATH="$REAL_EXEC_PATH" exec "$REAL_GIT" "$@"
