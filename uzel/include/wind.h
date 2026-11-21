/**
 * @file wind.h
 * @brief Anemometer reed input: pulse counting in interrupts, the CPU sleeps.
 */

#ifndef WIND_H_
#define WIND_H_

#include "config.h"
#include "wind_logic.h"

/** Pull-up and the both-edges interrupt on the reed input; starts the first window. */
void wind_init(void);

/** Called from the 1 s RTC PIT interrupt (power.c). */
void wind_on_second(void);

/** Seconds in the current window (atomic read). */
uint16_t wind_window_seconds(void);

/** End the window: result out, the next window starts at once (atomic). */
void wind_take_window(wind_report_t *r);

/** Pull-up off for good (power_off_forever): no current through a closed contact. */
void wind_off(void);

#endif /* WIND_H_ */
