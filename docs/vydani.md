# Vydání nové verze

Stejný postup jako u retenčky. Vitr si nové verze stahuje sám z **GitHub Releases**
(podepsané Ed25519). Firmware anemometru se nahrává ručně (UPDI, `uzel/`) – na vzduchu se
neaktualizuje.

## 0. Jednorázově: podpisový klíč (dělá majitel repozitáře)

Bez toho žádný Release nevznikne a vitr nic nepřijme.

1. Vygenerovat pár klíčů (mimo repozitář a mimo OneDrive):
   ```bash
   openssl genpkey -algorithm ed25519 -out release.key
   openssl pkey -in release.key -pubout -out release.pub
   ```
   Lze použít i stejný klíč jako u retenčky (pak se tam už nic negeneruje).
2. GitHub → repozitář Anemometr → *Settings → Secrets and variables → Actions* → secret
   `RELEASE_SIGNING_KEY` = celý obsah `release.key`.
3. `release.pub` → `system/keys/release.pub`, commit + push.
4. Pi instalované dřív než bod 3: jednou spustit instalaci znovu (zkopíruje klíč do
   `/etc/vitr/release.pub`).

## 1. Jaké číslo verze

[SemVer](https://semver.org/) `MAJOR.MINOR.PATCH`: opravy → PATCH, nová funkce → MINOR,
nekompatibilní změna (rádiový protokol) → MAJOR; zkušební verze pro kanál `beta` s příponou
(`1.2.0-beta1`).

- **Databáze:** jen přidávat tabulky / sloupce (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD`),
  nic nemazat ani nepřejmenovávat – při návratu na starší verzi se vrací i záloha databáze, ale
  starší verze musí s novějším schématem aspoň naběhnout.
- **Rádiový protokol** (`uzel/src/proto.c` ↔ `server/vitr/crypto.py`): hub musí rozumět čidlům
  se starším firmwarem.

## 2. Kontrola před vydáním

```bash
python -m unittest discover -s server/tests        # vše OK
```

- `server/vitr/__init__.py` → `__version__ = "1.2.0"`.
- `CHANGELOG.md` → nová sekce `## [1.2.0] - RRRR-MM-DD` nahoře (z ní se berou poznámky k vydání
  v dialogu aktualizace).

## 3. Vydání

```bash
git commit -am "Verze 1.2.0"
git push
git tag v1.2.0                  # přesně "v" + __version__, jinak Action skončí chybou
git push origin v1.2.0
```

GitHub Action **release**: zkontroluje tag ↔ verzi, spustí testy, sestaví `vitr-1.2.0.tar.gz`,
podepíše (`.sig`) a vytvoří Release. Tag s pomlčkou se vydá jako *pre-release* (jen kanál beta).

## 4. Ověření

1. Release `v1.2.0` má soubory `vitr-1.2.0.tar.gz` **a** `vitr-1.2.0.tar.gz.sig`.
2. Na Pi: *Nastavení → Systém → Zkontrolovat aktualizace* → *Aktualizovat na 1.2.0* → do ~2 min
   „Aktualizováno na 1.2.0“. Podrobnosti: `journalctl -u vitr-update -n 50`.
3. Jinak si ji vitr stáhne sám v noci (04:00 + náhodné zpoždění), pokud jsou automatické
   aktualizace zapnuté.

## 5. Když se vydání nepovede

- **Action selhala:** opravit, smazat tag (`git tag -d v1.2.0 && git push --delete origin v1.2.0`)
  a vydat znovu.
- **Verze nenaběhla:** vitr se sám vrátil na předchozí verzi (i s databází) a tuto verzi si
  zablokoval (`/var/lib/vitr/update-bad-versions.json`). Opravit a vydat **vyšší** verzi.
