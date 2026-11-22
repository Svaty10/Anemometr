# Instalace na Raspberry Pi s retenčkou

Anemometry využívají **rádio retenčky**: CC1101 ovládá jen služba `retencka`, pakety
anemometrů (typy 0x13–0x16) předává mostem přes `127.0.0.1:47613` službě `vitr`.
`vitr` je samostatná aplikace: vlastní databáze, web na portu **8080**, vlastní MQTT
zařízení v Home Assistantu. Retenčka se kvůli tomu nijak nemění (viz oddíl 1).

## 1. Most k rádiu retenčky (dělá instalace vitru sama)

Retenčka se kvůli anemometrům **nijak nemění** – ani její repozitář, ani nainstalované soubory,
a dál se normálně aktualizuje. Instalace vitru jí přidá jen doplněk služby
(`/etc/systemd/system/retencka.service.d/vitr-most.conf`), který při startu retenčky načte most
z nainstalované verze vitru (`/opt/vitr/current/system/retencka-most/`). Most pakety anemometrů (typy 0x13–0x16) pošle přes
`127.0.0.1:47613` vitru a odvysílá jeho odpověď na párování; pakety nádrží jdou beze změny dál.
Doplněk přežije aktualizace retenčky. Kdyby se rádio v nové verzi retenčky změnilo tak, že most
nesedí, most se nepřipojí a retenčka běží jako bez něj (v logu `… bridge NOT active`).

Jak poznat, že most běží:

```bash
journalctl -u retencka -b | grep vitr-most
```

→ `vitr-most: anemometer frames (0x13-0x16) go to vitr on 127.0.0.1:47613`. Na webu vitru
Nastavení → Diagnostika → *Rádio (přes retenčku): data chodí* (jakmile vysílá aspoň jeden
anemometr).

Odebrání mostu (retenčka bude přesně jako předtím): `sudo bash /opt/vitr/current/system/odinstalovat-most.sh`.

## 2. Služba vitr

Na Pi jedním příkazem (stejně jako retenčka):

```bash
curl -fsSL https://raw.githubusercontent.com/Svaty10/Anemometr/main/system/bootstrap.sh | sudo bash
```

Skript stáhne repozitář do `/opt/vitr-src`, založí uživatele `vitr`, nainstaluje aplikaci do
`/opt/vitr/releases/<verze>` (odkaz `/opt/vitr/current`), služby `vitr.service` a
`vitr-update.path/.timer`, připojí most k retenčce, ověří, že nová verze běží (jinak vrátí
předchozí), a vypíše adresu webu a **kód pro první přihlášení**. Data v `/var/lib/vitr` zůstávají.

### Aktualizace (stejně jako u retenčky)

Další verze si vitr stahuje sám z **GitHub Releases** – podepsané balíky, ověřené klíčem
`/etc/vitr/release.pub`. Na webu **Nastavení → Systém**:

- *Zkontrolovat aktualizace* → dialog s poznámkami k vydání → *Aktualizovat na X* (průběh:
  stahuji → ověřuji podpis → instaluji → restartuji); u **Nastavení** svítí tečka, když je nová verze;
- *Automatické aktualizace* (v noci kolem 4:00) a kanál *Stabilní / Beta*;
- když nová verze nenaběhne, vitr se sám vrátí na předchozí i s databází a verzi si zablokuje.

Výsledky jsou v Historii událostí (kategorie Systém), podrobnosti `journalctl -u vitr-update`.
Ruční přeinstalace (např. vývojová verze z `main`): znovu příkaz `curl … | sudo bash` výše.
Postup vydání nové verze: [vydani.md](vydani.md).

### Uživatelé (stejně jako u retenčky)

Bez přihlášení web neukáže nic (ani data) – jen přihlašovací stránku.

1. **První přihlášení:** jméno `admin` a kód z instalace (kdykoli znovu:
   `sudo cat /var/lib/vitr/setup_code`).
2. Hned si vytvoříte **vlastní účet administrátora** (jméno + heslo, min. 10 znaků). Výchozí
   přístup `admin` + kód tím přestane platit.
3. Další lidi přidáte v menu účtu → **Správa uživatelů**:
   - **Administrátor** – nastavení, párování čidel, kalibrace, Home Assistant, uživatelé,
     historie událostí;
   - **Uživatel (jen čtení)** – přehled a grafy, vlastní heslo.
4. Vlastní heslo: menu účtu → **Můj účet** (ostatní zařízení se odhlásí). Administrátor může
   jinému uživateli nastavit nové heslo (ten se odhlásí všude), změnit roli nebo ho smazat;
   poslední administrátor zůstat musí.
