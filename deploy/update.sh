#!/usr/bin/env bash
# Pull the latest code from GitHub and restart Lastcall. Run on the server: bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull
sudo docker compose -f deploy/compose.yaml up -d --build
sudo docker image prune -f >/dev/null
echo "Updated."
