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

# tar over ssh, not rsync: the HA OS SSH add-on's Alpine shell has no rsync binary.
ssh "$HOST" "mkdir -p $DEST/srne_inverter && rm -rf $DEST/srne_inverter/*"
tar -C "$REPO/custom_components/srne_inverter" --exclude='__pycache__' -cf - . \
  | ssh "$HOST" "tar -C $DEST/srne_inverter -xf -"

ssh "$HOST" "find $DEST/srne_inverter -type f | sort"
ssh "$HOST" "ha core restart"
echo "restart issued; wait ~40 s then check the log:"
echo "  ssh $HOST 'ha core logs | grep -i srne_inverter'"
