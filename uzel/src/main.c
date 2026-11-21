/**
 * @file main.c
 * @brief ATtiny1624 anemometer node – count reed pulses while sleeping, send one
 *        encrypted packet per window.
 *
 * Cycle (all the time, windows follow each other without a gap):
 *   the CPU sleeps in standby; every edge of the reed contact wakes it for a few
 *   microseconds (wind.c), the 1 s PIT closes each second (window sum, 3 s gust).
 *   When the window (WINDOW_S +- JITTER_S/2, random) is over:
 *   [battery, hourly, ~25 ms] -> chip temperature (~1 ms) -> encrypted TX (~55 ms)
 *   -> the next window has already started.
 *   Pulses are counted during the transmission too (interrupts).
 *
 * Collisions: two anemometers and the tank sensor share one channel. The node ID
 * comes from the chip serial number, the window length is random per window (the
 * nodes never stay in step) and listen-before-talk defers a TX while another
 * node is on air (cc1101.c).
 *
 * Wind speed is computed on the hub: speed = pulses / window * factor of the
 * anemometer (set on the web, e.g. 2.4 km/h per 1 pulse/s). The hub also corrects
 * the node's RC clock from the packet arrival times.
 *
 * Pairing: after a battery change (power-on reset) the node sends a PAIR_REQ
 * every 5 s for PAIR_WINDOW_S and listens 0.6 s for the hub's answer (as the tank
 * sensor, see link.c). A paired node keeps reporting in the meantime.
 *
 * Battery: below BATTERY_LOW_MV the packets carry PKT_FLAG_BAT_LOW; below
 * BATTERY_CUTOFF_MV at rest (1.0 V/cell) or BATTERY_CUTOFF_LOAD_MV under TX load
 * (0.9 V/cell), confirmed over several hours, not in frost (chip temperature), the
 * node sends a last packet and switches itself off for good.
 *
 * Factory reset: only with the programmer (4-smazat-parovani.cmd) or by the hub
 * (unpair). A bridge on the input at power-on is not used: cups that stopped with
 * the magnet over the reed look exactly the same.
 */

#include "config.h"
#include "power.h"
#include "adc.h"
#include "wind.h"
#include "cc1101.h"
#include "link.h"
#include <avr/interrupt.h>
#include <avr/sleep.h>
#include <stddef.h>

typedef struct {
    uint16_t node_id;
    uint8_t  sticky_flags;          /* BOOT / WDT_RESET / PARTIAL, sent once */
    bool     rf_ok;

    /* battery (measured once per BATTERY_PERIOD_S, inside a normal cycle) */
    uint16_t vbat_mv;
    bool     vbat_valid;
    uint32_t since_bat_s;

    /* battery under TX load (measured hourly during the transmission) */
    uint16_t vbat_load_mv;
    bool     load_ok;
    uint8_t  sag;                   /* rest - load [SAG_UNIT_MV], sent in the next packets */

    /* battery end of life */
    uint8_t  low_count;             /* hourly readings below the cut-off in a row */
    bool     cutoff;

    /* diagnostics for the next packet (DIAG_*) */
    uint8_t  diag;
} node_state_t;

static node_state_t g;

static bool radio_ready(void)
{
    if (!cc1101_sleep_ok) {
        g.rf_ok = false;                     /* last SLEEP strobe timed out: radio may be awake */
    }
    if (!g.rf_ok) {
        g.rf_ok = cc1101_init();             /* retry radio init every cycle */
    }
    return g.rf_ok;
}

/** Sleep until the current window has `target_s` seconds (the PIT counts them in
 *  wind.c, so time spent awake – TX, pairing – is part of the window). */
static void wait_window(uint16_t target_s)
{
    for (;;) {
        cli();
        if (wind_window_seconds() >= target_s) {
            sei();
            return;
        }
        sei();                               /* SEI + SLEEP is atomic */
        sleep_cpu();
        wdt_kick();
    }
}

/** Battery under load: runs while the frame is on air (~30 mA from the cells). */
static void tx_battery_hook(void)
{
    uint16_t v = 0;
    g.load_ok = adc_measure_battery(&v);
    g.vbat_load_mv = v;
}

