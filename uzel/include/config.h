/**
 * @file config.h
 * @brief Global hardware configuration, tuning constants and the radio packet
 *        of the ATtiny1624 anemometer node (board of the tank sensor, see
 *        docs/deska.md for the assembly changes).
 *
 * Every constant that may need tuning on real hardware lives in this file.
 */

#ifndef CONFIG_H_
#define CONFIG_H_

#include <avr/io.h>
#include <stdint.h>
#include <stdbool.h>

/* ========================================================================== */
/* CPU clock: OSC20M / 6 = 3.333 MHz (safe down to 1.8 V, low active current) */
/* ========================================================================== */
#ifndef F_CPU
#define F_CPU 3333333UL
#endif

/* ========================================================================== */
/* Pinout (ATtiny1624, SOIC-14) – the tank sensor board                       */
/* ========================================================================== */
/* PORT A */
#define PIN_UPDI_PROG           PIN0_bm   /* PA0: UPDI */
#define PIN_SPI_MOSI            PIN1_bm   /* PA1: CC1101 SI   */
#define PIN_SPI_MISO            PIN2_bm   /* PA2: CC1101 SO   */
#define PIN_SPI_SCK             PIN3_bm   /* PA3: CC1101 SCLK */
#define PIN_SPI_CSN             PIN4_bm   /* PA4: CC1101 CSn (active low) */
#define PIN_ADC_BAT             PIN5_bm   /* PA5: battery divider  (AIN5) */
#define PIN_WIND                PIN6_bm   /* PA6: anemometer reed contact to GND
                                             (was the probe input; fully asynchronous
                                             pin = wakes the chip from standby on any edge) */
#define PIN_EN_BAT_MEASURE      PIN7_bm   /* PA7: battery divider switch (active high) */
#define WIND_PINCTRL            PORTA.PIN6CTRL
/* PORT B – not used on the anemometer (SHT40 and the 5 V boost not fitted):
 * PB0/PB1 SCL/SDA, PB2 CC1101 GDO0, PB3 boost enable (driven low) */
#define PIN_I2C_SCL             PIN0_bm
#define PIN_I2C_SDA             PIN1_bm
#define PIN_RF_GDO0             PIN2_bm
#define PIN_EN_5V_PROBE         PIN3_bm

/* ========================================================================== */
/* Wind measurement                                                           */
/* ========================================================================== */
/** One measurement window = one packet. The node counts reed pulses the whole
 *  time; when the window ends it sends the result and the next window starts at
 *  once (nothing is lost between windows). The window length is random in
 *  WINDOW_S +- JITTER_S/2 (the true length is in the packet): two anemometers and
 *  the tank sensor share one channel, and a random period keeps their RC clocks
 *  from transmitting in step packet after packet. */
#define WINDOW_S                60U
#define JITTER_S                16U

/** Debounce: a falling edge counts as a pulse only when no other edge came during
 *  the previous WIND_DEBOUNCE_TICKS (RTC ticks of 1/32768 s, 98 = 3 ms). The 22 nF
 *  C10 on the board already smooths the contact (~0.8 ms with the pull-up), the
 *  rule removes the rest of the release bounce. Max. 166 Hz – far above any wind. */
#define WIND_DEBOUNCE_TICKS     98U

/** Contact closed for good (the cups stopped with the magnet over the reed): the
 *  internal pull-up would draw ~95 uA all the time. After WIND_STUCK_S seconds
 *  low without an edge the pull-up is switched off and the node only looks every
 *  WIND_RECHECK_S whether the contact opened (pull-up on for WIND_PROBE_US). */
#define WIND_STUCK_S            2U
#define WIND_RECHECK_S          2U
#define WIND_PROBE_US           2500U   /**< ~3 tau of 35 k x 22 nF */

/* ========================================================================== */
/* Battery (3x AA in series, Li-FeS2 or alkaline)                             */
/* ========================================================================== */
#define BATTERY_PERIOD_S        3600U   /**< Battery is measured once per hour (value repeated in between) */
/** Below this the packet carries PKT_FLAG_BAT_LOW (warning, ~1 % left). */
#define BATTERY_LOW_MV          3300U

