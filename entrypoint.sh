#!/bin/sh
# Права как у стандартных контейнеров Unraid: nobody:users (99:100)
PUID="${PUID:-99}"
PGID="${PGID:-100}"
mkdir -p "${CONFIG_DIR:-/config}"
chown -R "$PUID:$PGID" "${CONFIG_DIR:-/config}"
exec su-exec "$PUID:$PGID" python /app/app.py