/** Close the window and send one encrypted DATA packet. */
static void report(void)
{
    telemetry_t t = {0};
    wind_report_t w;
    bool bat_now = false;

    wind_take_window(&w);                    /* the next window starts now */
    g.since_bat_s += w.seconds;
    t.fw = FW_VERSION;
    t.pulses = w.pulses;
    t.gust_pulses = w.gust_pulses;
    t.window_s = (w.seconds > 255U) ? 255U : (uint8_t)w.seconds;
    if (w.overflow) {
        t.flags |= PKT_FLAG_OVERFLOW;
    }
    if (w.stuck) {
        t.flags |= PKT_FLAG_STUCK;
    }

    wdt_kick();
    /* ---------------- battery (hourly, inside this wake-up) ---------------- */
    if (g.since_bat_s >= BATTERY_PERIOD_S || !g.vbat_valid) {
        uint16_t v = 0;
        g.vbat_valid = adc_measure_battery(&v);
        if (g.vbat_valid) {
            g.vbat_mv = v;
        }
        g.since_bat_s = 0;
        bat_now = g.vbat_valid;
    }
    if (g.vbat_valid) {
        t.vbat_mv = g.vbat_mv;
        if (g.vbat_mv < BATTERY_LOW_MV) {
            t.flags |= PKT_FLAG_BAT_LOW;     /* warning only, keep working */
        }
    } else {
        t.flags |= PKT_FLAG_BAT_ERR;
        t.vbat_mv = PKT_U16_INVALID;
    }

    /* ---------------- chip temperature (every packet, ~1 ms) ---------------- */
    int8_t temp = PKT_TEMP_INVALID;
    bool temp_ok = adc_measure_temp_c(&temp);
    if (!temp_ok) {
        t.flags |= PKT_FLAG_TEMP_ERR;
        temp = PKT_TEMP_INVALID;
    }
    t.temp_c = temp;

    /* ---------------- transmit ---------------- */
    t.flags |= g.sticky_flags;
    t.diag = (uint8_t)(g.diag | (bat_now ? DIAG_BAT_FRESH : 0U));
    t.vbat_sag = g.sag;
    bool important = (t.flags & PKT_FLAG_BOOT) || g.cutoff;
    g.load_ok = false;
    if (radio_ready()) {
        wdt_kick();
        cc1101_tx_hook = bat_now ? tx_battery_hook : NULL;   /* battery under TX load */
        bool sent = link_send_data(&t);
        cc1101_tx_hook = NULL;
        if (sent) {
            g.sticky_flags = 0;              /* delivered to the air at least once */
            g.diag = (cc1101_lbt_backoffs > 3U) ? 3U : cc1101_lbt_backoffs;
            if (important) {
                power_sleep_ms((uint16_t)(REPEAT_GAP_MIN_MS + (rng_next() & (REPEAT_GAP_SPAN_MS - 1U))));
                wdt_kick();
                (void)link_resend_last();
            }
        } else {
            g.rf_ok = false;                 /* force a full re-init next time */
            g.diag |= DIAG_RADIO_REINIT;
        }
    }

    /* ---------------- battery end of life (hourly, at rest or under TX load) ---- */
    if (bat_now) {
        bool empty = g.vbat_mv < BATTERY_CUTOFF_MV;
        if (g.load_ok) {
            uint16_t drop = (g.vbat_mv > g.vbat_load_mv) ? (uint16_t)(g.vbat_mv - g.vbat_load_mv) : 0U;
            drop = (uint16_t)((drop + SAG_UNIT_MV / 2U) / SAG_UNIT_MV);
            g.sag = (drop > 254U) ? 254U : (uint8_t)drop;
            if (g.vbat_load_mv < BATTERY_CUTOFF_LOAD_MV) {
                empty = true;
            }
        }
        if (empty) {
            if (g.low_count < 255U) {
                g.low_count++;
            }
        } else {
            g.low_count = 0;
        }
        if (g.low_count >= CUTOFF_CONFIRM && (!temp_ok || temp >= CUTOFF_MIN_TEMP_C)) {
            g.cutoff = true;                 /* the next packet is the last one */
        }
    }
}

/** Last packet (sent twice), then off for good. */
static void shut_down(void)
{
    report();
    power_off_forever();
}

