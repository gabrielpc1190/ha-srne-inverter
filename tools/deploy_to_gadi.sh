#!/usr/bin/env bash
# Deploy custom_components/srne_inverter to the GADI Home Assistant.
# PRODUCTION: only run with Gabriel's explicit OK.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="GADI-HomeAssistant"
DEST="/config/custom_components"

echo "== files to ship =="
find "$REPO/custom_components/srne_inverter" -name '__pycache__' -prune -o -type f -print

read -r -p "Copy to $HOST:$DEST and restart HA core? [y/N] " answer
[ "$answer" = "y" ] || { echo "aborted"; exit 1; }

ssh "$HOST" "mkdir -p $DEST"
rsync -av --delete --exclude '__pycache__' \
  "$REPO/custom_components/srne_inverter/" "$HOST:$DEST/srne_inverter/"

ssh "$HOST" "ls -la $DEST/srne_inverter"
ssh "$HOST" "ha core restart"
echo "restart issued; wait ~40 s then check the log:"
echo "  ssh $HOST 'ha core logs | grep -i srne_inverter'"
