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
Bez přihlášení web neukáže nic (ani data) – jen přihlašovací stránku. Po prvním přihlášení
heslo změňte v Nastavení → Heslo (min. 10 znaků); soubor `heslo.txt` se tím smaže a ostatní
přihlášená zařízení se odhlásí. Data v `/var/lib/vitr` při aktualizaci zůstávají. Port webu: `VITR_WEB_PORT=…` v
`/etc/default/vitr` a `sudo systemctl restart vitr`.

```bash
systemctl status vitr          # stav
journalctl -u vitr -f          # log
```

## 3. Spárování anemometru

1. Nahrát firmware do čidla: `uzel/2-novy-cip.cmd` (nový čip) nebo `uzel/3-aktualizace-firmwaru.cmd`
   – použije přeložený `uzel/build/firmware.hex`.
2. Web `http://retencka.local:8080` → přihlásit → **Nastavení** → **Čekat na nové čidlo**.
3. Vložit do anemometru baterii. Do minuty se objeví jako „Anemometr 1“; přejmenovat a
   nastavit kalibraci „1 otáčka za sekundu = X m/s“ podle popisu anemometru a počet pulzů na
   otáčku (výchozí 0,667 m/s = 2,4 km/h a 1 pulz).
4. Druhý anemometr stejně.

## 4. Home Assistant

Nastavení → **Home Assistant (MQTT)**: stejný broker, jméno a heslo jako u retenčky. Každý
anemometr se objeví jako zařízení s rychlostí, nárazem, průměrem 10 min, nejsilnějším
nárazem dne, baterií, teplotou čidla a signálem.

## 5. Přístup zvenku přes Cloudflare Tunnel

Web je celý za heslem, takže ho lze vystavit ven. Doporučený postup je `cloudflared`
**přímo na Pi** s cílem `http://localhost:8080` (Public Hostname → Service `HTTP`,
`localhost:8080`):

- Požadavky z `cloudflared` přicházejí z loopbacku, takže vitr věří hlavičkám
  `CF-Connecting-IP` a `X-Forwarded-Proto`: omezení pokusů o přihlášení počítá se skutečnou IP
  návštěvníka, cookie dostane příznak `Secure` a odpovědi hlavičku HSTS.
- Z jiných adres než loopback se těmto hlavičkám nevěří (nelze je podvrhnout z LAN).
- Po 3 chybných heslech z jedné IP se další pokus odkládá 2, 4, 8 … s (max. 1 h).
- Session: cookie `HttpOnly`, `SameSite=Strict`, platnost 7 dní nečinnosti / 30 dní celkem.
- Změny (POST) jen jako JSON a se stejným `Origin` – ochrana proti CSRF; přísné CSP,
  zákaz vložení do rámu.

Pro ještě vyšší bezpečnost lze v Cloudflare zapnout **Access** (přihlášení e-mailem před
samotnou stránkou). Retenčka i vitr pak mohou mít každá vlastní hostname
(např. `retencka.…` → `localhost:80`, `vitr.…` → `localhost:8080`).

Kdyby měl být web dostupný **jen** přes tunel (ne z LAN), nastavte v `/etc/default/vitr`
`VITR_BIND=127.0.0.1` a `sudo systemctl restart vitr`.