/** End of life: 1.0 V per cell at rest. Below this (CUTOFF_CONFIRM hourly readings
 *  in a row, not in frost – cold cells sag temporarily) the node sends its last
 *  packet and switches itself off for good – only a new battery starts it again.
 *  Draining a series pack further could drive the weakest cell into reversal (and
 *  an alkaline cell into leaking). The BOD fuse (1.8 V) stays only as the last
 *  resort for the MCU. The frost check uses the chip's own temperature sensor. */
#define BATTERY_CUTOFF_MV       3000U
/** The same under TX load (~30 mA): 0.9 V per cell. */
#define BATTERY_CUTOFF_LOAD_MV  2700U
#define CUTOFF_CONFIRM          3U
#define CUTOFF_MIN_TEMP_C       0       /**< no cut-off decision below 0 degC */
/** Pairing (X25519) at 10 MHz only above this battery voltage at boot, else 5 MHz. */
#define PAIR_10MHZ_MIN_MV       3100U

/** Battery divider (high side switched): R_top / R_bot in kOhm.
 *  Measured against the internal 1.024 V reference:
 *  full scale = 1.024 * 1220 / 220 = 5.68 V (fresh 3x Li-FeS2 ~5.4 V open circuit). */
#define BAT_RTOP_K              1000UL
#define BAT_RBOT_K              220UL
#define BAT_SETTLE_MS           20U     /**< 5 tau of divider + 22 nF C0G */

/* ========================================================================== */
/* Radio                                                                      */
/* ========================================================================== */
/** PATABLE value: 0xC0 = +10 dBm, 0x60 = 0 dBm (433 MHz band). */
#define RF_PA_VALUE             0xC0

/** Listen before talk: ~3 ms in RX before each transmission; if the channel is
 *  busy (the other anemometer, the tank sensor, a remote control) wait a random
 *  20-147 ms and try again, after LBT_MAX_TRIES send anyway. */
#define LBT_BUSY_DBM            (-90)
#define LBT_MAX_TRIES           5U
#define LBT_BACKOFF_MIN_MS      20U
#define LBT_BACKOFF_SPAN_MS     128U    /**< power of two */

/* ========================================================================== */
/* Radio protocol – the same frames and crypto as the tank sensor, but its own */
/* packet types (0x13..0x16) and key labels: the tank hub ignores anemometer  */
/* frames, an anemometer never accepts a tank PAIR_ACK and keys of the two    */
/* systems can never be confused. CC1101 variable length, little endian.      */
/*                                                                            */
/* DATA     23 B  [0x13][node_id 2][ctr 4][ciphertext 12][mac 4]              */
/*                plaintext = telemetry_t (12 B), keystream = AES(Kenc,       */
/*                node_id|ctr|0..), mac = CMAC(Kmac, bytes 0..18)[0..3]       */
/* PAIR_REQ 36 B  [0x14][node_id 2][node X25519 public key 32][state 1]       */
/* PAIR_ACK 35 B  [0x15][node_id 2][hub  X25519 public key 32]   (hub -> node)*/
/* UNPAIR   19 B  [0x16][node_id 2][CMAC(Kmac, 0x16|node_id|node pub) 16]     */
/*                shared S = X25519; K0 = S[0..15] ^ S[16..31];               */
/*                Kenc = AES(K0,"ANM-ENC-KEY"), Kmac = AES(K0,"ANM-MAC-KEY")   */
/* ========================================================================== */
#define PKT_DATA                0x13
#define PKT_PAIR_REQ            0x14
#define PKT_PAIR_ACK            0x15
#define PKT_UNPAIR              0x16
#define PKT_DATA_LEN            23U
#define PKT_PAIR_LEN            35U     /**< PAIR_ACK */
#define PKT_PAIR_REQ_LEN        36U
#define PKT_UNPAIR_LEN          19U
/** Diagnostic firmware only (diag.c, docs/diagnostika.md): plain test transmission,
 *  nobody answers it (the tank hub counts it as a foreign packet). */
