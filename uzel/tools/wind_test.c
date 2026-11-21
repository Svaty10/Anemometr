/* Host test of the pulse counting, gust and stuck-contact logic (include/wind_logic.h).
 *   cc -std=gnu11 -I include tools/wind_test.c -o /tmp/wt && /tmp/wt
 * Simulates the reed contact at 1/32768 s resolution, including contact bounce,
 * and drives wind_edge() / wind_second() like the interrupts do.
 * Prints "OK" when all cases pass. */
#include <stdio.h>
#include <stdlib.h>
#include "wind_logic.h"

/* values of include/config.h (needs the AVR headers, not usable on the host) */
#define DEB      98U
#define STUCK_S  2U
#define RECHECK  2U
#define TPS      32768UL     /* RTC ticks per second */

static int fails = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); fails++; } } while (0)

typedef struct {
    wind_t w;
    uint32_t t;              /* absolute tick */
    bool level_low;          /* pin as seen by the MCU */
    bool pullup;             /* false while stuck (input disabled) */
    int probes;
    uint32_t generated;      /* pulses produced by the simulated anemometer */
} sim_t;

static void sim_init(sim_t *s)
{
    wind_reset(&s->w);
    s->t = 1000;             /* not aligned with the PIT */
    s->level_low = false;
    s->pullup = true;
    s->probes = 0;
    s->generated = 0;
}

/* advance to absolute tick `to`, running the PIT at every multiple of TPS */
static void advance(sim_t *s, uint32_t to, bool contact_closed)
{
    while (s->t < to) {
        s->t++;
        if (s->t % TPS == 0) {
            wind_act_t a = wind_second(&s->w, s->level_low, STUCK_S, RECHECK);
            if (a == WIND_ACT_PULLUP_OFF) {
                s->pullup = false;
            } else if (a == WIND_ACT_PROBE) {
                s->probes++;
                if (wind_probed(&s->w, contact_closed)) {
                    s->pullup = true;
                    s->level_low = false;
                }
            }
        }
    }
}

/* the contact changes; the pin follows only with the pull-up on (edge interrupt) */
static void set_contact(sim_t *s, bool closed)
{
    if (!s->pullup) {
        return;              /* input disabled: no interrupts */
    }
    if (s->level_low != closed) {
        s->level_low = closed;
        (void)wind_edge(&s->w, closed, (uint16_t)s->t, DEB);
    }
}

/* one revolution's pulse: close (with bounce), stay closed `dwell` ticks,
 * open (with bounce); bounce edges a few ticks apart */
static void pulse(sim_t *s, uint32_t dwell, int bounces)
{
    s->generated++;
    set_contact(s, true);
    for (int i = 0; i < bounces; i++) {
        advance(s, s->t + 2, true);
        set_contact(s, false);
        advance(s, s->t + 2, true);
        set_contact(s, true);
    }
    advance(s, s->t + dwell, true);
    set_contact(s, false);
    for (int i = 0; i < bounces; i++) {
        advance(s, s->t + 3, false);
        set_contact(s, true);
        advance(s, s->t + 3, false);
        set_contact(s, false);
    }
}

/* run `seconds` with a steady pulse rate (pulses per second, fractional ok) */
static void run_rate(sim_t *s, double hz, uint32_t seconds, int bounces)
{
    uint32_t end = s->t + seconds * TPS;
    if (hz <= 0) {
        advance(s, end, s->level_low);
        return;
    }
    double period = TPS / hz;
    double next = s->t;
    while (next + period < end) {
        next += period;
        advance(s, (uint32_t)next, false);
        uint32_t dwell = (uint32_t)(period * 0.2);   /* closed ~20 % of the turn */
        pulse(s, dwell, bounces);
    }
    advance(s, end, false);
}

