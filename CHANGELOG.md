# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

## [1.2.1] - 2026-11-22

### Přidáno
- Grafy větru (jednoho anemometru i všech) mají i krátké rozsahy **10 min** a **1 h**, s popisky osy po minutách. Každý bod je jedno skutečné minutové měření čidla, mezi body jsou rovné úsečky (bez vyhlazení). Graf baterie krátké rozsahy nemá – baterie se měří jednou za hodinu.

## [1.2.0] - 2026-11-22

### Přidáno
- **Baterie jako u retenčky:** u každého anemometru se volí typ baterie (lithiové Li-FeS2 / alkalické) a procenta se počítají podle vybíjecí křivky daného typu (stejné křivky jako retenčka). V pokročilém nastavení **Kalibrace měření baterie**: zadáte napětí změřené voltmetrem a hub spočítá korekci (násobek, max. ±20 %). Změny se zapisují do Historie událostí.
- **Grafy jako retenčka 1.25:** uprostřed grafu pevná čára, tažením (nebo šipkami) se posouvá čas, bublina nad grafem ukazuje hodnoty pod čarou a tlačítko *Aktuální →* vrací na poslední měření; starší data se dotahují při posouvání. Popisky osy na celé hodiny / dny / měsíce.
- Graf **Baterie a signál** v procentech (baterie i signál na jedné ose 0–100 %), se značkami vložení baterie a restartu čidla.

### Změněno
- Signál všude stejně jako u retenčky: „dobrý · 57 % (−76 dBm)“, 0 % = −110 dBm, 100 % = −50 dBm (dříve −60 dBm); na kartě v přehledu všech anemometrů je i signál.
- Napětí baterie se ukazuje na tisíciny voltu.

## [1.1.2] - 2026-11-22

### Změněno
- **Bez teploty:** anemometr nemá teploměr, takže web ani Home Assistant už neukazují „Teplotu čidla“ (byla to jen teplota uvnitř čipu ATtiny, kterou čidlo potřebuje pro mrazovou pojistku baterie) a hub ji neukládá. Entita teploty v Home Assistantu se sama smaže.

## [1.1.1] - 2026-11-22

### Opraveno
- **Párování se mohlo zaseknout na „čekám na data“:** odpověď na žádost čidla (čidlo poslouchá jen 0,6 s) šla až po zápisu do databáze na SD kartu a mohla přijít pozdě. Teď se odpověď pošle hned z paměti (jako u retenčky) a databáze se zapíše až potom; klíč potvrzeného ID se počítá už při potvrzení.
- Most v logu retenčky zapisuje, za kolik ms vitr odpověděl, nebo že neodpověděl (`journalctl -u retencka | grep vitr-most`); vitr zapisuje čas odpovědi do svého logu.
- Když nová verze změní most, instalace i aktualizace retenčku restartují, aby se načetl.

## [1.1.0] - 2026-11-22

### Přidáno
- **Automatické aktualizace z GitHubu, stejně jako u retenčky:** Nastavení → Systém → *Zkontrolovat aktualizace*, dialog s poznámkami k vydání a průběhem instalace, tečka u Nastavení, když je nová verze. Balíky jsou podepsané (Ed25519); když nová verze nenaběhne, vitr se sám vrátí na předchozí i s databází. Volba automatických aktualizací (v noci) a kanálu stabilní / beta.
- **Párování jako u retenčky:** *Přidat čidlo…* – dialog s kroky, ID ze štítku a název, nová čidla v dosahu. Čidlo se spáruje jen po potvrzení svého ID; odpárování se dokončí, až čidlo po vložení baterie potvrdí. Klíče odebraných čidel se uschovají, takže nalezené čidlo jde *Spárovat znovu* bez programátoru.
- Pokročilá nastavení: *Čidla v dosahu* a údaje o příjmu.

### Změněno
- **Retenčka se kvůli anemometrům nemění:** most k jejímu rádiu přidá instalace vitru jako doplněk služby (`retencka.service.d/vitr-most.conf`); přežije aktualizace retenčky a odebere se `system/odinstalovat-most.sh`.
- Aplikace je v `/opt/vitr/releases/<verze>` s odkazem `current` (kvůli aktualizacím a návratu na předchozí verzi).

## [1.0.0] - 2026-11-21

### Přidáno
- Firmware anemometru (ATtiny1624 + CC1101 na desce retenčky), služba `vitr` s webem na portu 8080 ve stylu retenčky, uživatelské účty a historie událostí, MQTT do Home Assistantu.
