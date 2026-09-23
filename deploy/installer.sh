#!/usr/bin/env bash
# Installe Mission Poubelles comme service système, pour qu'elle redémarre
# toute seule après un redémarrage de la machine ou un plantage.
# Usage : sudo bash ~/poubelles-app/deploy/installer.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "À lancer avec sudo : sudo bash $0" >&2
  exit 1
fi

BASE="/home/umbrel/poubelles-app"
cd "$BASE"

echo "==> Installation des unités systemd"
install -m 644 "$BASE/deploy/poubelles.service"          /etc/systemd/system/
install -m 644 "$BASE/deploy/poubelles-watchdog.service" /etc/systemd/system/
install -m 644 "$BASE/deploy/poubelles-watchdog.timer"   /etc/systemd/system/
systemctl daemon-reload

echo "==> Activation au démarrage"
systemctl enable --now poubelles.service
systemctl enable --now poubelles-watchdog.timer

echo "==> Remise de umbrel dans le groupe docker"
usermod -aG docker umbrel || true

echo "==> État"
sleep 3
docker compose ps
systemctl --no-pager --lines=0 status poubelles.service || true
systemctl list-timers --no-pager poubelles-watchdog.timer || true

echo
echo "Terminé. L'app redémarrera automatiquement au boot,"
echo "et sera vérifiée toutes les 5 minutes."
