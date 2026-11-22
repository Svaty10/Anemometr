# Changelog

Formát dle [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), verze dle [SemVer](https://semver.org/).
Postup vydání nové verze: [docs/vydani.md](docs/vydani.md).

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