#define PKT_DIAG                0x17
#define PKT_DIAG_LEN            23U
#define PAIR_REQ_PAIRED         0x01
#define PKT_MAC_LEN             4U
#define FW_VERSION              10U     /**< 1.0 – reported in every packet */

#define PAIR_WINDOW_S           600U    /**< Pairing offered for 10 min after a battery change */
#define PAIR_PERIOD_S           5U      /**< One PAIR_REQ every 5 s inside the window */
#define PAIR_RX_WINDOW_MS       600U    /**< Listen for PAIR_ACK / UNPAIR after each request */
#define UNPAIRED_FAST_S         60U     /**< Unpaired node: one PAIR_REQ per minute ... */
#define UNPAIRED_FAST_TIME_S    86400UL /**< ... for the first day, then every UNPAIRED_SLOW_S */
#define UNPAIRED_SLOW_S         300U
#define CTR_RESERVE             1024UL  /**< Frame counter persisted in EEPROM in steps of 1024 */

#define PKT_FLAG_BAT_ERR        0x01    /**< Battery ADC conversion failed */
#define PKT_FLAG_TEMP_ERR       0x02    /**< Chip temperature not available */
#define PKT_FLAG_STUCK          0x04    /**< Contact found closed for good in this window */
#define PKT_FLAG_OVERFLOW       0x08    /**< Pulse counter saturated (values are lower bounds) */
#define PKT_FLAG_BAT_LOW        0x10    /**< Battery below BATTERY_LOW_MV (warning only) */
#define PKT_FLAG_BOOT           0x20    /**< First packet after a reset */
#define PKT_FLAG_WDT_RESET      0x40    /**< Last reset was caused by the watchdog */
#define PKT_FLAG_PARTIAL        0x80    /**< Data before this window were dropped (just paired) */

#define PKT_TEMP_INVALID        INT8_MIN
#define PKT_U16_INVALID         0xFFFFU

/* Diagnostic byte (telemetry_t.diag) – what happened since the previous packet */
#define DIAG_LBT_MASK           0x03    /**< LBT back-offs before the previous TX (3 = 3 or more) */
#define DIAG_BOD_RESET          0x08    /**< last reset was a brown-out (sent once) */
#define DIAG_BAT_FRESH          0x10    /**< battery measured in this cycle */
#define DIAG_RADIO_REINIT       0x20    /**< a TX failed, the radio was re-initialised */
#define PKT_SAG_UNKNOWN         0xFFU   /**< vbat_sag not measured yet */
#define SAG_UNIT_MV             5U      /**< vbat_sag unit */

/** Important packets (first after a reset / pairing, last before the cut-off) are
 *  sent twice, the copy after a random 150-405 ms (same frame, the hub drops it
 *  if the first one arrived). */
#define REPEAT_GAP_MIN_MS       150U
#define REPEAT_GAP_SPAN_MS      256U    /**< power of two */

/** Encrypted part of a DATA packet (Python: '<BHHHBbBBB'). */
typedef struct __attribute__((packed)) {
    uint8_t  flags;        /**< PKT_FLAG_* */
    uint16_t vbat_mv;      /**< Battery voltage at rest [mV] (measured hourly) */
    uint16_t pulses;       /**< Reed pulses in the window */
    uint16_t gust_pulses;  /**< Max. pulses in any 3 s inside the window (WMO gust) */
    uint8_t  window_s;     /**< Window length [s] (node clock, ~+-3 %) */
    int8_t   temp_c;       /**< Chip temperature [degC], PKT_TEMP_INVALID if unknown */
    uint8_t  fw;           /**< FW_VERSION */
    uint8_t  diag;         /**< DIAG_* */
    uint8_t  vbat_sag;     /**< Battery drop during TX [SAG_UNIT_MV] (last hourly measurement) */
} telemetry_t;

_Static_assert(sizeof(telemetry_t) == 12, "telemetry must be 12 bytes");

#endif /* CONFIG_H_ */
