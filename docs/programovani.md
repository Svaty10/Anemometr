# Programování čidla anemometru

Programátor, zapojení UPDI a řešení potíží jsou **stejné jako u retenčky**:
[Retencni_Nadrz_V2/docs/programovani_uzlu.md](../../Retencni_Nadrz_V2/docs/programovani_uzlu.md).
Rozdíly:

- Firmware se **nestahuje z GitHubu** – překládá se ze složky `uzel/`. Potřeba je
  avr-gcc ≥ 12 (např. [ZakKemble/avr-gcc-build](https://github.com/ZakKemble/avr-gcc-build/releases),
  rozbalit do `C:\avr`); Arduino avr-gcc 7.3 ATtiny1624 nezná.
  `nahraj.ps1` přeloží firmware sám, když `uzel\build\firmware.hex` chybí; `-Build` vynutí nový překlad.
- Skript nikdy nenahraje firmware retenčky. Čip s firmwarem retenčky se přehraje normálně;
  jeho párování z retenčky anemometr nepřevezme (jiný formát klíčů) a nabídne se jako nové čidlo.
- Tovární reset „můstkem na konektoru“ anemometr nemá (stojící rotor se sepnutým kontaktem
  vypadá stejně). Párování se maže z webu (odpárovat) nebo `4-smazat-parovani.cmd`.

```bash
cd uzel
make            # build/firmware.hex
make test       # test kryptografie + logiky čítání pulzů na PC
```

| Soubor | Co dělá |
|---|---|
| `1-test-spojeni.cmd` | přečte signaturu čipu |
| `2-novy-cip.cmd` | nový čip: fuses + firmware |
| `3-aktualizace-firmwaru.cmd` | jen firmware (párování zůstane) |
| `4-smazat-parovani.cmd` | smaže klíč v EEPROM |
| `5-diagnostika.cmd` | nahraje diagnostický firmware (test rádia a desky), viz [diagnostika.md](diagnostika.md) |
| `6-precist-diagnostiku.cmd` | vyčte z čidla výsledek diagnostiky a vysvětlí ho |
