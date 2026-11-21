/**
 * @file wind_logic.h
 * @brief Pulse counting, gust and stuck-contact decisions – pure functions, no
 *        hardware, so they can be unit-tested on the host (tools/wind_test.c).
 *
 * The hardware glue (wind.c) calls:
 *   wind_edge()    from the pin interrupt on every edge of the reed contact,
 *   wind_second()  from the 1 s RTC PIT interrupt,
 *   wind_take()    from the main loop when a window ends.
 */

#ifndef WIND_LOGIC_H_
#define WIND_LOGIC_H_

#include <stdint.h>
#include <stdbool.h>

#define WIND_GUST_SPAN  3U      /**< gust = max. pulses within this many seconds */

typedef struct {
    /* debounce (edge interrupt) */
    uint16_t last_edge;         /**< RTC tick of the previous edge */
    bool     quiet;             /**< no edge during the whole previous second */
    bool     edge_seen;         /**< an edge came during the current second */
    uint16_t sec_pulses;        /**< pulses counted in the current second */

    /* window (1 s tick) */
    uint16_t ring[WIND_GUST_SPAN]; /**< pulses of the last seconds, for the gust */
    uint8_t  ring_pos;
    uint8_t  ring_fill;         /**< seconds in the ring since the start (gust needs a full ring) */
    uint32_t pulses;            /**< pulses in the window */
    uint16_t gust;              /**< max. sum over WIND_GUST_SPAN s in the window */
    uint16_t seconds;           /**< seconds in the window */
    bool     stuck_seen;        /**< contact was found stuck in this window */

    /* stuck contact (1 s tick) */
    bool     stuck;             /**< pull-up switched off, contact closed */
    uint8_t  low_s;             /**< seconds the pin has been low without an edge */
    uint8_t  recheck_s;
} wind_t;

typedef struct {
    uint16_t pulses;
    uint16_t gust_pulses;
    uint16_t seconds;
    bool     overflow;
    bool     stuck;
} wind_report_t;

static inline void wind_reset(wind_t *w)
{
    *w = (wind_t){0};
    w->quiet = true;
}

/**
 * @brief An edge on the reed input.
 * @param level_low pin level read in the interrupt (true = contact closed)
 * @param now       RTC tick (32768 Hz, free running 16 bit)
 * @param min_ticks debounce: the previous edge must be at least this old
 * @return true when the edge is a new pulse
 *
 * A pulse = a falling edge after a stable period (no edge for min_ticks, or a
 * whole quiet second – the 16-bit tick wraps after 2 s). The RC filter on the
 * input keeps the closing clean; bounce on release gives edges close together,
 * which never pass the stability rule.
 */
static inline bool wind_edge(wind_t *w, bool level_low, uint16_t now, uint16_t min_ticks)
{
    bool stable = w->quiet || (uint16_t)(now - w->last_edge) >= min_ticks;
    w->last_edge = now;
    w->quiet = false;
    w->edge_seen = true;
    w->low_s = 0;
    if (level_low && stable) {
        if (w->sec_pulses < 0xFFFFU) {
            w->sec_pulses++;
        }
        return true;
    }
    return false;
}

typedef enum {
    WIND_ACT_NONE = 0,
    WIND_ACT_PULLUP_OFF,        /**< contact stuck closed: switch the pull-up off */
    WIND_ACT_PROBE              /**< stuck: pull-up on for a moment, read, report with wind_probed() */
} wind_act_t;

/**
 * @brief The 1 s tick: closes the second, updates window, gust and stuck state.
 * @param pin_low  pin level now (ignored while stuck – the pull-up is off)
 * @param stuck_s  seconds low without an edge before the pull-up goes off
 * @param recheck_s spacing of the checks while stuck
 */
static inline wind_act_t wind_second(wind_t *w, bool pin_low, uint8_t stuck_s, uint8_t recheck_s)
{
    uint16_t n = w->sec_pulses;
    w->sec_pulses = 0;
    w->quiet = !w->edge_seen;
    w->edge_seen = false;

    /* window and gust */
    w->pulses += n;
    if (w->seconds < 0xFFFFU) {
        w->seconds++;
    }
    w->ring[w->ring_pos] = n;
    w->ring_pos = (uint8_t)((w->ring_pos + 1U) % WIND_GUST_SPAN);
    if (w->ring_fill < WIND_GUST_SPAN) {
        w->ring_fill++;
    }
    uint32_t sum = 0;
    for (uint8_t i = 0; i < WIND_GUST_SPAN; i++) {
        sum += w->ring[i];
    }
    /* a gust needs a full 3 s span; before that the partial sum is the best bound */
    uint16_t s16 = (sum > 0xFFFFU) ? 0xFFFFU : (uint16_t)sum;
    if (s16 > w->gust) {
        w->gust = s16;
    }

    /* stuck contact */
    if (w->stuck) {
        w->stuck_seen = true;
        if (++w->recheck_s >= recheck_s) {
            w->recheck_s = 0;
            return WIND_ACT_PROBE;
        }
        return WIND_ACT_NONE;
    }
    if (pin_low && w->quiet) {
        if (w->low_s < 255U) {
            w->low_s++;
        }
        if (w->low_s >= stuck_s) {
            w->stuck = true;
            w->stuck_seen = true;
            w->recheck_s = 0;
            return WIND_ACT_PULLUP_OFF;
        }
    } else if (!pin_low) {
        w->low_s = 0;
    }
    return WIND_ACT_NONE;
}

/** Result of a WIND_ACT_PROBE: returns true when the contact opened (pull-up and
 *  edge interrupt back on). The next falling edge counts (quiet). */
static inline bool wind_probed(wind_t *w, bool pin_low)
{
    if (pin_low) {
        return false;
    }
    w->stuck = false;
    w->low_s = 0;
    w->quiet = true;
    w->edge_seen = false;
    return true;
}

/** End of a window: hand out the result, start the next window right away.
 *  The gust ring carries on (a gust across the boundary counts in the new window). */
static inline void wind_take(wind_t *w, wind_report_t *r)
{
    r->overflow = w->pulses > 0xFFFEUL;
    r->pulses = r->overflow ? 0xFFFEU : (uint16_t)w->pulses;
    r->gust_pulses = w->gust;
    r->seconds = w->seconds;
    r->stuck = w->stuck_seen || w->stuck;
    w->pulses = 0;
    w->gust = 0;
    w->seconds = 0;
    w->stuck_seen = false;
}

#endif /* WIND_LOGIC_H_ */
