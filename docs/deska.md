# Deska čidla anemometru

Používá se **deska čidla retenční nádrže** (`Retencni_Nadrz_V2/pcb/retencka_V2/uzel`), jen
jinak osazená. Na místě vstupu hydrostatické sondy (PA6) je jazýčkový kontakt anemometru
(LaskaKit, konektor RJ11).

## Co neosadit

| Díl | Proč |
|---|---|
| **U4 TPS63900**, L1, C6, C7, R8, R9 | 5V měnič pro sondu – anemometr nepotřebuje napájení |
| **U2 SHT40**, R6, R7, C3 | teplota/vlhkost se neměří (teplotu pro mrazovou pojistku baterie měří ATtiny sám) |
| **R12 220 k** | dělič sondy proti GND: s interním pull-upem by trvale bral ~13 µA a stáhl by úroveň |

## Co změnit

| Díl | Retenčka | Anemometr | Proč |
|---|---|---|---|
| **R11** | 100 k | **0 Ω** (nebo 1 k) | se 100 k by sepnutý kontakt stáhl pin jen na ~2,2 V (interní pull-up ~35 k), čip by ho četl jako „1“ |
| **C10** | 22 nF | **22 nF – ponechat** | s interním pull-upem RC ≈ 0,8 ms: vyhladí zákmity kontaktu |

Zbytek (ATtiny1624, CC1101, LDO, spínaný dělič baterie, ochrana proti přepólování) zůstává
jako u retenčky.

## Zapojení anemometru

Adaptér LaskaKit (na obrázku) má R1 100 k proti VCC a C1 10 pF. **Oba jsou zbytečné**:

- **R1 100 k** nahrazuje interní pull-up ATtiny (firmware ho zapíná sám),
- **C1 10 pF** nahrazuje C10 22 nF na desce (filtruje mnohem lépe; 10 pF prakticky nic nedělá).

Anemometr se proto připojí **přímo**, bez adaptéru (nebo s adaptérem bez VCC):

| RJ11 anemometru | Konektor desky `Senzor_J2` |
|---|---|
| pin 4 (OUT, kontakt) | signál (SENS_ADC, dříve výstup sondy) |
| pin 3 (GND) | GND |
| ostatní | nezapojeno; **5V pin J2 nezapojovat** (měnič není osazen) |

## Spotřeba

Čip spí ve standby (běží jen RTC z 32kHz oscilátoru) a probouzí ho každá hrana kontaktu
na pár mikrosekund. Odhad:

| Stav | Proud |
|---|---|
| spánek (RTC, WDT, CC1101 SLEEP) | ~1,5 µA |
| vysílání 1× za ~60 s (~1,7 mAs) | ~28 µA |
| pulzy (probuzení ~10 µs na hranu) | < 1 µA i při silném větru |
| pull-up přes sepnutý kontakt, když se točí | ~95 µA × podíl sepnutí (typicky 10–20 %) → 10–20 µA |
| rotor stojí se sepnutým kontaktem | pull-up se po ~3 s vypne, kontrola každé 2 s → ~1 µA |

**Průměr ~30 µA za klidu, ~45 µA ve větru** → 3× AA Li-FeS2 (3000 mAh) odhadem 7–10 let,
alkalické kratší (samovybíjení). Pokud by spotřeba ve větru vadila: místo interního
pull-upu externí 1 MΩ na 3,3 V a C10 zmenšit na 1 nF (RC 1 ms) – proud přes kontakt klesne
na ~3 µA; ve firmwaru pak vypnout `PORT_PULLUPEN_bm` v `uzel/src/wind.c`.