int main(void)
{
    sim_t s;
    wind_report_t r;

    /* 1) steady 5 Hz with bounce for 60 s: 300 pulses (+-1 at the edges) */
    sim_init(&s);
    run_rate(&s, 5.0, 60, 3);
    wind_take(&s.w, &r);
    CHECK(r.seconds == 60);
    CHECK(r.pulses >= 299 && r.pulses <= 300);
    CHECK(r.gust_pulses >= 14 && r.gust_pulses <= 16);   /* 3 s x 5 Hz */
    CHECK(!r.stuck && !r.overflow);

    /* 2) no bounce, fast 60 Hz (~144 km/h at 2.4 km/h/Hz) */
    sim_init(&s);
    run_rate(&s, 60.0, 10, 0);
    wind_take(&s.w, &r);
    CHECK(r.pulses >= 598 && r.pulses <= 600);

    /* 3) bounce never adds pulses even at 30 Hz */
    sim_init(&s);
    run_rate(&s, 30.0, 10, 4);
    wind_take(&s.w, &r);
    CHECK(r.pulses >= 298 && r.pulses <= 300);

    /* 4) gust: 2 Hz background, 3 s burst at 20 Hz in the middle */
    sim_init(&s);
    run_rate(&s, 2.0, 20, 1);
    run_rate(&s, 20.0, 3, 1);
    run_rate(&s, 2.0, 20, 1);
    wind_take(&s.w, &r);
    CHECK(r.seconds == 43);
    CHECK(r.gust_pulses >= 56 && r.gust_pulses <= 62);
    CHECK(r.pulses >= 135 && r.pulses <= 142);

    /* 5) windows follow each other without losing pulses */
    sim_init(&s);
    uint32_t total = 0;
    for (int i = 0; i < 5; i++) {
        run_rate(&s, 3.7, 52 + i * 3, 2);
        wind_take(&s.w, &r);
        total += r.pulses;
    }
    CHECK(total == s.generated);                 /* nothing lost between windows */
    CHECK(total >= 1070);

    /* 6) cups stop with the magnet over the reed: pull-up off after a few s,
     *    checks every RECHECK s, counting again once the contact opens */
    sim_init(&s);
    run_rate(&s, 4.0, 5, 1);
    set_contact(&s, true);                       /* stops closed */
    advance(&s, s.t + 10 * TPS, true);
    CHECK(!s.pullup);                            /* no current through the contact */
    CHECK(s.w.stuck);
    CHECK(s.probes >= 3 && s.probes <= 5);
    wind_take(&s.w, &r);
    CHECK(r.stuck);
    CHECK(r.pulses >= 20 && r.pulses <= 21);     /* the 4 Hz part + the closing pulse */
    advance(&s, s.t + 3 * TPS, false);           /* wind again: contact opens */
    CHECK(s.pullup && !s.w.stuck);
    wind_take(&s.w, &r);
    CHECK(r.stuck);                              /* still stuck at the start of this window */
    run_rate(&s, 4.0, 10, 1);
    wind_take(&s.w, &r);
    CHECK(r.pulses >= 39 && r.pulses <= 41);
    CHECK(!r.stuck);
    wind_take(&s.w, &r);                         /* stuck flag does not stick */
    CHECK(!r.stuck);

    /* 7) slow turning (contact closed 1.5 s at a time) is not "stuck" */
    sim_init(&s);
    for (int i = 0; i < 6; i++) {
        advance(&s, s.t + TPS, false);
        pulse(&s, (uint32_t)(1.5 * TPS), 1);
    }
    advance(&s, s.t + 2 * TPS, false);
    wind_take(&s.w, &r);
    CHECK(r.pulses == 6);
    CHECK(!r.stuck);

    /* 8) calm: zero pulses, 16-bit RTC wrap does not matter */
    sim_init(&s);
    run_rate(&s, 0, 300, 0);
    set_contact(&s, true);                       /* first pulse after a long calm */
    advance(&s, s.t + 500, true);
    set_contact(&s, false);
    advance(&s, s.t + TPS, false);
    wind_take(&s.w, &r);
    CHECK(r.pulses == 1);

    if (fails == 0) {
        printf("OK\n");
        return 0;
    }
    return 1;
}
