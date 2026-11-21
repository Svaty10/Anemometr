/**
 * @file wind.c
 * @brief Reed contact of the anemometer on PA6 (fully asynchronous pin).
 *
 * The CPU sleeps in standby (RTC counter running). Every edge of the contact
 * wakes it for a few microseconds: the interrupt reads the level and the RTC tick
 * and counts the pulse (debounce in wind_logic.h). The 1 s PIT closes each
 * second: window sum, 3 s gust, stuck-contact handling. Nothing of this depends
 * on the main loop, so pulses are counted during the radio transmission and the
 * pairing computation as well.
 *
 * Pull-up: internal (~35 k), replaces R1 100 k of the anemometer adapter; C10
 * 22 nF of the board filters the contact (C1 10 pF of the adapter is not needed).
 */

#include "wind.h"
#include <avr/interrupt.h>
#include <util/atomic.h>
#include <util/delay.h>

static wind_t g_w;

#define PINCTRL_COUNT   (PORT_PULLUPEN_bm | PORT_ISC_BOTHEDGES_gc)
#define PINCTRL_PROBE   (PORT_PULLUPEN_bm | PORT_ISC_INTDISABLE_gc)
#define PINCTRL_OFF     (PORT_ISC_INPUT_DISABLE_gc)    /* no pull-up, no buffer */

static inline bool pin_low(void)
{
    return (VPORTA.IN & PIN_WIND) == 0;
}

ISR(PORTA_PORT_vect)
{
    uint8_t f = VPORTA.INTFLAGS;
    VPORTA.INTFLAGS = f;
    if (f & PIN_WIND) {
        (void)wind_edge(&g_w, pin_low(), RTC.CNT, WIND_DEBOUNCE_TICKS);
    }
}

void wind_init(void)
{
    PORTA.DIRCLR = PIN_WIND;
    WIND_PINCTRL = PINCTRL_COUNT;
    _delay_us(WIND_PROBE_US);                 /* C10 charges before the first edge counts */
    VPORTA.INTFLAGS = PIN_WIND;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        wind_reset(&g_w);
    }
}

/* PIT interrupt context (interrupts of the same level do not nest on AVR). */
void wind_on_second(void)
{
    wind_act_t a = wind_second(&g_w, pin_low(), WIND_STUCK_S, WIND_RECHECK_S);
    if (a == WIND_ACT_PULLUP_OFF) {
        WIND_PINCTRL = PINCTRL_OFF;
    } else if (a == WIND_ACT_PROBE) {
        WIND_PINCTRL = PINCTRL_PROBE;
        _delay_us(WIND_PROBE_US);             /* ~2.5 ms every WIND_RECHECK_S: ~1 uA */
        if (wind_probed(&g_w, pin_low())) {
            VPORTA.INTFLAGS = PIN_WIND;
            WIND_PINCTRL = PINCTRL_COUNT;     /* contact open: count again */
        } else {
            WIND_PINCTRL = PINCTRL_OFF;
        }
    }
}

uint16_t wind_window_seconds(void)
{
    uint16_t s;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        s = g_w.seconds;
    }
    return s;
}

void wind_take_window(wind_report_t *r)
{
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        wind_take(&g_w, r);
    }
}

void wind_off(void)
{
    WIND_PINCTRL = PINCTRL_OFF;
}