/** Batteries already empty when inserted: two readings ~2 s apart, no frost. */
static bool battery_dead_at_boot(uint16_t *vbat)
{
    uint16_t v1 = 0, v2 = 0;
    *vbat = 0;
    if (!adc_measure_battery(&v1)) {
        return false;
    }
    *vbat = v1;
    if (v1 >= BATTERY_CUTOFF_MV) {
        return false;
    }
    power_sleep_seconds(2);
    if (!adc_measure_battery(&v2) || v2 >= BATTERY_CUTOFF_MV) {
        return false;
    }
    int8_t temp = 0;
    return !adc_measure_temp_c(&temp) || temp >= CUTOFF_MIN_TEMP_C;
}

/** Freshly paired: the window that ran while unpaired may be very long (nothing
 *  was sent) – drop it and start a new one; the first packet is marked. */
static void just_paired(void)
{
    wind_report_t dump;
    wind_take_window(&dump);
    g.sticky_flags |= PKT_FLAG_BOOT | PKT_FLAG_PARTIAL;
}

/** Window length for the next window: WINDOW_S +- JITTER_S/2, random. */
static uint16_t next_window(void)
{
    return (uint16_t)(WINDOW_S - JITTER_S / 2U + (rng_next() % JITTER_S));
}

int main(void)
{
    uint8_t rst = power_take_reset_flags();

    power_init();
    wdt_init();
    adc_init();
    rtc_pit_init();

    g.node_id = power_node_id();
    rng_seed(g.node_id);                       /* unique per chip -> nodes drift apart */
    g.sticky_flags = PKT_FLAG_BOOT | ((rst & RSTCTRL_WDRF_bm) ? PKT_FLAG_WDT_RESET : 0);
    g.since_bat_s = BATTERY_PERIOD_S;          /* measure in the first cycle */
    g.sag = PKT_SAG_UNKNOWN;
    if (rst & RSTCTRL_BORF_bm) {
        g.diag |= DIAG_BOD_RESET;
    }

    wdt_kick();
    g.rf_ok = cc1101_init();                   /* also puts the radio to SLEEP */
    uint16_t vbat_boot;
    if (battery_dead_at_boot(&vbat_boot)) {
        power_off_forever();                   /* no pairing on empty cells */
    }
    link_set_fast_clock(vbat_boot >= PAIR_10MHZ_MIN_MV);
    bool power_on = (rst & (RSTCTRL_PORF_bm | RSTCTRL_BORF_bm | RSTCTRL_UPDIRF_bm)) != 0;
    bool paired = link_init(g.node_id);
    wind_init();                               /* counting from now on */

    /* ---------------- pairing window after a battery change ---------------- */
    if (power_on || !paired) {
        uint16_t left = PAIR_WINDOW_S;
        uint16_t target = next_window();
        while (left > 0) {
            if (link_paired() && wind_window_seconds() >= target) {
                report();                        /* keep reporting with the old keys */
                target = next_window();
                if (g.cutoff) {
                    shut_down();
                }
            }
            if (radio_ready()) {
                uint16_t s0 = wind_window_seconds();
                link_pair_t r = link_pair_attempt();
                if (r == LINK_PAIR_PAIRED) {
                    just_paired();
                    break;
                }
                /* LINK_PAIR_UNPAIRED: released by the hub, keep offering pairing in
                 * this window (re-pair to the same hub, or to a new one) */
                uint16_t spent = (uint16_t)(wind_window_seconds() - s0);
                left = (left > spent) ? (uint16_t)(left - spent) : 0U;
            }
            power_sleep_seconds(PAIR_PERIOD_S);
            left = (left > PAIR_PERIOD_S) ? (uint16_t)(left - PAIR_PERIOD_S) : 0U;
        }
    }

    uint32_t unpaired_s = 0;
    for (;;) {
        if (!link_paired()) {
            /* unpaired: keep offering pairing until a hub accepts, nothing else to do */
            if (radio_ready() && link_pair_attempt() == LINK_PAIR_PAIRED) {
                just_paired();
                continue;
            }
            uint16_t s = (unpaired_s < UNPAIRED_FAST_TIME_S) ? UNPAIRED_FAST_S : UNPAIRED_SLOW_S;
            unpaired_s += s;
            power_sleep_seconds(s);
            continue;
        }
        wait_window(next_window());
        report();
        if (g.cutoff) {
            shut_down();
        }
    }
}
