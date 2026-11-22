/**
 * @file diag.c
 * @brief Diagnostic firmware of the anemometer node (separate build: make diag,
 *        5-diagnostika.cmd). Tests the board and the radio over and over and keeps
 *        the last result in the EEPROM (0xC0.., outside the pairing keys);
 *        6-precist-diagnostiku.cmd reads it over UPDI and explains it
 *        (docs/diagnostika.md). Works even with a dead radio; nothing on the hub.
 *
 * Cycle (~6 s): radio probe (SPI bus, crystal, PLL) -> battery + chip temperature
 * -> 2 s listening on the channel (any packet: tank sensors, the other anemometer)
 * -> one test transmission (plain DIAG packet 0x17; the tank hub counts it as a
 * foreign packet and drops it) -> EEPROM record -> 4 s sleep.
 * Reed pulses are counted the whole time.
 *
 * The pairing keys in the EEPROM are never written, only read (is a key there?).
 * Back to normal: flash the normal firmware (3-aktualizace-firmwaru.cmd), the
 * pairing is kept.
 */

#include "config.h"
#include "power.h"
#include "adc.h"
#include "wind.h"
#include "cc1101.h"
#include <avr/eeprom.h>
#include <avr/interrupt.h>
#include <string.h>

#define DIAG_VERSION        1U
#define DIAG_PERIOD_S       4U
#define DIAG_LISTEN_MS      2000U

/* EEPROM: the normal firmware keeps two copies of its key store (2 x 59 B) from 0 */
#define KEY_STORE_SIZE      59U
#define KEY_STORE_MAGIC     0x314D4E41UL     /* "ANM1", see link.c */
#define DIAG_EE_ADDR        0xC0U
#define DIAG_EE_MAGIC       0x47414944UL     /* "DIAG" */

/* flags byte of the DIAG packet */
#define DF_RF_SO            0x01U   /* radio answers (SO low after CSn), crystal runs */
#define DF_RF_SPI           0x02U   /* register write/read back ok */
#define DF_RF_PLL           0x04U   /* PLL locked after calibration */
#define DF_RF_GDO0          0x08U   /* GDO0 rose during the previous transmission */
#define DF_TX_OK            0x10U   /* previous transmission finished (TX -> IDLE, FIFO empty) */
#define DF_PAIRED           0x40U   /* a pairing key is stored in the EEPROM */
#define DF_REED_CLOSED      0x80U   /* anemometer contact closed right now */

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t cycles;
    uint16_t heard_total;             /* packets heard on the channel since the start */
    int8_t   heard_best;              /* strongest of them [dBm], -128 = none */
    uint8_t  pkt[PKT_DIAG_LEN];
    uint16_t crc;
} diag_ee_t;

static uint16_t crc16(const uint8_t *d, uint8_t n)
{
    uint16_t crc = 0xFFFF;
    for (uint8_t i = 0; i < n; i++) {
        crc ^= (uint16_t)d[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

/** A pairing key in either copy of the normal firmware's store (magic + paired flag). */
static bool key_stored(void)
{
    for (uint8_t i = 0; i < 2U; i++) {
        uint16_t a = (uint16_t)(i * KEY_STORE_SIZE);
        if (eeprom_read_dword((const uint32_t *)a) == KEY_STORE_MAGIC
                && eeprom_read_byte((const uint8_t *)(a + 4U)) == 1U) {
            return true;
        }
    }
    return false;
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

int main(void)
{
    uint8_t rst = power_take_reset_flags();

    power_init();
    wdt_init();
    adc_init();
    rtc_pit_init();
    uint16_t id = power_node_id();
    rng_seed(id);
    wind_init();
    bool paired = key_stored();

    diag_ee_t rec;
    uint8_t *pkt = rec.pkt;
    uint8_t seq = 0, prev = 0;               /* prev: DF_RF_GDO0 | DF_TX_OK of the last TX */
    uint32_t pulses = 0;
    uint16_t cycles = 0;
    rec.heard_total = 0;
    rec.heard_best = -128;

    for (;;) {
        wdt_kick();
        cc1101_probe_t pr;
        cc1101_probe(&pr);
        wdt_kick();

        uint16_t vbat = PKT_U16_INVALID;
        if (!adc_measure_battery(&vbat)) {
            vbat = PKT_U16_INVALID;
        }
        int8_t temp = PKT_TEMP_INVALID;
        if (!adc_measure_temp_c(&temp)) {
            temp = PKT_TEMP_INVALID;
        }

        cc1101_heard_t h;
        memset(&h, 0, sizeof(h));
        h.best_dbm = -128;
        h.floor_dbm = -128;
        if (pr.so_ready) {
            cc1101_listen(DIAG_LISTEN_MS, &h);
        }

        if ((uint16_t)(rec.heard_total + h.count) >= rec.heard_total) {
            rec.heard_total = (uint16_t)(rec.heard_total + h.count);
        }
        if (h.count && h.best_dbm > rec.heard_best) {
            rec.heard_best = h.best_dbm;
        }

        wind_report_t w;
        wind_take_window(&w);
        pulses += w.pulses;

        uint8_t flags = prev;
        flags |= pr.so_ready ? DF_RF_SO : 0U;
        flags |= pr.spi_rw ? DF_RF_SPI : 0U;
        flags |= pr.pll_lock ? DF_RF_PLL : 0U;
        flags |= paired ? DF_PAIRED : 0U;
        flags |= (PORTA.IN & PIN_WIND) ? 0U : DF_REED_CLOSED;

        pkt[0] = PKT_DIAG;
        put16(&pkt[1], id);
        pkt[3] = DIAG_VERSION;
        pkt[4] = seq;
        pkt[5] = rst;
        put16(&pkt[6], vbat);
        pkt[8] = (uint8_t)temp;
        pkt[9] = pr.partnum;
        pkt[10] = pr.version;
        pkt[11] = flags;
        pkt[12] = pr.marcstate;
        pkt[13] = pr.fscal1;
        put16(&pkt[14], pulses > 0xFFFFUL ? 0xFFFFU : (uint16_t)pulses);
        pkt[16] = h.count;
        pkt[17] = h.errors;
        pkt[18] = (uint8_t)h.best_dbm;
        pkt[19] = (uint8_t)h.floor_dbm;
        pkt[20] = h.last_type;
        put16(&pkt[21], h.last_id);

        /* ---------------- test transmission ---------------- */
        prev = 0;
        if (pr.so_ready) {
            wdt_kick();
            int8_t r = cc1101_send(pkt, PKT_DIAG_LEN, NULL, 0, 0);
            prev |= (r >= 0) ? DF_TX_OK : 0U;
            prev |= cc1101_gdo0_seen ? DF_RF_GDO0 : 0U;
        }

        /* ---------------- EEPROM record: this cycle with its own TX result ---------------- */
        pkt[11] = (uint8_t)((flags & (uint8_t)~(DF_RF_GDO0 | DF_TX_OK)) | prev);
        if (cycles < 15U || (cycles % 15U) == 0U) {      /* first ~2 min, then once a ~2 min */
            rec.magic = DIAG_EE_MAGIC;
            rec.cycles = cycles;
            rec.crc = crc16((const uint8_t *)&rec, (uint8_t)(sizeof(rec) - 2U));
            wdt_kick();
            eeprom_update_block(&rec, (void *)DIAG_EE_ADDR, sizeof(rec));
        }
        if (cycles < 0xFFFFU) {
            cycles++;
        }
        seq++;
        power_sleep_seconds(DIAG_PERIOD_S);
    }
}
