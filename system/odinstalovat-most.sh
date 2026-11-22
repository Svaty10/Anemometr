#!/usr/bin/env bash
# Odebere most pro anemometry z retenčky (retenčka se vrátí přesně do původního stavu).
#   sudo bash system/odinstalovat-most.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Spusťte přes sudo."; exit 1; }
rm -f /etc/systemd/system/retencka.service.d/vitr-most.conf
rmdir /etc/systemd/system/retencka.service.d 2>/dev/null || true
rm -rf /usr/local/lib/vitr-most
systemctl daemon-reload
systemctl restart retencka 2>/dev/null || true
echo "Most odebrán, retenčka běží bez něj."
