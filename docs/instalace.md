# Instalace na Raspberry Pi s retenčkou

Anemometry využívají **rádio retenčky**: CC1101 ovládá jen služba `retencka`, pakety
anemometrů (typy 0x13–0x16) předává mostem přes `127.0.0.1:47613` službě `vitr`.
`vitr` je samostatná aplikace: vlastní databáze, web na portu **8080**, vlastní MQTT
zařízení v Home Assistantu. Automaticky se neaktualizuje.

## 1. Retenčka s mostem (jednou)

Most je ve větvi **`anemometr-most`** repozitáře retenčky. Na Pi (ve složce s klonem
repozitáře retenčky):

```bash
git fetch origin anemometr-most
git checkout anemometr-most
sudo ./system/install.sh
```

Verze retenčky zůstává stejná, takže noční automatická aktualizace most nepřepíše, dokud
nevyjde **novější** verze retenčky. **Před dalším vydáním retenčky sloučit větev
`anemometr-most` do `main`**, jinak se most s novou verzí ztratí. Pro nádrže most nic nemění
(bez služby vitr cizí pakety jen zahodí). Vypnutí: `RETENCKA_BRIDGE_PORT=0` v `/etc/default/retencka`.

## 2. Služba vitr

Na Pi stáhnout repozitář a nainstalovat:

```bash
git clone https://github.com/Svaty10/Anemometr.git ~/Anemometr && cd ~/Anemometr && sudo bash system/install.sh
```

Aktualizace později:

```bash
cd ~/Anemometr && git pull && sudo bash system/install.sh
```

Skript založí uživatele `vitr`, nainstaluje aplikaci do `/opt/vitr`, službu `vitr.service`,
spustí ji a vypíše adresu webu a **heslo správce** (`sudo cat /var/lib/vitr/heslo.txt`).
Data v `/var/lib/vitr` při aktualizaci zůstávají. Port webu: `VITR_WEB_PORT=…` v
`/etc/default/vitr` a `sudo systemctl restart vitr`.

```bash
systemctl status vitr          # stav
journalctl -u vitr -f          # log
```

## 3. Spárování anemometru

1. Nahrát firmware do čidla: `uzel/2-novy-cip.cmd` (nový čip) nebo `uzel/3-aktualizace-firmwaru.cmd`
   – použije přeložený `uzel/build/firmware.hex`.
2. Web `http://retencka.local:8080` → **Nastavení** → přihlásit → **Čekat na nové čidlo**.
3. Vložit do anemometru baterii. Do minuty se objeví jako „Anemometr 1“; přejmenovat a
   nastavit kalibraci „1 otáčka za sekundu = X m/s“ podle popisu anemometru a počet pulzů na
   otáčku (výchozí 0,667 m/s = 2,4 km/h a 1 pulz).
4. Druhý anemometr stejně.

## 4. Home Assistant

Nastavení → **Home Assistant (MQTT)**: stejný broker, jméno a heslo jako u retenčky. Každý
anemometr se objeví jako zařízení s rychlostí, nárazem, průměrem 10 min, nejsilnějším
nárazem dne, baterií, teplotou čidla a signálem.