5. **Historie událostí** (jen administrátor): přihlášení i neúspěšná, změny uživatelů,
   párování a odebrání čidel, kalibrace, nastavení.

Účty jsou vlastní pro vitr (vlastní databáze), nesdílí se s retenčkou. Port webu: `VITR_WEB_PORT=…` v
`/etc/default/vitr` a `sudo systemctl restart vitr`.

```bash
systemctl status vitr          # stav
journalctl -u vitr -f          # log
```

## 3. Spárování anemometru

1. Nahrát firmware do čidla: `uzel/2-novy-cip.cmd` (nový čip) nebo `uzel/3-aktualizace-firmwaru.cmd`
   – použije přeložený `uzel/build/firmware.hex`.
2. Web `http://retencka.local:8080` → přihlásit (administrátor) → **Nastavení** → **Čidla** →
   **Přidat čidlo…** (stejně jako u retenčky).
3. Vložit do anemometru baterii. V dialogu se objeví v tabulce *Nová čidla v dosahu* – kliknout
   **Spárovat**, ověřit ID podle štítku a zadat název (ID se dá zadat i předem). Kroky v dialogu
   ukazují postup: vložte baterii → potvrďte ID → první data → hotovo. Čidlo se nikdy nespáruje
   samo, jen po potvrzení jeho ID.
4. U čidla (Nastavení → karta anemometru) nastavit kalibraci „1 otáčka za sekundu = X m/s“ podle
   popisu anemometru a počet pulzů na otáčku (výchozí 0,667 m/s = 2,4 km/h a 1 pulz).
5. **Odpárování:** v tabulce čidel **Odpárovat** a pak do anemometru vyjmout a vložit baterii –
   dokončí se, až to čidlo potvrdí. Ztracené čidlo: **Odebrat bez čidla**; hub si jeho klíč
   uschová, a když se najde, stačí u něj v *Čidla v dosahu* (pokročilá nastavení) kliknout
   **Spárovat znovu**. Naměřená data zůstávají.
6. Druhý anemometr stejně.
7. V **Nastavení → název anemometru** zvolte **typ baterie** (lithiové / alkalické), podle něj se
   z napětí počítají procenta. Když údaj nesedí s voltmetrem, v pokročilém nastavení
   **Kalibrace měření baterie** zadejte změřené napětí – hub spočítá korekci (jako u retenčky).

## 4. Home Assistant

Nastavení → **Home Assistant (MQTT)**: stejný broker, jméno a heslo jako u retenčky. Každý
anemometr se objeví jako zařízení s rychlostí, nárazem, průměrem 10 min, nejsilnějším
nárazem dne, baterií a signálem (teplotu anemometr neměří).

## 5. Přístup zvenku přes Cloudflare Tunnel

Web je celý za přihlášením, takže ho lze vystavit ven. Doporučený postup je `cloudflared`
**přímo na Pi** s cílem `http://localhost:8080` (Public Hostname → Service `HTTP`,
`localhost:8080`):

- Požadavky z `cloudflared` přicházejí z loopbacku, takže vitr věří hlavičkám
  `CF-Connecting-IP` a `X-Forwarded-Proto`: omezení pokusů o přihlášení počítá se skutečnou IP
  návštěvníka, cookie dostane příznak `Secure` a odpovědi hlavičku HSTS.
- Z jiných adres než loopback se těmto hlavičkám nevěří (nelze je podvrhnout z LAN).
- Po 3 chybných heslech z jedné IP se další pokus odkládá 2, 4, 8 … s (max. 1 h); po 5
  chybách na jedno jméno z jedné IP je jméno z té IP 15 min zamčené (skutečného majitele
  z jiné adresy to neblokuje).
- Relace na serveru (uložený jen otisk SHA-256), cookie `HttpOnly`, `SameSite=Strict`,
  platnost 7 dní nečinnosti / 30 dní celkem.
- Změny (POST) jen jako JSON, s CSRF tokenem relace a se stejným `Origin`; přísné CSP,
  zákaz vložení do rámu.

Pro ještě vyšší bezpečnost lze v Cloudflare zapnout **Access** (přihlášení e-mailem před
samotnou stránkou). Retenčka i vitr pak mohou mít každá vlastní hostname
(např. `retencka.…` → `localhost:80`, `vitr.…` → `localhost:8080`).

Kdyby měl být web dostupný **jen** přes tunel (ne z LAN), nastavte v `/etc/default/vitr`
`VITR_BIND=127.0.0.1` a `sudo systemctl restart vitr`.
