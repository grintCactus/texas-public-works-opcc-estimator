#!/usr/bin/env bash
# Send the assembled package to the server and restart the site.
#
#     DEPLOY_HOST=<public IP> DEPLOY_KEY=<path to the SSH private key> \
#         bash deploy/redeploy.sh
#
#   DEPLOY_HOST   the VM's public IP (the site is https://<IP with dashes>.sslip.io)
#   DEPLOY_KEY    the SSH private key allowed on the VM (kept by the owner;
#                 never in this repository)
#   DEPLOY_USER   ubuntu on Ubuntu images (the default), opc on Oracle Linux
#
# What it sends: deploy/space (the Docker build context with the public data
# bundle inside, about 350 MB, compressed in transit) and deploy/oracle
# (compose file, Caddy file, setup.sh). --exclude .env keeps the server's own
# .env (site name, optional access code). Then it runs setup.sh on the VM,
# which rebuilds the image, restarts the two containers and waits for HTTPS,
# and finally asks the live site for its health.
#
# Before this: deploy/tools/assemble_space.py and deploy/tools/check_package.sh.
set -euo pipefail
: "${DEPLOY_HOST:?set DEPLOY_HOST to the public IP of the server}"
: "${DEPLOY_KEY:?set DEPLOY_KEY to the SSH private key file}"
U="${DEPLOY_USER:-ubuntu}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/deploy/space/Dockerfile" ] || { echo "STOP: deploy/space is missing -- run deploy/tools/assemble_space.py"; exit 2; }
SSH="ssh -i $DEPLOY_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

echo "start $(date '+%H:%M:%S')"
rsync -az --delete --exclude .env -e "$SSH" "$ROOT/deploy/space" "$ROOT/deploy/oracle" "$U@$DEPLOY_HOST:opcc/"
echo "rsync done $(date '+%H:%M:%S')"
$SSH "$U@$DEPLOY_HOST" "cd ~/opcc/oracle && sudo ./setup.sh $DEPLOY_HOST" 2>&1 | tail -25
echo "setup done $(date '+%H:%M:%S')"
SITE="https://$(echo "$DEPLOY_HOST" | tr . -).sslip.io"
curl -fsS --max-time 30 "$SITE/api/health" && echo && echo "live: $SITE"
