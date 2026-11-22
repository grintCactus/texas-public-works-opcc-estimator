#!/usr/bin/env bash
# Set up -- or update -- the public estimator on an Oracle Cloud VM
# (Ubuntu 22.04 / 24.04 or Oracle Linux 8 / 9, arm64 or x86_64).
#
# On the Mac, send the package (the data bundle is inside space/):
#   rsync -az --delete --exclude .env -e "ssh -i ~/.ssh/oci_bid_estimator" \
#       ~/priceprediction/deploy/space ~/priceprediction/deploy/oracle \
#       <user>@<PUBLIC_IP>:opcc/
# (--exclude .env keeps the VM's .env -- site name and access code -- from
# being deleted by --delete)
# On the VM (<user> is ubuntu on Ubuntu images, opc on Oracle Linux):
#   cd ~/opcc/oracle && sudo ./setup.sh <PUBLIC_IP>
#
# What it does, each step skipped when already done (safe to run again; a
# second run after a new rsync rebuilds and restarts the app):
#   1. installs Docker Engine and the compose plugin (Docker's own
#      repository) and enables them at boot;
#   2. opens TCP 80/443 (and UDP 443) in the VM's firewall: iptables on
#      Ubuntu (Oracle's images reject everything but SSH), firewalld on
#      Oracle Linux. The OCI security list is separate: see DEPLOY.md;
#   3. writes .env: SITE_HOST=<ip-with-dashes>.sslip.io (from the argument,
#      the existing .env, or the VM's public IP), keeps DEMO_ACCESS_CODE
#      (an exported DEMO_ACCESS_CODE replaces it);
#   4. builds and starts app + caddy (restart: unless-stopped, so both come
#      back after a reboot) and waits for the health check and HTTPS.
set -euo pipefail

ARG="${1:-}"          # the public IP or a host name (optional)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
log() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run it with sudo: sudo ./setup.sh <PUBLIC_IP>"
[ -f ../space/Dockerfile ] || die "../space/Dockerfile is missing -- rsync deploy/space and deploy/oracle side by side"
. /etc/os-release
FAMILY=""
case "${ID:-} ${ID_LIKE:-}" in
  *ubuntu*|*debian*) FAMILY=debian ;;
  *ol*|*rhel*|*fedora*|*centos*) FAMILY=rhel ;;
  *) die "unsupported OS: ${PRETTY_NAME:-unknown} (Ubuntu or Oracle Linux expected)" ;;
esac
log "OS: ${PRETTY_NAME:-?}, $(uname -m)"

# ---------------------------------------------------------------- 1. Docker
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker already installed: $(docker --version)"
elif [ "$FAMILY" = debian ]; then
  log "Installing Docker (Ubuntu)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "Installing Docker (Oracle Linux)"
  dnf install -y dnf-plugins-core
  dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
  # Oracle Linux ships podman/buildah, whose runc conflicts: let dnf replace them
  dnf install -y --allowerasing docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now containerd docker
if [ -n "${SUDO_USER:-}" ] && ! id -nG "$SUDO_USER" | grep -qw docker; then
  usermod -aG docker "$SUDO_USER"   # 'docker' without sudo from the next login
fi

# ------------------------------------------------------- 2. VM firewall 80/443
if [ "$FAMILY" = debian ]; then
  log "Opening 80/443 in iptables"
  command -v iptables >/dev/null || apt-get install -y iptables
  for rule in "tcp 80" "tcp 443" "udp 443"; do
    set -- $rule
    if iptables -C INPUT -p "$1" -m state --state NEW -m "$1" --dport "$2" -j ACCEPT 2>/dev/null; then
      continue
    fi
    # insert ahead of Oracle's catch-all REJECT, else append
    pos="$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')"
    if [ -n "$pos" ]; then
      iptables -I INPUT "$pos" -p "$1" -m state --state NEW -m "$1" --dport "$2" -j ACCEPT
    else
      iptables -A INPUT -p "$1" -m state --state NEW -m "$1" --dport "$2" -j ACCEPT
    fi
  done
  # persist in Oracle's rules file (not with iptables-save, which would also
  # save Docker's own chains)
  RULES=/etc/iptables/rules.v4
  if [ -f "$RULES" ]; then
    for rule in "tcp 80" "tcp 443" "udp 443"; do
      set -- $rule
      line="-A INPUT -p $1 -m state --state NEW -m $1 --dport $2 -j ACCEPT"
      if ! grep -qxF -- "$line" "$RULES"; then
        if grep -q -- "--dport 22 -j ACCEPT" "$RULES"; then
          sed -i "/--dport 22 -j ACCEPT/a $line" "$RULES"
        else
          echo "WARNING: $RULES has no SSH rule to anchor on; add '$line' before the REJECT line by hand"
        fi
      fi
    done
  fi
else
  log "Opening 80/443 in firewalld"
  systemctl enable --now firewalld
  firewall-cmd --permanent --add-service=http --add-service=https >/dev/null
  firewall-cmd --permanent --add-port=443/udp >/dev/null
  firewall-cmd --reload >/dev/null
fi

# ------------------------------------------------------------------ 3. .env
ip_to_host() { echo "${1//./-}.sslip.io"; }
OLD_HOST=""; OLD_CODE=""
if [ -f .env ]; then
  OLD_HOST="$(sed -n 's/^SITE_HOST=//p' .env | tail -n 1)"
  OLD_CODE="$(sed -n 's/^DEMO_ACCESS_CODE=//p' .env | tail -n 1)"
fi
if [[ "$ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  HOST="$(ip_to_host "$ARG")"
elif [ -n "$ARG" ]; then
  HOST="$ARG"
elif [ -n "$OLD_HOST" ]; then
  HOST="$OLD_HOST"
else
  PUB="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]' || true)"
  [[ "$PUB" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not find the public IP; run: sudo ./setup.sh <PUBLIC_IP>"
  HOST="$(ip_to_host "$PUB")"
fi
CODE="${DEMO_ACCESS_CODE-$OLD_CODE}"
umask 077
printf 'SITE_HOST=%s\nDEMO_ACCESS_CODE=%s\n' "$HOST" "$CODE" > .env
log "Site: https://$HOST  (access code: $([ -n "$CODE" ] && echo set || echo none))"

# --------------------------------------------------------------- 4. services
log "Building and starting (the first build takes a few minutes)"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null || true

log "Waiting for the estimator to load its tables"
ok=""
for _ in $(seq 1 90); do
  if docker compose exec -T app python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4)); raise SystemExit(0 if d.get('ready') else 1)" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 4
done
[ -n "$ok" ] || { docker compose logs --tail 60 app; die "the app did not become ready"; }
echo "app ready"

log "Checking HTTPS (the first certificate can take a minute)"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 10 "https://$HOST/api/health" >/dev/null 2>&1; then
    echo "OK: https://$HOST is up"
    docker compose ps
    exit 0
  fi
  sleep 5
done
docker compose logs --tail 40 caddy
echo
echo "The app runs, but https://$HOST did not answer yet. Check that the OCI"
echo "security list allows TCP 80 and 443 from 0.0.0.0/0 (DEPLOY.md), then"
echo "run this script again."
exit 2
