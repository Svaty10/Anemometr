#!/usr/bin/env bash
# Instalace / aktualizace služby "vitr" (anemometry) na Raspberry Pi s retenčkou.
#   sudo ./system/install.sh            (spouštět ze složky Anemometr na Pi)
# Opakované spuštění = aktualizace; data (/var/lib/vitr) zůstávají.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Spusťte přes sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/server/vitr/__main__.py" ] || { echo "Chybí $SRC/server/vitr – spouštějte ze složky Anemometr."; exit 1; }
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/server/vitr/__init__.py")
step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

step "Uživatel a složky"
id vitr >/dev/null 2>&1 || useradd --system --home-dir /var/lib/vitr --no-create-home --shell /usr/sbin/nologin vitr
install -d -m 755 -o root -g root /opt/vitr
install -d -m 750 -o vitr -g vitr /var/lib/vitr

step "Aplikace verze $VER"
rm -rf /opt/vitr/server.new
cp -a "$SRC/server" /opt/vitr/server.new
rm -rf /opt/vitr/server.new/tests
find /opt/vitr/server.new -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root /opt/vitr/server.new
chmod -R u=rwX,go=rX /opt/vitr/server.new
rm -rf /opt/vitr/server.old
[ -d /opt/vitr/server ] && mv /opt/vitr/server /opt/vitr/server.old
mv /opt/vitr/server.new /opt/vitr/server

step "Služba"
if [ ! -f /etc/default/vitr ]; then
    cat > /etc/default/vitr <<'EOF'
# Služba vitr (anemometry). Po změně: sudo systemctl restart vitr
#VITR_WEB_PORT=8080
#VITR_UDP_PORT=47613
EOF
fi
install -m 644 "$SRC/system/vitr.service" /etc/systemd/system/vitr.service
systemctl daemon-reload
systemctl enable vitr >/dev/null 2>&1
systemctl restart vitr
sleep 3
systemctl is-active --quiet vitr && echo "vitr běží." || { echo "vitr neběží:"; journalctl -u vitr -n 30 --no-pager; exit 1; }

step "Kontrola rádia retenčky"
if [ -f /var/lib/retencka/app/current/server/retencka/bridge.py ]; then
    echo "Retenčka má most pro anemometry – pakety se budou předávat sem."
else
    echo -e "\033[1;33mPOZOR:\033[0m nainstalovaná retenčka ještě nemá most pro anemometry (bridge.py)."
    echo "Nainstalujte retenčku z větve anemometr-most (viz docs/instalace.md), jinak sem žádná data nepřijdou."
fi

PORT=$(sed -n 's/^VITR_WEB_PORT=\([0-9]*\)/\1/p' /etc/default/vitr); PORT=${PORT:-8080}
echo
echo "Web: http://$(hostname).local:$PORT"
IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] && echo "     http://$IP:$PORT"
USERS=$(python3 -c "import sqlite3; print(sqlite3.connect('file:/var/lib/vitr/vitr.db?mode=ro', uri=True).execute('SELECT COUNT(*) FROM users').fetchone()[0])" 2>/dev/null || echo 0)
if [ "$USERS" = "0" ] && [ -f /var/lib/vitr/setup_code ]; then
    echo
    echo "První přihlášení: jméno  admin  a kód  $(cat /var/lib/vitr/setup_code)"
    echo "Pak si vytvoříte vlastní účet administrátora a výchozí přístup se zruší."
    echo "(Kód kdykoli znovu: sudo cat /var/lib/vitr/setup_code)"
fi
