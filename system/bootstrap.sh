#!/bin/bash
# =============================================================================
# Vítr (anemometry) – instalace jedním příkazem na Raspberry Pi s retenčkou:
#
#   curl -fsSL https://raw.githubusercontent.com/Svaty10/Anemometr/main/system/bootstrap.sh | sudo bash
#
# Nainstaluje git (je-li potřeba), stáhne repozitář do /opt/vitr-src (nebo ho
# aktualizuje) a spustí system/install.sh. Nikdy nečte ze stdin.
# =============================================================================
set -euo pipefail

# Vše je ve funkci: bash při "curl | bash" čte skript ze stdin postupně, takže
# kód se musí celý načíst dřív, než se stdin odpojí.
main() {

REPO_URL="${VITR_REPO_URL:-https://github.com/Svaty10/Anemometr.git}"
BRANCH="${VITR_BRANCH:-main}"
DEST=/opt/vitr-src

[ "$(id -u)" -eq 0 ] || { echo "Spusťte jako root:  curl -fsSL <adresa> | sudo bash"; exit 1; }
command -v apt-get >/dev/null && command -v systemctl >/dev/null || {
    echo "Chybí apt-get nebo systemctl – podporován je jen Raspberry Pi OS."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null; then
    echo "==> Instaluji git"
    apt-get update -qq
    apt-get install -y -qq git ca-certificates >/dev/null
fi

if [ -d "$DEST/.git" ]; then
    echo "==> Aktualizuji $DEST"
    git -C "$DEST" fetch --quiet origin "$BRANCH"
    git -C "$DEST" checkout --quiet -B "$BRANCH" FETCH_HEAD
else
    echo "==> Stahuji repozitář do $DEST"
    rm -rf "$DEST"
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEST"
fi

exec bash "$DEST/system/install.sh" "$@"
}

main "$@" </dev/null
