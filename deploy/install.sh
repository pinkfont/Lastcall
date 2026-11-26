#!/usr/bin/env bash
# One-time server setup for Lastcall on Ubuntu (e.g. Oracle Cloud Always Free).
# Run from the repository folder:   bash deploy/install.sh
# Safe to run again; it skips steps that are already done.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/5 Docker"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh
fi

echo "== 2/5 Open ports 80 and 443 in the server's own firewall"
# Oracle's Ubuntu images block everything except SSH by default.
for port in 80 443; do
    if ! sudo iptables -C INPUT -m state --state NEW -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
    fi
done
if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save
fi

echo "== 3/5 Data folder"
mkdir -p data
sudo chown -R 10001:10001 data   # the app runs as user 10001 inside its container

echo "== 4/5 Settings"
if [ ! -f deploy/server.env ]; then
    cp deploy/server.env.example deploy/server.env
    chmod 600 deploy/server.env
    ip="$(curl -fsS https://ifconfig.me || true)"
    if [ -n "$ip" ]; then
        sed -i "s/^DOMAIN=.*/DOMAIN=${ip//./-}.sslip.io/" deploy/server.env
    fi
fi
missing=$(grep -E '^(FEED_URL|API_KEY|TELEGRAM_BOT_TOKEN|DOMAIN)=$' deploy/server.env || true)
if [ -n "$missing" ]; then
    echo
    echo "Fill these in deploy/server.env, then run this script again:"
    echo "$missing"
    echo "  (edit with:  nano deploy/server.env)"
    exit 0
fi

echo "== 5/5 Start Lastcall"
sudo docker compose -f deploy/compose.yaml up -d --build
domain=$(grep '^DOMAIN=' deploy/server.env | cut -d= -f2)
echo
echo "Done. In a minute, check:  https://${domain}/health   (should say {\"status\":\"ok\"})"
echo "Logs:  sudo docker compose -f deploy/compose.yaml logs -f lastcall"
