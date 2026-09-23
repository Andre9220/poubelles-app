#!/usr/bin/env bash
# Relance Mission Poubelles et collecte le diagnostic dont j'ai besoin.
# Usage : sudo bash ~/poubelles-app/deploy/relance.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo : sudo bash $0" >&2; exit 1; }
cd /home/umbrel/poubelles-app

echo "===== AVANT ====="
docker ps -a --filter name=poubelles --format '  {{.Names}} | {{.Status}} | {{.Image}}' || true
echo "  politique de redémarrage : $(docker inspect poubelles \
  --format '{{.HostConfig.RestartPolicy.Name}} | redemarrages={{.RestartCount}} | OOM={{.State.OOMKilled}} | exit={{.State.ExitCode}}' 2>/dev/null || echo 'conteneur absent')"
echo "  --- 30 dernières lignes de log (cause du plantage) ---"
docker logs --tail 30 poubelles 2>&1 | sed 's/^/    /' || echo "    (aucun log)"

echo
echo "===== RELANCE ====="
docker compose up -d --remove-orphans
sleep 8

echo
echo "===== APRES ====="
docker ps --filter name=poubelles --format '  {{.Names}} | {{.Status}} | {{.Ports}}'
printf '  santé locale  : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://127.0.0.1:8501/_stcore/health
printf '  lien public   : '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 https://umbrel.tail2fcf06.ts.net/_stcore/health

echo
echo "===== ACCES (temporaire, /etc est effacé à chaque boot) ====="
usermod -aG docker umbrel && echo "  umbrel remis dans le groupe docker"
