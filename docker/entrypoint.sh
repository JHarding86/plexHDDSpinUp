#!/bin/sh
# Drop to PUID:PGID (Unraid convention: 99:100 = nobody:users) and run the app.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$CONFIG_DIR"
    chown -R "$PUID:$PGID" "$CONFIG_DIR"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
