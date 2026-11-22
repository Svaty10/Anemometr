# Diagnostika čidla přes programátor

Když se čidlo neobjevuje na hubu, diagnostický firmware ukáže, co se v čidle děje: jestli
rádio CC1101 odpovídá, jestli vysílá a jestli slyší ostatní čidla v dosahu. Všechno se čte
**jen přes UPDI programátor**, na webu ani v retenčce se nic nemění. Funguje i s úplně
mrtvým rádiem.

## Postup

1. Programátor zapojte stejně jako při nahrávání firmwaru ([programovani.md](programovani.md)).
2. Dvojklik na **`uzel/5-diagnostika.cmd`** – nahraje diagnostický firmware.
   Párovací klíč v čidle zůstává (EEPROM se při nahrávání nemaže).
3. Nechte čidlo běžet **aspoň 5 minut** (napájené baterií, nebo 3,3 V z programátoru).
   Programátor může zůstat zapojený.
4. Dvojklik na **`uzel/6-precist-diagnostiku.cmd`** – vyčte výsledek a vypíše, co je v pořádku
   a co zkontrolovat. Vyčtení čidlo restartuje a test začne znovu.
5. Zpět na normální firmware: **`uzel/3-aktualizace-firmwaru.cmd`** (párování zůstane).

## Co se testuje (každých ~6 s)

| Test | Co znamená NE |
|---|---|
| Rádio odpovídá (SO, krystal) | CC1101 nemá napájení, nebo vadný spoj CSn (PA4) / SO-MISO (PA2), nekmitá krystal 26 MHz |
| PARTNUM / VERSION | má být 0x00 / 0x14; jiné číslo = čip není CC1101 nebo SPI čte nesmysly |
| Zápis a čtení registru | vadný spoj SI-MOSI (PA1) nebo SCLK (PA3) |
| PLL zamčená | krystal 26 MHz a jeho kondenzátory, napájení rádia |
| Vysílání dokončeno | rádio nedokončilo vysílání (napájení při zátěži ~30 mA, PLL) |
| GDO0 při vysílání | přerušený spoj GDO0 (PB2) – čidlo pak nepozná odpověď hubu, **párování nefunguje** |
| Šum kanálu | nad −80 dBm = rušení nebo zvlněné napájení rádia |
| Zachyceno od startu | pakety ostatních čidel v dosahu (druhý anemometr vysílá každou minutu, čidla retenčky každých 5 min); 0 po 5 min = nefunguje příjem (anténa, krystal, GDO0) |

Výpis navíc ukáže **ID čidla** (stejné jako na webu), **příčinu posledního restartu** (podpětí
= slabá baterie nebo napájení), napětí baterie, teplotu čipu, jestli je v čidle **uložený
párovací klíč** a stav kontaktu anemometru s počtem pulzů (otočte miskami a vyčtěte znovu).

## Technicky

- Firmware `uzel/src/diag.c`, překlad `make diag` (nebo `nahraj.ps1 -Diagnostika -Build`);
  hotový `uzel/build/diag.hex` je v repozitáři. Běžný firmware se tím nemění.
- Výsledek je v EEPROM od adresy 0xC0 (klíče párování jsou na 0x00–0x75, diagnostika je jen čte).
- V každém cyklu čidlo jednou vysílá testovací paket typu 0x17 bez šifrování. Retenčka ho
  započítá jako cizí paket a zahodí.
