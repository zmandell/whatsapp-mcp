#!/bin/bash
set -euo pipefail

die() { echo "entrypoint: $*" >&2; exit 1; }

need_secret() {
	local v="${!1:-}"
	[[ ${#v} -ge 32 ]] || die "$1 must be set to at least 32 characters"
}
need_path_secret() {
	need_secret "$1"
	[[ "${!1}" =~ ^[A-Za-z0-9_-]+$ ]] || die "$1 may only contain letters, digits, '_' and '-'"
}
need_host() {
	[[ "${!1:-}" =~ ^[A-Za-z0-9.-]+$ ]] || die "$1 must be set to a bare hostname"
}

for v in WHATSAPP_BEARER_TOKEN MONARCH_BEARER_TOKEN; do need_secret "$v"; done
for v in WHATSAPP_PATH_SECRET MONARCH_PATH_SECRET; do need_path_secret "$v"; done
for v in WHATSAPP_HOST MONARCH_HOST; do need_host "$v"; done
[[ "$WHATSAPP_HOST" != "$MONARCH_HOST" ]] || die "WHATSAPP_HOST and MONARCH_HOST must differ"

# Disk is mounted root-owned at runtime; prepare it, then drop privileges.
if [ "$(id -u)" = 0 ]; then
	mkdir -p /data/whatsapp/store /data/monarch-home
	chmod 700 /data/monarch-home
	chown -R 10001:10001 /data
	# whatsapp.py reads ../whatsapp-bridge/store/messages.db relative to itself.
	ln -sfn /data/whatsapp/store /app/whatsapp-bridge/store
	exec setpriv --reuid=10001 --regid=10001 --clear-groups "$0" "$@"
fi

PORT="${PORT:-10000}"
BRIDGE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
BASE_ENV=(PATH="$PATH" HOME=/tmp PYTHONUNBUFFERED=1)

pids=()
supervise() {
	local name=$1
	shift
	(
		child=
		trap 'kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 0' TERM INT
		while true; do
			echo "[$name] starting"
			"$@" &
			child=$!
			wait "$child" || true
			echo "[$name] exited; restarting in 5s"
			sleep 5 &
			wait $!
		done
	) &
	pids+=("$!")
}
trap 'echo "shutting down"; kill "${pids[@]}" 2>/dev/null || true; wait; exit 0' TERM INT

supervise bridge env -i "${BASE_ENV[@]}" BRIDGE_API_TOKEN="$BRIDGE_API_TOKEN" \
	bash -c 'cd /data/whatsapp && exec /usr/local/bin/whatsapp-bridge'

supervise whatsapp-mcp env -i "${BASE_ENV[@]}" \
	/usr/local/bin/mcp-proxy --port 8765 --host 127.0.0.1 \
	-e BRIDGE_API_TOKEN "$BRIDGE_API_TOKEN" \
	/app/whatsapp-mcp-server/.venv/bin/python -- /app/whatsapp-mcp-server/main.py

supervise monarch-mcp env -i PATH="$PATH" HOME=/data/monarch-home PYTHONUNBUFFERED=1 \
	MONARCH_MCP_TRANSPORT=streamable-http \
	MONARCH_MCP_HOST=127.0.0.1 \
	MONARCH_MCP_PORT=8767 \
	MONARCH_MCP_ALLOWED_HOSTS="$MONARCH_HOST" \
	MONARCH_MCP_READ_ONLY="${MONARCH_MCP_READ_ONLY:-1}" \
	/opt/monarch/.venv/bin/monarch-mcp-server

supervise caddy env -i PATH="$PATH" HOME=/tmp \
	XDG_DATA_HOME=/tmp/xdg-data XDG_CONFIG_HOME=/tmp/xdg-config \
	PORT="$PORT" \
	WHATSAPP_HOST="$WHATSAPP_HOST" \
	WHATSAPP_BEARER_TOKEN="$WHATSAPP_BEARER_TOKEN" \
	WHATSAPP_PATH_SECRET="$WHATSAPP_PATH_SECRET" \
	MONARCH_HOST="$MONARCH_HOST" \
	MONARCH_BEARER_TOKEN="$MONARCH_BEARER_TOKEN" \
	MONARCH_PATH_SECRET="$MONARCH_PATH_SECRET" \
	/usr/local/bin/caddy run --config /opt/hub/Caddyfile --adapter caddyfile

wait
