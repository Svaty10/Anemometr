#!/usr/bin/env bash
# =============================================================================
# Vítr (anemometry) – instalace / aktualizace na Raspberry Pi s retenčkou.
#
# Nejjednodušeji jedním příkazem (stáhne repozitář a spustí tento skript):
#   curl -fsSL https://raw.githubusercontent.com/Svaty10/Anemometr/main/system/bootstrap.sh | sudo bash
# Nebo ručně ze složky s klonem repozitáře:
#   sudo bash system/install.sh
#
# Opakované spuštění = aktualizace; data (/var/lib/vitr) zůstávají. Další verze si pak
# vitr stahuje sám z GitHub Releases (podepsané, viz docs/vydani.md) – jako retenčka.
# Retenčka se nemění: dostane jen doplněk služby s mostem pro anemometry.
# Nikdy nečte ze stdin (funguje i přes "curl | sudo bash").
# =============================================================================
set -euo pipefail
exec </dev/null

[ "$(id -u)" -eq 0 ] || { echo "Spusťte přes sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/server/vitr/__main__.py" ] || { echo "Chybí $SRC/server/vitr – spouštějte ze složky Anemometr."; exit 1; }
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/server/vitr/__init__.py")
APP=/opt/vitr
DATA=/var/lib/vitr
step() { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33mPOZOR:\033[0m $*"; }

step "Uživatel a složky"
id vitr >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin vitr
install -d -m 755 -o root -g root "$APP" "$APP/releases"
install -d -m 750 -o vitr -g vitr "$DATA"

step "Aplikace verze $VER"
# nová verze se připraví vedle staré, teprve pak se přepne odkaz "current"
NAME="$VER"
[ -e "$APP/releases/$NAME" ] && NAME="$VER+$(date +%s)"          # stejná verze se instaluje znovu
STAGE="$APP/releases/.stage-$$"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -a "$SRC/server" "$SRC/system" "$STAGE/"
[ -f "$SRC/CHANGELOG.md" ] && cp "$SRC/CHANGELOG.md" "$STAGE/"
rm -rf "$STAGE/server/tests"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +
python3 -m compileall -q "$STAGE/server" >/dev/null 2>&1 || true
chown -R root:root "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"
chmod 755 "$STAGE"/system/bin/* "$STAGE"/system/*.sh 2>/dev/null || true
mv "$STAGE" "$APP/releases/$NAME"
PREV=$(readlink -f "$APP/current" 2>/dev/null || true)
ln -sfn "releases/$NAME" "$APP/current.new"
mv -T "$APP/current.new" "$APP/current"
CUR=$(readlink -f "$APP/current")
# starší kopie téže verze už nejsou potřeba; jiné verze zůstávají pro návrat (3 nejnovější)
for d in "$APP"/releases/"$VER"+* "$APP/releases/$VER"; do
    [ -d "$d" ] && [ "$(readlink -f "$d")" != "$CUR" ] && [ "$(readlink -f "$d")" != "$PREV" ] && rm -rf "$d"
done
ls -1d "$APP"/releases/*/ 2>/dev/null | sort -V | head -n -3 | while read -r d; do
    [ "$(readlink -f "$d")" != "$CUR" ] && [ "$(readlink -f "$d")" != "$PREV" ] && rm -rf "$d"
done
rm -rf "$APP/server" "$APP/server.old"                            # rozložení verze 1.0
rm -rf /usr/local/lib/vitr-most                                   # most z doby před vydáním 1.1

step "Klíč pro ověření aktualizací"
if [ -s "$SRC/system/keys/release.pub" ]; then
    install -d -m 755 /etc/vitr
    install -m 644 "$SRC/system/keys/release.pub" /etc/vitr/release.pub
    echo "Aktualizace se ověřují klíčem /etc/vitr/release.pub."
else
    warn "system/keys/release.pub chybí – automatické aktualizace budou odmítnuty (docs/vydani.md, oddíl 0)."
fi

step "Služby"
if [ ! -f /etc/default/vitr ]; then
    cat > /etc/default/vitr <<'EOF'
# Služba vitr (anemometry). Po změně: sudo systemctl restart vitr
#VITR_WEB_PORT=8080
#VITR_BIND=0.0.0.0
#VITR_UDP_PORT=47613
# Aktualizace (výchozí hodnoty; na webu: Nastavení → Systém)
#VITR_AUTO_UPDATE=1
#VITR_CHANNEL=stable
EOF
elif ! grep -q "VITR_AUTO_UPDATE" /etc/default/vitr; then
    printf '\n# Aktualizace (výchozí hodnoty; na webu: Nastavení → Systém)\n#VITR_AUTO_UPDATE=1\n#VITR_CHANNEL=stable\n' >> /etc/default/vitr
fi
for u in "$SRC"/system/systemd/vitr*; do
    install -m 644 "$u" "/etc/systemd/system/$(basename "$u")"
done
systemctl daemon-reload
systemctl enable vitr vitr-update.path vitr-update.timer >/dev/null 2>&1
systemctl start vitr-update.path vitr-update.timer
SINCE=$(date +%s)
systemctl restart vitr
OK=""
for _ in $(seq 1 20); do
    sleep 2
    H=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("ok" if d.get("version")==sys.argv[2] and d.get("ts",0)>=int(sys.argv[3])-2 else "")' \
        "$DATA/health.json" "$VER" "$SINCE" 2>/dev/null || true)
    [ "$H" = "ok" ] && { OK=1; break; }
done
if [ -n "$OK" ]; then
    echo "vitr $VER běží."
else
    echo "vitr $VER nenaběhl:"; journalctl -u vitr -n 30 --no-pager
    if [ -n "$PREV" ] && [ -d "$PREV" ] && [ "$PREV" != "$CUR" ]; then
        ln -sfn "$PREV" "$APP/current.new" && mv -T "$APP/current.new" "$APP/current"
        systemctl restart vitr
        echo "Vráceno na předchozí verzi ($(basename "$PREV"))."
    fi
    exit 1
fi

step "Most k rádiu retenčky"
# Retenčka se nemění: jen doplněk služby (drop-in), který při jejím startu připojí most
# pro anemometry z aktuální verze vitru. Přežije aktualizace retenčky;
# odinstalace: sudo bash /opt/vitr/current/system/odinstalovat-most.sh
if systemctl cat retencka.service >/dev/null 2>&1; then
    DROP=/etc/systemd/system/retencka.service.d/vitr-most.conf
    install -d -m 755 /etc/systemd/system/retencka.service.d
    MOST_CHANGED=""
    for f in vitr_most.py sitecustomize.py; do
        cmp -s "$SRC/system/retencka-most/$f" "$PREV/system/retencka-most/$f" 2>/dev/null || MOST_CHANGED=1
    done
    if ! cmp -s "$SRC/system/retencka-most/vitr-most.conf" "$DROP" || [ -n "$MOST_CHANGED" ]; then
        install -m 644 "$SRC/system/retencka-most/vitr-most.conf" "$DROP"
        systemctl daemon-reload
        echo "Restartuji retenčku, aby se načetl most…"
        systemctl restart retencka
        sleep 8
    fi
    if journalctl -u retencka -b --no-pager 2>/dev/null | grep "vitr-most:" | tail -n1 | grep -q "anemometer frames"; then
        echo "Most běží: pakety anemometrů z rádia retenčky jdou do vitru."
    else
        warn "most se v retenčce zatím nepřipojil. Zkontrolujte:  journalctl -u retencka -b | grep vitr-most"
    fi
else
    warn "retenčka (retencka.service) tu není nainstalovaná – anemometry nemají rádio."
fi

PORT=$(sed -n 's/^VITR_WEB_PORT=\([0-9]*\)/\1/p' /etc/default/vitr); PORT=${PORT:-8080}
echo
echo "Web: http://$(hostname).local:$PORT"
IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] && echo "     http://$IP:$PORT"
USERS=$(python3 -c "import sqlite3; print(sqlite3.connect('file:$DATA/vitr.db?mode=ro', uri=True).execute('SELECT COUNT(*) FROM users').fetchone()[0])" 2>/dev/null || echo 0)
if [ "$USERS" = "0" ] && [ -f "$DATA/setup_code" ]; then
    echo
    echo "První přihlášení: jméno  admin  a kód  $(cat "$DATA/setup_code")"
    echo "Pak si vytvoříte vlastní účet administrátora a výchozí přístup se zruší."
    echo "(Kód kdykoli znovu: sudo cat $DATA/setup_code)"
fi
echo
echo "Další verze se instalují samy (Nastavení → Systém → Aktualizace)."
